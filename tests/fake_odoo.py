"""In-memory fake Odoo server for httpx.MockTransport. Mirrors the documented endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

DT = "%Y-%m-%d %H:%M:%S"


def _err(name: str, message: str, code: int = 200) -> dict:
    return {"code": code, "message": "Odoo Server Error", "data": {"name": name, "message": message}}


class FakeOdoo:
    def __init__(self, version=(17, 0), can_write=False, api_allowed=True, password="pw",
                 api_key="KEY", uid=7, emp_id=12, dbs=("prod",), web_2fa=False):
        self.version = version
        self.can_write = can_write
        self.api_allowed = api_allowed
        self.password, self.api_key = password, api_key
        self.uid, self.emp_id = uid, emp_id
        self.dbs = list(dbs)
        self.web_2fa = web_2fa
        self.sessions: set[str] = set()
        self.attendances: list[dict] = []
        self.calls: list[str] = []
        self.writes = 0
        self.offline = False
        self.fail_writes: str | None = None    # exception name to raise on writes

    # ---- helpers ---------------------------------------------------------
    def open_attendance(self):
        return next((a for a in self.attendances if not a["check_out"]), None)

    def _now(self):
        return datetime.now(timezone.utc).strftime(DT)

    def _toggle(self):
        self._write_guard()
        att = self.open_attendance()
        if att:
            att["check_out"] = self._now()
        else:
            self.attendances.append({"id": len(self.attendances) + 1, "employee_id": self.emp_id,
                                     "check_in": self._now(), "check_out": False})

    def _write_guard(self):
        if self.fail_writes:
            raise RpcError(self.fail_writes, "Write refused by fake server")
        self.writes += 1

    def _search_read_attendance(self, domain):
        floor = None
        for term in domain:
            if isinstance(term, list) and term[0] == "check_in" and term[1] == ">=":
                floor = term[2]
        out = [a for a in self.attendances
               if not a["check_out"] or (floor and a["check_in"] >= floor)]
        return sorted([{k: a[k] for k in ("id", "check_in", "check_out")} for a in out],
                      key=lambda a: a["check_in"])

    def dispatch(self, model, method, ids, args, kwargs):
        self.calls.append(f"{model}.{method}")
        if (model, method) == ("res.users", "context_get"):
            return {"uid": self.uid, "tz": "UTC"}
        if model in ("hr.employee", "hr.employee.public") and method == "search_read":
            return [{"id": self.emp_id, "name": "Sam Tester"}]
        if model == "hr.attendance":
            if method == "search_read":
                return self._search_read_attendance(kwargs.get("domain", []))
            if method in ("has_access", "check_access_rights"):
                if method == "has_access" and self.version[0] < 18:
                    raise RpcError("builtins.AttributeError", "no has_access")
                return self.can_write
            if method == "create":
                if not self.can_write:
                    raise RpcError("odoo.exceptions.AccessError", "You are not allowed to create")
                vals = (kwargs.get("vals_list") or args)[0]
                vals = vals[0] if isinstance(vals, list) else vals
                if self.open_attendance():
                    raise RpcError("odoo.exceptions.ValidationError", "Already checked in")
                self._write_guard()
                self.attendances.append({"id": len(self.attendances) + 1, "employee_id": vals["employee_id"],
                                         "check_in": vals["check_in"], "check_out": False})
                return [len(self.attendances)]
            if method == "write":
                if not self.can_write:
                    raise RpcError("odoo.exceptions.AccessError", "You are not allowed to write")
                vals = kwargs.get("vals") or args[0]
                self._write_guard()
                for a in self.attendances:
                    if a["id"] in ids:
                        a.update(vals)
                return True
        if (model, method) == ("hr.employee", "attendance_manual"):
            if self.version[0] >= 17:
                raise RpcError("builtins.AttributeError", "no attendance_manual")
            self._toggle()
            return {"action": {"attendance": {}}}
        raise RpcError("builtins.AttributeError", f"{model}.{method} not found")

    # ---- HTTP ------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path.startswith("/json/2/"):
            return self._json2(request, path, body)
        params = body.get("params", {})
        try:
            result, cookie = self._rpc(request, path, params)
        except RpcError as exc:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                             "error": _err(exc.name, exc.message)})
        except NotFound:
            return httpx.Response(404, text="Not Found")
        headers = {"Set-Cookie": f"session_id={cookie}; Path=/"} if cookie else {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result},
                              headers=headers)

    def _json2(self, request, path, body):
        if self.version[0] < 19:
            return httpx.Response(404, text="Not Found")
        auth = request.headers.get("Authorization", "")
        if not self.api_allowed or auth != f"bearer {self.api_key}":
            return httpx.Response(401, json={"name": "werkzeug.exceptions.Unauthorized",
                                             "message": "Invalid apikey"})
        _, _, _, model, method = path.split("/")
        ids = body.pop("ids", None)
        body.pop("context", None)
        try:
            return httpx.Response(200, json=self.dispatch(model, method, ids, [], body))
        except RpcError as exc:
            status = 403 if exc.name.endswith("AccessError") else 422
            return httpx.Response(status, json={"name": exc.name, "message": exc.message})

    def _session_ok(self, request):
        cookie = request.headers.get("Cookie", "")
        sid = next((p.split("=", 1)[1] for p in cookie.split("; ") if p.startswith("session_id=")), None)
        return sid in self.sessions

    def _rpc(self, request, path, params):
        if path == "/web/webclient/version_info":
            major, minor = self.version
            return {"server_version": f"{major}.{minor}", "server_version_info": [major, minor, 0, "final", 0, ""],
                    "protocol_version": 1}, None
        if path == "/web/database/list":
            return self.dbs, None
        if path == "/jsonrpc":
            service, method, args = params["service"], params["method"], params["args"]
            if service == "common" and method == "authenticate":
                db, login, secret, _ = args
                ok = self.api_allowed and db in self.dbs and secret in (self.password, self.api_key)
                return (self.uid if ok else False), None
            if service == "object" and method == "execute_kw":
                db, uid, secret, model, meth, pos, kw = args
                if not self.api_allowed or secret not in (self.password, self.api_key):
                    raise RpcError("odoo.exceptions.AccessDenied", "Access Denied")
                ids = pos[0] if pos and isinstance(pos[0], list) and meth in ("write", "attendance_manual") else None
                rest = pos[1:] if ids is not None else pos
                return self.dispatch(model, meth, ids, rest, kw), None
        if path == "/web/session/authenticate":
            if params.get("password") != self.password or params.get("db") not in self.dbs:
                raise RpcError("odoo.exceptions.AccessDenied", "Wrong login/password")
            if self.web_2fa:
                return {"uid": None}, None
            sid = f"s{len(self.sessions) + 1}"
            self.sessions.add(sid)
            return {"uid": self.uid}, sid
        if path.startswith("/web/dataset/call_kw/"):
            if not self._session_ok(request):
                raise RpcError("odoo.http.SessionExpiredException", "Session expired")
            pos = params["args"]
            meth = params["method"]
            ids = pos[0] if pos and isinstance(pos[0], list) and meth in ("write", "attendance_manual") else None
            rest = pos[1:] if ids is not None else pos
            return self.dispatch(params["model"], meth, ids, rest, params["kwargs"]), None
        if path == "/hr_attendance/systray_check_in_out":
            if self.version[0] < 17:
                raise NotFound()
            if not self._session_ok(request):
                raise RpcError("odoo.http.SessionExpiredException", "Session expired")
            self.calls.append("systray")
            self._toggle()
            return {"attendance_state": "checked_in" if self.open_attendance() else "checked_out"}, None
        raise NotFound()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class RpcError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name, self.message = name, message


class NotFound(Exception):
    pass
