"""Odoo Attendance adapter.

Transports (tried in this order when ``transport="auto"``):

* ``json2``   - Odoo 19+ external JSON-2 API (``/json/2/<model>/<method>``, bearer API key).
* ``jsonrpc`` - legacy external API (``/jsonrpc`` execute_kw; deprecated, removed in Odoo 22).
                External APIs require a Custom plan on Odoo Online.
* ``web``     - the same authenticated session the Odoo web client uses
                (``/web/session/authenticate`` + ``/web/dataset/call_kw``). Works on every plan,
                needs the account password (not an API key) and no 2FA.

Write modes (how a check-in/out is recorded), chosen per account:

* ``direct``  - create/write ``hr.attendance`` (needs Attendance officer rights). Can record an
                exact past timestamp (e.g. the moment you left the office).
* ``manual``  - ``hr.employee.attendance_manual`` (Odoo <= 16, what "My Attendances" calls).
* ``systray`` - ``/hr_attendance/systray_check_in_out`` (Odoo 17+, the systray button).
                Odoo records the current server time.

Every change is preceded by a fresh state read (no duplicate toggles) and followed by a
confirmation read. Secrets are never logged.
"""
from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from .config import normalize_url

log = logging.getLogger(__name__)

ODOO_DT = "%Y-%m-%d %H:%M:%S"
USER_AGENT = "OdooAutoAttendance/1.0"
MY_ATTENDANCE_ACTION = "hr_attendance.hr_attendance_action_my_attendances"


class OdooError(Exception):
    """Odoo answered with an error."""


class OdooNetworkError(OdooError):
    """Odoo unreachable (offline, DNS, timeout, 502/503/504)."""


class OdooAuthError(OdooError):
    """Credentials rejected / API key invalid or expired."""


class OdooAccessError(OdooError):
    """Authenticated but not allowed (permissions, plan restrictions)."""


class OdooSessionExpired(OdooError):
    pass


class OdooNotFound(OdooError):
    """Endpoint or method does not exist on this server."""


def to_odoo(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ODOO_DT)


def from_odoo(value) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value[:19], ODOO_DT).replace(tzinfo=timezone.utc).astimezone()


def _first_int(value) -> int:
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else 0


def _map_rpc_error(err: dict) -> OdooError:
    data = err.get("data") or {}
    name = str(data.get("name") or "")
    msg = str(data.get("message") or err.get("message") or "Odoo error")
    if "SessionExpired" in name:
        return OdooSessionExpired(msg)
    if name.endswith("AccessDenied"):
        return OdooAuthError(msg)
    if name.endswith("AccessError"):
        return OdooAccessError(msg)
    if err.get("code") == 404 or "NotFound" in name or name.endswith("AttributeError"):
        return OdooNotFound(msg)
    return OdooError(msg)


@dataclass
class ServerInfo:
    version: str
    major: int
    minor: int


@dataclass
class AttendanceStatus:
    checked_in: bool
    open_attendance_id: int | None = None
    open_check_in: datetime | None = None
    first_check_in_today: datetime | None = None


class _Http:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None, timeout: float):
        self.client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            transport=transport,
            follow_redirects=True,
        )
        self._ids = itertools.count(1)

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.TimeoutException:
            raise OdooNetworkError(f"Timed out contacting Odoo ({path})") from None
        except httpx.TransportError as exc:
            raise OdooNetworkError(f"Cannot reach Odoo ({exc.__class__.__name__})") from None
        if r.url.scheme != "https" and r.url.host not in ("localhost", "127.0.0.1", "::1"):
            raise OdooError("Odoo redirected to a non-HTTPS address; refusing.")
        if r.status_code in (502, 503, 504):
            raise OdooNetworkError(f"Odoo temporarily unavailable (HTTP {r.status_code})")
        return r

    def jsonrpc(self, path: str, params: dict):
        r = self.request("POST", path, json={"jsonrpc": "2.0", "method": "call",
                                             "params": params, "id": next(self._ids)})
        if r.status_code == 404:
            raise OdooNotFound(f"{path} not found")
        if r.status_code >= 400:
            raise OdooError(f"HTTP {r.status_code} from {path}")
        try:
            body = r.json()
        except ValueError:
            raise OdooError(f"Unexpected non-JSON response from {path}") from None
        if body.get("error"):
            raise _map_rpc_error(body["error"])
        return body.get("result")


class _JsonRpc:
    kind = "jsonrpc"
    label = "External API (JSON-RPC)"

    def __init__(self, http: _Http, db: str, login: str, secret: str):
        self.http, self.db, self.login, self._secret = http, db, login, secret
        self.uid: int | None = None

    def authenticate(self) -> None:
        if not self.db:
            raise OdooError("Database name is required for JSON-RPC.")
        uid = self.http.jsonrpc("/jsonrpc", {"service": "common", "method": "authenticate",
                                             "args": [self.db, self.login, self._secret, {}]})
        if not uid:
            raise OdooAuthError("Login or password/API key rejected by the external API.")
        self.uid = uid

    def execute(self, model, method, ids=None, args=(), kwargs=None):
        pos = ([list(ids)] if ids is not None else []) + list(args)
        return self.http.jsonrpc("/jsonrpc", {"service": "object", "method": "execute_kw", "args": [
            self.db, self.uid, self._secret, model, method, pos, kwargs or {}]})


class _Json2:
    kind = "json2"
    label = "External API (JSON-2)"

    def __init__(self, http: _Http, db: str, login: str, secret: str):
        self.http, self.db, self._secret = http, db, secret
        self.uid: int | None = None

    def authenticate(self) -> None:
        ctx = self.execute("res.users", "context_get")
        self.uid = ctx.get("uid") if isinstance(ctx, dict) else None
        if not self.uid:
            raise OdooError("JSON-2: could not determine the current user.")

    def execute(self, model, method, ids=None, args=(), kwargs=None):
        if args:
            raise ValueError("JSON-2 only accepts named arguments")
        body = dict(kwargs or {})
        if ids is not None:
            body["ids"] = list(ids)
        headers = {"Authorization": f"bearer {self._secret}"}
        if self.db:
            headers["X-Odoo-Database"] = self.db
        r = self.http.request("POST", f"/json/2/{model}/{method}", json=body, headers=headers)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code == 200 and payload is not None:
            return payload
        err = payload if isinstance(payload, dict) else {}
        msg = str(err.get("message") or f"HTTP {r.status_code}")
        name = str(err.get("name") or "")
        if r.status_code == 401:
            raise OdooAuthError(msg)
        if r.status_code == 403 or name.endswith("AccessError"):
            raise OdooAccessError(msg)
        if r.status_code == 404 or name.endswith("AttributeError"):
            raise OdooNotFound(msg)
        raise OdooError(msg)


class _Web:
    kind = "web"
    label = "Web session"

    def __init__(self, http: _Http, db: str, login: str, secret: str):
        self.http, self.db, self.login, self._secret = http, db, login, secret
        self.uid: int | None = None

    def authenticate(self) -> None:
        if not self.db:
            raise OdooError("Database name is required for web login.")
        self.http.client.cookies.clear()
        res = self.http.jsonrpc("/web/session/authenticate",
                                {"db": self.db, "login": self.login, "password": self._secret})
        self.uid = (res or {}).get("uid") if isinstance(res, dict) else None
        if not self.uid:
            raise OdooAuthError("Web login did not complete (wrong password, or two-factor "
                                "authentication is enabled on this account).")

    def _reauth(self, fn):
        try:
            return fn()
        except OdooSessionExpired:
            log.info("Odoo web session expired; signing in again")
            self.authenticate()
            return fn()

    def execute(self, model, method, ids=None, args=(), kwargs=None):
        pos = ([list(ids)] if ids is not None else []) + list(args)
        params = {"model": model, "method": method, "args": pos, "kwargs": kwargs or {}}
        return self._reauth(lambda: self.http.jsonrpc(f"/web/dataset/call_kw/{model}/{method}", params))

    def route(self, path: str, params: dict | None = None):
        return self._reauth(lambda: self.http.jsonrpc(path, params or {}))


_TRANSPORTS = {"json2": _Json2, "jsonrpc": _JsonRpc, "web": _Web}
_MODE_LABELS = {"direct": "direct record (exact time)", "manual": "My Attendances action",
                "systray": "systray check-in button"}


class OdooClient:
    def __init__(self, url: str, database: str, login: str, secret: str, transport: str = "auto",
                 employee_id: int = 0, http_transport: httpx.BaseTransport | None = None,
                 timeout: float = 15.0):
        self.base_url = normalize_url(url)
        self.database = (database or "").strip()
        self.login = (login or "").strip()
        self._secret = secret or ""
        self.transport_pref = transport
        self._employee_override = int(employee_id or 0)
        self._http = _Http(self.base_url, http_transport, timeout)
        self.server: ServerInfo | None = None
        self.t = None
        self.employee_id: int | None = None
        self.employee_name: str = ""
        self.write_mode: str | None = None

    # ---- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._http.client.close()

    @property
    def connected(self) -> bool:
        return self.t is not None and self.write_mode is not None

    @property
    def supports_explicit_time(self) -> bool:
        return self.write_mode == "direct"

    def describe(self) -> str:
        ver = self.server.version if self.server else "unknown version"
        return (f"Odoo {ver} via {self.t.label}; employee '{self.employee_name}' "
                f"(id {self.employee_id}); records with {_MODE_LABELS.get(self.write_mode, '?')}")

    # ---- detection -------------------------------------------------------
    def detect_server(self) -> ServerInfo | None:
        info = None
        try:
            info = self._http.jsonrpc("/web/webclient/version_info", {})
        except OdooNetworkError:
            raise
        except OdooError:
            try:
                r = self._http.request("GET", "/web/version")
                info = r.json() if r.status_code == 200 else None
            except OdooNetworkError:
                raise
            except (OdooError, ValueError):
                info = None
        if not isinstance(info, dict):
            return None
        vi = info.get("server_version_info") or info.get("version_info") or []
        ver = str(info.get("server_version") or info.get("version") or "")
        major = _first_int(vi[0]) if vi else _first_int(ver)
        minor = _first_int(vi[1]) if len(vi) > 1 else 0
        return ServerInfo(ver or f"{major}.{minor}", major, minor)

    def detect_database(self) -> str:
        if self.database:
            return self.database
        dbs = None
        try:
            dbs = self._http.jsonrpc("/web/database/list", {})
        except OdooNetworkError:
            raise
        except OdooError:
            pass
        if isinstance(dbs, list) and len(dbs) == 1:
            return dbs[0]
        host = urlparse(self.base_url).hostname or ""
        if host.endswith(".odoo.com"):
            return host.split(".")[0]
        if isinstance(dbs, list) and len(dbs) > 1:
            raise OdooError("Several databases on this server; enter the database name in Settings.")
        return ""

    def _auto_order(self) -> list[str]:
        order = []
        if self.server is None or self.server.major >= 19:
            order.append("json2")
        return order + ["jsonrpc", "web"]

    def connect(self) -> str:
        """Authenticate, find the employee and choose a write mode. Read-only."""
        self.t = self.write_mode = None
        self.server = self.detect_server()
        db = self.database = self.detect_database()
        kinds = self._auto_order() if self.transport_pref == "auto" else [self.transport_pref]
        errors: list[tuple[str, OdooError]] = []
        for kind in kinds:
            t = _TRANSPORTS[kind](self._http, db, self.login, self._secret)
            try:
                t.authenticate()
                self._resolve_employee(t)
                self.write_mode = self._pick_write_mode(t)
            except OdooNetworkError:
                raise
            except OdooError as exc:
                log.info("Odoo %s: %s", kind, exc)
                errors.append((kind, exc))
                continue
            self.t = t
            log.info("Connected: %s", self.describe())
            return self.describe()
        detail = "; ".join(f"{k}: {e}" for k, e in errors) or "no connection method available"
        if errors and all(isinstance(e, OdooAuthError) for _, e in errors):
            raise OdooAuthError(detail)
        if any(isinstance(e, OdooAccessError) for _, e in errors):
            raise OdooAccessError(detail)
        raise OdooError(detail)

    def _search_read(self, t, model, domain, fields, limit=None, order=None):
        kw = {"domain": domain, "fields": fields}
        if limit:
            kw["limit"] = limit
        if order:
            kw["order"] = order
        return t.execute(model, "search_read", kwargs=kw) or []

    def _resolve_employee(self, t) -> None:
        if self._employee_override:
            self.employee_id, self.employee_name = self._employee_override, f"#{self._employee_override}"
            return
        for model in ("hr.employee", "hr.employee.public"):
            try:
                recs = self._search_read(t, model, [["user_id", "=", t.uid]], ["id", "name"], limit=5)
            except (OdooAccessError, OdooNotFound):
                continue
            if recs:
                if len(recs) > 1:
                    log.warning("Several employees linked to this user; using id %s "
                                "(set Employee ID in Settings to choose)", recs[0]["id"])
                self.employee_id, self.employee_name = recs[0]["id"], recs[0].get("name", "")
                return
        raise OdooAccessError("No employee is linked to this Odoo user (set Employee ID in Settings).")

    def _has_access(self, t, op: str) -> bool | None:
        for method in ("has_access", "check_access_rights"):
            try:
                if t.kind == "json2":
                    return bool(t.execute("hr.attendance", method, kwargs={"operation": op}))
                kw = {} if method == "has_access" else {"raise_exception": False}
                return bool(t.execute("hr.attendance", method, args=[op], kwargs=kw))
            except OdooAccessError:
                return False
            except OdooNetworkError:
                raise
            except OdooError:
                continue
        return None

    def _pick_write_mode(self, t) -> str:
        major = self.server.major if self.server else None
        if self._has_access(t, "create") and self._has_access(t, "write"):
            return "direct"
        if major is not None and major <= 16:
            return "manual"
        if t.kind == "web":
            return "systray"
        raise OdooAccessError(
            "This user may only read attendances through the external API. "
            "Use the 'web' connection method with your account password.")

    def _ensure(self):
        if not self.connected:
            self.connect()
        return self.t

    # ---- attendance ------------------------------------------------------
    def get_status(self, now: datetime | None = None) -> AttendanceStatus:
        t = self._ensure()
        now = now or datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        domain = [["employee_id", "=", self.employee_id], "|", ["check_out", "=", False],
                  ["check_in", ">=", to_odoo(midnight)]]
        recs = self._search_read(t, "hr.attendance", domain, ["id", "check_in", "check_out"],
                                 limit=50, order="check_in asc")
        open_recs = [r for r in recs if not r.get("check_out")]
        today = [from_odoo(r["check_in"]) for r in recs if from_odoo(r["check_in"]) >= midnight]
        last_open = open_recs[-1] if open_recs else None
        return AttendanceStatus(
            checked_in=bool(open_recs),
            open_attendance_id=last_open["id"] if last_open else None,
            open_check_in=from_odoo(last_open["check_in"]) if last_open else None,
            first_check_in_today=min(today) if today else None,
        )

    def set_checked_in(self, want: bool, at: datetime | None = None) -> tuple[AttendanceStatus, bool]:
        """Bring Odoo to the wanted state. Returns (confirmed status, changed?)."""
        before = self.get_status()
        if before.checked_in == want:
            return before, False
        now = datetime.now().astimezone()
        if at is not None and (self.write_mode != "direct" or at > now + timedelta(minutes=1)):
            at = None
        if at is not None and not want and before.open_check_in and at <= before.open_check_in:
            raise OdooError("Check-out time would be before the check-in time.")
        try:
            self._write(want, at or now, before)
        except OdooAccessError:
            if self.write_mode != "direct":
                raise
            # ACL probe said yes but a record rule said no: fall back to the UI methods.
            fallback = self._fallback_mode()
            if not fallback:
                raise
            log.warning("Direct attendance write refused; switching to %s", fallback)
            self.write_mode = fallback
            self._write(want, now, before)
        after = self.get_status()
        if after.checked_in != want:
            raise OdooError("Odoo did not confirm the attendance change.")
        return after, True

    def _fallback_mode(self) -> str | None:
        major = self.server.major if self.server else None
        if major is not None and major <= 16:
            return "manual"
        if self.t.kind == "web":
            return "systray"
        return None

    def _write(self, want: bool, at: datetime, before: AttendanceStatus) -> None:
        t, mode = self.t, self.write_mode
        log.info("Recording %s in Odoo (%s)", "check-in" if want else "check-out", mode)
        if mode == "direct":
            if want:
                vals = {"employee_id": self.employee_id, "check_in": to_odoo(at)}
                if t.kind == "json2":
                    t.execute("hr.attendance", "create", kwargs={"vals_list": [vals]})
                else:
                    t.execute("hr.attendance", "create", args=[vals])
            else:
                vals = {"check_out": to_odoo(at)}
                ids = [before.open_attendance_id]
                if t.kind == "json2":
                    t.execute("hr.attendance", "write", ids=ids, kwargs={"vals": vals})
                else:
                    t.execute("hr.attendance", "write", ids=ids, args=[vals])
        elif mode == "manual":
            res = t.execute("hr.employee", "attendance_manual", ids=[self.employee_id],
                            args=[MY_ATTENDANCE_ACTION])
            if isinstance(res, dict) and res.get("warning"):
                raise OdooAccessError(str(res["warning"]))
        elif mode == "systray":
            t.route("/hr_attendance/systray_check_in_out", {"latitude": False, "longitude": False})
        else:
            raise OdooError("No write method selected.")
