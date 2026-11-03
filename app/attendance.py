"""Attendance automation engine. No UI code; everything is injected so it can be tested.

Rules:
* Auto check-in: workday + detection window, session unlocked, user input observed at the
  start and again after the confirmation delay, (optionally) on a trusted office network.
* Auto check-out: only after a *confirmed* departure from the office network (gone for the
  grace period, network settled). Lock/idle alone never checks out. Anything ambiguous
  (still active on the PC, laptop was asleep, Odoo can't record the real departure time,
  or the user asked to confirm) becomes a question instead of a record.
* Every action re-reads Odoo first (no duplicates) and confirms afterwards.
* Offline actions are retried with backoff; once stale they need manual confirmation.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable

from .config import Config, parse_hm
from .odoo_client import (AttendanceStatus, OdooAccessError, OdooAuthError, OdooError,
                          OdooNetworkError)
from .presence import PresenceSnapshot, match_trusted

log = logging.getLogger(__name__)

ACTIVE_IDLE_S = 60            # input within this many seconds = user active right now
AMBIGUOUS_ACTIVE_IDLE_S = 300
NETWORK_SETTLE = timedelta(minutes=2)
WAKE_GAP = timedelta(minutes=2)
BACKOFF_START_S, BACKOFF_MAX_S = 30, 900
MIN_SYNC_GAP = timedelta(seconds=30)
STALE_EXPLICIT = timedelta(minutes=15)   # Odoo can record the original timestamp
STALE_NOW_ONLY = timedelta(minutes=5)    # Odoo records "now" -> stale quickly
AUTO_FAILURE_PAUSE = timedelta(minutes=30)
REMINDER_AFTER_FINISH = timedelta(minutes=60)
FINISH_ASK_WINDOW = timedelta(hours=3)   # don't ask at finish time if we only notice much later
LATE_ANSWER = timedelta(minutes=15)      # a "yes" this late would record a wrong "now"
DORMANT_POLL_S = 600                     # days off: check the date every 10 minutes
POWER_OFFICE_RECENT = timedelta(minutes=5)


class Status(str, Enum):
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    WAITING = "waiting"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class View:
    status: Status
    status_text: str
    checkin_text: str
    tooltip: str
    paused: bool


@dataclass
class Pending:
    action: str        # "check_in" | "check_out"
    at: datetime       # when the real event happened
    created: datetime
    source: str        # "auto" | "manual"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _dt(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _hm(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%H:%M") if dt else "—"


class AttendanceEngine:
    def __init__(self, config: Config, client_factory: Callable, presence, state_path: Path,
                 notify: Callable[[str, str], None] | None = None,
                 confirm: Callable[[str, str, Callable[[bool], None]], None] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 on_change: Callable[[View], None] | None = None,
                 fresh_login: bool = False,
                 reload_config: Callable[[], Config | None] | None = None):
        self.cfg = config
        self._factory = client_factory
        self._presence = presence
        self._state_path = Path(state_path)
        self._notify = notify or (lambda title, msg: None)
        self._confirm = confirm or (lambda title, msg, cb: None)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.on_change = on_change
        self._fresh_login = fresh_login
        self._reload_config = reload_config

        self._client = None
        self._commands: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.known: AttendanceStatus | None = None
        self.synced_at: datetime | None = None
        self.error: str | None = None
        self.error_kind: str | None = None   # network | auth | access | odoo | config
        self.next_sync_at: datetime | None = None
        self._backoff = BACKOFF_START_S
        self.pending: Pending | None = None
        self.candidate_since: datetime | None = None
        self.office_last_seen: datetime | None = None
        self.off_since: datetime | None = None
        self.prev_locked: bool | None = None
        self.last_tick: datetime | None = None
        self.last_wake: datetime | None = None
        self.suppress_auto_checkin = False
        self.paused = False
        self.declined_departure_for: str | None = None
        self.question: str | None = None
        self.auto_blocked_until: datetime | None = None
        self.dormant = False
        self._notified: set[str] = set()
        self._view: View | None = None
        self._saved: str | None = None
        self._load_state()

    # ---- thread / commands ---------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._save_state()

    def command(self, name: str, payload=None) -> None:
        self._commands.put((name, payload))
        self._wake.set()

    def on_power_event(self, kind: str, timeout: float = 5.0) -> None:
        """Called (from any thread) just before sleep/shutdown; waits briefly for the engine."""
        done = threading.Event()
        self.command("power", (kind, done))
        done.wait(timeout)

    def _interval(self) -> float:
        return DORMANT_POLL_S if self.dormant else self.cfg.poll_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Engine tick failed")
            self._wake.wait(self._interval())
            self._wake.clear()

    # ---- main tick -------------------------------------------------------
    def tick(self) -> None:
        now = self._clock()
        if self.last_tick is None:
            self._on_wake(now, "startup")
            if self._fresh_login:
                self.suppress_auto_checkin = False
        elif now - self.last_tick > max(WAKE_GAP, timedelta(seconds=3 * self._interval())):
            self._on_wake(now, f"resume after {now - self.last_tick}")
        self.last_tick = now

        if self._reload_config:
            try:
                new_cfg = self._reload_config()
            except Exception:
                log.exception("Config reload failed")
                new_cfg = None
            if new_cfg is not None:
                self._handle(now, "reload", new_cfg)
        self._drain_commands(now)
        dormant = self._is_dormant(now)
        if dormant != self.dormant:
            log.info("Day off: automation sleeping" if dormant else "Automation active")
            self.dormant = dormant
        if dormant:
            self._publish()
            self._save_state()
            return
        snap = self._snapshot()
        on_office = match_trusted(snap.network_ids, self.cfg.trusted_networks) if snap else None
        if snap:
            self._track(now, snap, on_office)
        self._sync(now)
        self._retry_pending(now)
        if snap and not self.paused:
            self._eval_checkin(now, snap, on_office)
            self._eval_checkout(now, snap, on_office)
        self._reminders(now)
        if self._is_dormant(now):          # e.g. first sync of a day off just finished
            log.info("Day off: automation sleeping")
            self.dormant = True
            self.candidate_since = None
        self._publish()
        self._save_state()

    def _is_dormant(self, now: datetime) -> bool:
        """Non-workday, checked out, nothing pending, already synced today: nothing to watch."""
        k = self.known
        return bool(
            self.cfg.dormant_on_days_off and now.weekday() not in self.cfg.workdays
            and k is not None and not k.checked_in
            and not (self.pending or self.question or self.candidate_since or self.error_kind)
            and self.synced_at is not None and self.synced_at.date() == now.date())

    def _snapshot(self) -> PresenceSnapshot | None:
        try:
            return self._presence.snapshot()
        except Exception as exc:
            log.warning("Presence snapshot failed: %s", exc)
            return None

    def _on_wake(self, now: datetime, reason: str) -> None:
        log.info("Wake/start detected (%s): resetting presence confirmation, resyncing", reason)
        self.candidate_since = None
        self.off_since = None
        self.last_wake = now
        self._backoff = BACKOFF_START_S
        if self.error_kind not in ("auth", "config"):
            self.next_sync_at = None
        invalidate = getattr(self._presence, "invalidate_network", None)
        if invalidate:
            invalidate()

    def _track(self, now: datetime, snap: PresenceSnapshot, on_office: bool | None) -> None:
        if self.prev_locked and not snap.locked:
            log.info("Session unlocked")
            self.suppress_auto_checkin = False
            self._force_sync()
        if self.prev_locked is not None and snap.locked and not self.prev_locked:
            log.info("Session locked")
            self.candidate_since = None
        self.prev_locked = snap.locked
        if on_office:
            if self.off_since is not None:
                log.info("Back on office network")
            self.office_last_seen = now
            self.off_since = None
            self.declined_departure_for = None
        elif on_office is False and self.off_since is None:
            log.info("Not on office network")
            self.off_since = now

    # ---- Odoo calls ------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            try:
                self._client = self._factory(self.cfg)
            except Exception as exc:
                self._client = None
                self._set_error("config", f"Invalid settings: {exc}")
                return None
            if self._client is None:
                self._set_error("config", "Not configured — open Settings")
        return self._client

    def _drop_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def _set_error(self, kind: str, msg: str) -> None:
        if (kind, msg) != (self.error_kind, self.error):
            log.warning("Odoo %s error: %s", kind, msg)
        self.error, self.error_kind = msg, kind

    def _call(self, now: datetime, fn):
        client = self._get_client()
        if client is None:
            return False, None
        try:
            result = fn(client)
        except OdooNetworkError as exc:
            self._fail(now, "network", str(exc))
            return False, None
        except OdooAuthError as exc:
            self._fail(now, "auth", f"Login failed — check Settings ({exc})")
            self._drop_client()
            self._notify_once("err:auth", "Odoo login failed", "Automation is on hold. Open Settings to fix the login.")
            return False, None
        except OdooAccessError as exc:
            self._fail(now, "access", str(exc))
            self._notify_once(f"err:access:{exc}", "Odoo refused the request", str(exc)[:200])
            return False, None
        except OdooError as exc:
            self._fail(now, "odoo", str(exc))
            return False, None
        except Exception as exc:  # never let a client bug kill the engine
            log.exception("Unexpected client failure")
            self._fail(now, "odoo", f"Unexpected error: {exc.__class__.__name__}")
            return False, None
        if self.error_kind:
            log.info("Odoo reachable again")
        self.error = self.error_kind = None
        self._notified = {k for k in self._notified if not k.startswith("err:")}
        self._backoff = BACKOFF_START_S
        self.next_sync_at = now + timedelta(minutes=self.cfg.sync_interval_min)
        return True, result

    def _fail(self, now: datetime, kind: str, msg: str) -> None:
        self._set_error(kind, msg)
        if kind == "auth":
            self.next_sync_at = None   # don't retry (avoids lockouts) until Settings/manual action
            return
        self.next_sync_at = now + timedelta(seconds=self._backoff)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

    def _force_sync(self) -> None:
        if self.error_kind in ("auth", "config"):
            return
        self.next_sync_at = (self.synced_at + MIN_SYNC_GAP) if self.synced_at else None

    def _sync(self, now: datetime) -> None:
        if self.error_kind in ("auth", "config"):
            return
        if self.next_sync_at is not None and now < self.next_sync_at:
            return
        ok, status = self._call(now, lambda c: c.get_status())
        if ok:
            self._set_known(status, now)

    def _set_known(self, status: AttendanceStatus, now: datetime) -> None:
        if self.known is None or status.checked_in != self.known.checked_in:
            log.info("Odoo state: %s", "checked in" if status.checked_in else "checked out")
        self.known, self.synced_at = status, now

    def _explicit(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "supports_explicit_time", False))

    def _execute(self, now: datetime, action: str, at: datetime | None) -> bool:
        want = action == "check_in"

        def op(client):
            use_at = at if (at is not None and client.supports_explicit_time) else None
            return client.set_checked_in(want, use_at)

        ok, res = self._call(now, op)
        if not ok:
            return False
        status, changed = res
        self._set_known(status, now)
        if self.pending and self.pending.action == action:
            self.pending = None
        if changed:
            when = status.open_check_in if want else (at if self._explicit() and at else now)
            word = "Checked in" if want else "Checked out"
            log.info("%s (Odoo confirmed, %s)", word, _hm(when))
            if self.cfg.notify_actions:
                self._notify(word, f"Odoo attendance recorded at {_hm(when)}.")
        else:
            log.info("Already %s in Odoo; nothing to do", "checked in" if want else "checked out")
        return True

    def _run_action(self, now: datetime, action: str, at: datetime, source: str) -> bool:
        if self._execute(now, action, at):
            return True
        if self.error_kind == "network":
            if not self.pending:
                log.info("Odoo unreachable; %s queued for retry", action)
            self.pending = Pending(action, at, now, source)
            self._notify_once(f"err:offline:{action}", "Odoo unreachable",
                              f"Will retry the {action.replace('_', '-')} shortly.")
        else:
            if source == "auto":
                self.auto_blocked_until = now + AUTO_FAILURE_PAUSE
            self._notify("Attendance not recorded", (self.error or "Odoo error")[:200])
        return False

    def _retry_pending(self, now: datetime) -> None:
        p = self.pending
        if not p or self.question or self.error_kind:
            return  # errors: wait for the backoff sync to succeed first
        limit = STALE_EXPLICIT if self._explicit() else STALE_NOW_ONLY
        if now - p.created > limit:
            self.pending = None
            log.info("Queued %s from %s is stale; asking for confirmation", p.action, _hm(p.at))
            verb = "check in" if p.action == "check_in" else "check out"
            when = (f"at {_hm(p.at)} (the original time)" if self._explicit()
                    else f"now ({_hm(now)}); Odoo will record the current time")
            self._ask(f"stale:{p.action}", "Confirm attendance",
                      f"Odoo was unreachable when you wanted to {verb} at {_hm(p.at)}.\n\n"
                      f"Record the {verb} {when}?", p)
            return
        if not self._execute(now, p.action, p.at) and self.error_kind != "network":
            self.pending = None  # a real refusal: don't keep retrying it
            self._notify("Attendance not recorded", (self.error or "Odoo error")[:200])

    # ---- questions -------------------------------------------------------
    def _ask(self, key: str, title: str, text: str, ctx) -> None:
        if self.question:
            return
        self.question = key

        def done(answer: bool) -> None:
            self.command("answer", (key, bool(answer), ctx))

        try:
            self._confirm(title, text, done)
        except Exception:
            log.exception("Confirmation dialog failed")
            self.question = None

    def _drain_commands(self, now: datetime) -> None:
        while True:
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(now, name, payload)
            except Exception:
                log.exception("Command %s failed", name)

    def _handle(self, now: datetime, name: str, payload) -> None:
        if name in ("check_in", "check_out"):
            log.info("Manual %s requested", name)
            self.candidate_since = None
            self.pending = None
            self.suppress_auto_checkin = name == "check_out"
            if self.error_kind in ("auth", "access", "odoo"):
                self.error = self.error_kind = None
                self._drop_client()
            self._run_action(now, name, now, "manual")
        elif name == "refresh":
            if self.error_kind in ("auth", "access", "odoo", "config"):
                self.error = self.error_kind = None
                self._drop_client()
            self.next_sync_at = None
        elif name == "pause":
            self.paused = bool(payload) if payload is not None else not self.paused
            self.candidate_since = None
            log.info("Automation %s", "paused" if self.paused else "resumed")
        elif name == "reload":
            self.cfg = payload
            self._drop_client()
            self.error = self.error_kind = None
            self.next_sync_at = None
            self.candidate_since = None
            self.auto_blocked_until = None
            log.info("Settings reloaded")
        elif name == "power":
            kind, done = payload
            try:
                self._power_checkout(now, kind)
            finally:
                done.set()
        elif name == "answer":
            key, yes, ctx = payload
            self.question = None
            log.info("Answer to %s: %s", key, "yes" if yes else "no")
            if key.startswith("stale:") and yes:
                self._run_action(now, ctx.action, ctx.at, "manual")
            elif key == "departure":
                if not yes:
                    self.declined_departure_for = ctx["check_in"]
                elif not self._explicit() and now - ctx["asked"] > LATE_ANSWER:
                    self._late_answer(now, ctx["asked"])
                else:
                    self.suppress_auto_checkin = True
                    self._run_action(now, "check_out", ctx["departure"], "manual")
            elif key == "finish" and yes:
                if now - ctx["asked"] > LATE_ANSWER:
                    self._late_answer(now, ctx["asked"])
                else:
                    self.suppress_auto_checkin = True
                    self._run_action(now, "check_out", now, "manual")

    def _power_checkout(self, now: datetime, kind: str) -> None:
        log.info("System is going to %s", kind)
        k = self.known
        if not self.cfg.checkout_on_sleep_after_finish or self.paused or (k is not None and not k.checked_in):
            return
        if now.weekday() not in self.cfg.workdays:
            return
        fh, fm = parse_hm(self.cfg.finish_time)
        if now < now.replace(hour=fh, minute=fm, second=0, microsecond=0):
            log.info("Before finish time: staying checked in")
            return
        at_office = self.office_last_seen is not None and now - self.office_last_seen <= POWER_OFFICE_RECENT
        if self.cfg.trusted_networks and not at_office:
            log.info("Not at the office: %s is not treated as leaving work", kind)
            return
        log.info("Leaving work (%s after finish time at the office); checking out", kind)
        self.suppress_auto_checkin = True
        self._run_action(now, "check_out", now, "auto")

    def _late_answer(self, now: datetime, asked: datetime) -> None:
        mins = int((now - asked).total_seconds() // 60)
        log.info("Check-out answer arrived %s min after the question; not recording", mins)
        self._notify("Not checked out", f"You answered {mins} min after the question, so the time would be "
                     "wrong. Use Check Out Now, or correct the time in Odoo.")

    # ---- automation rules ------------------------------------------------
    def _in_checkin_window(self, now: datetime) -> bool:
        if now.weekday() not in self.cfg.workdays:
            return False
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sh, sm = parse_hm(self.cfg.start_time)
        fh, fm = parse_hm(self.cfg.finish_time)
        lo = day + timedelta(hours=sh, minutes=sm - self.cfg.earliest_before_start_min)
        hi = day + timedelta(hours=fh, minutes=fm)
        return lo <= now <= hi

    def _eval_checkin(self, now: datetime, snap: PresenceSnapshot, on_office: bool | None) -> None:
        blocked = (
            not self.cfg.auto_checkin
            or (self.known is not None and self.known.checked_in)
            or self.error_kind in ("auth", "config", "access")
            or self.pending is not None
            or self.question is not None
            or self.suppress_auto_checkin
            or (self.auto_blocked_until and now < self.auto_blocked_until)
            or not self._in_checkin_window(now)
            or snap.locked
        )
        if blocked:
            self.candidate_since = None
            return
        if on_office is False and self.cfg.checkin_requires_office:
            self.candidate_since = None
            if snap.idle_seconds <= ACTIVE_IDLE_S and self.cfg.notify_reminders:
                self._notify_once(f"remote:{now.date()}", "Not on the office network",
                                  "Automatic check-in skipped. Working remotely? Use Check In Now.")
            return
        if self.candidate_since is None:
            if snap.idle_seconds <= ACTIVE_IDLE_S:
                self.candidate_since = now
                log.info("Presence detected; confirming for %s min", self.cfg.checkin_delay_min)
            return
        elapsed = now - self.candidate_since
        if elapsed < timedelta(minutes=self.cfg.checkin_delay_min):
            return
        if snap.idle_seconds >= elapsed.total_seconds():
            log.info("No input since presence was first seen; restarting confirmation")
            self.candidate_since = None
            return
        started = self.candidate_since
        self.candidate_since = None
        log.info("Presence confirmed (since %s); checking in", _hm(started))
        self._run_action(now, "check_in", started, "auto")

    def _eval_checkout(self, now: datetime, snap: PresenceSnapshot, on_office: bool | None) -> None:
        k = self.known
        if (not self.cfg.auto_checkout or on_office is not False or k is None or not k.checked_in
                or self.pending or self.question or self.error_kind
                or self.office_last_seen is None or self.off_since is None or k.open_check_in is None):
            return
        if self.office_last_seen < k.open_check_in:
            return  # never at the office during this attendance (remote work): not a departure
        if self.declined_departure_for == _iso(k.open_check_in):
            return
        if (now - self.office_last_seen < timedelta(minutes=self.cfg.checkout_grace_min)
                or now - self.off_since < NETWORK_SETTLE):
            return
        departure = self.office_last_seen
        active = not snap.locked and snap.idle_seconds < AMBIGUOUS_ACTIVE_IDLE_S
        slept = self.last_wake is not None and self.last_wake > departure
        late = now - departure > timedelta(minutes=self.cfg.checkout_grace_min) + 2 * NETWORK_SETTLE
        explicit = self._explicit()
        if self.cfg.confirm_checkout or active or slept or late or not explicit:
            when = (f"at {_hm(departure)}" if explicit
                    else f"now ({_hm(now)}) — this Odoo account can only record the current time")
            extra = "\n\nYou still seem to be using this PC." if active else ""
            self._ask("departure", "Did you finish work?",
                      f"You left the office network at {_hm(departure)}.{extra}\n\n"
                      f"Check out {when}?\n\nChoose No if you are still working.",
                      {"departure": departure, "check_in": _iso(k.open_check_in), "asked": now})
            return
        log.info("Departure confirmed (left office %s); checking out", _hm(departure))
        self.suppress_auto_checkin = True
        self._run_action(now, "check_out", departure, "auto")

    def _reminders(self, now: datetime) -> None:
        k = self.known
        if not (k and k.checked_in and self.cfg.notify_reminders):
            return
        if k.open_check_in and k.open_check_in.date() < now.date():
            self._notify_once(f"stale_open:{_iso(k.open_check_in)}", "Still checked in from an earlier day",
                              f"Open attendance since {k.open_check_in:%a %H:%M}. Check out or fix it in Odoo.")
            return
        if now.weekday() in self.cfg.workdays:
            fh, fm = parse_hm(self.cfg.finish_time)
            finish = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
            key = f"finish:{now.date()}"
            if (self.cfg.ask_checkout_at_finish and not self.paused and finish <= now <= finish + FINISH_ASK_WINDOW
                    and not (self.question or self.pending or self.error_kind) and key not in self._notified
                    and not (k.open_check_in and k.open_check_in > finish)):
                self._notified.add(key)
                self._ask("finish", "Workday finished?",
                          f"Your workday ends at {finish:%H:%M} and you are checked in since "
                          f"{_hm(k.open_check_in)}.\n\nCheck out now ({_hm(now)})?\n\n"
                          "Choose No if you are still working.", {"asked": now})
                return
            if now >= finish + REMINDER_AFTER_FINISH:
                self._notify_once(f"late:{now.date()}", "Still checked in",
                                  f"Checked in since {_hm(k.open_check_in)}. Use Check Out Now when you finish.")

    def _notify_once(self, key: str, title: str, msg: str) -> None:
        if key in self._notified:
            return
        self._notified.add(key)
        self._notify(title, msg)

    # ---- view / persistence ---------------------------------------------
    def view(self) -> View:
        k = self.known
        if self.error_kind:
            status = Status.ERROR
        elif self.candidate_since or self.pending or self.question:
            status = Status.WAITING
        elif k is None:
            status = Status.UNKNOWN
        else:
            status = Status.CHECKED_IN if k.checked_in else Status.CHECKED_OUT

        state = ("Checked in" if k and k.checked_in else "Checked out") if k else "Unknown"
        if status == Status.ERROR:
            text = f"Error: {self.error}"
        elif status == Status.WAITING:
            text = ("Waiting: confirming presence" if self.candidate_since
                    else "Waiting: retrying Odoo" if self.pending else "Waiting for your confirmation")
        elif self.dormant:
            text = f"{state} — day off, automation sleeping"
        else:
            text = state
        if self.paused:
            text += " (automation paused)"
        first = k.first_check_in_today if k else None
        checkin = f"Today's check-in: {_hm(first)}"
        tip = f"Odoo Attendance — {state}"
        if k and k.checked_in:
            tip += f" since {_hm(k.open_check_in)}"
        if status in (Status.ERROR, Status.WAITING):
            tip += f"\n{text}"
        elif self.paused:
            tip += " (paused)"
        return View(status, f"Status: {text}"[:120], checkin, tip[:127], self.paused)

    def _publish(self) -> None:
        v = self.view()
        if v != self._view:
            self._view = v
            if self.on_change:
                try:
                    self.on_change(v)
                except Exception:
                    log.exception("UI update failed")

    def _state_dict(self) -> dict:
        k = self.known
        p = self.pending
        return {
            "known": {"checked_in": k.checked_in, "open_attendance_id": k.open_attendance_id,
                      "open_check_in": _iso(k.open_check_in),
                      "first_check_in_today": _iso(k.first_check_in_today)} if k else None,
            "synced_at": _iso(self.synced_at),
            "pending": {"action": p.action, "at": _iso(p.at), "created": _iso(p.created),
                        "source": p.source} if p else None,
            "office_last_seen": _iso(self.office_last_seen.replace(second=0, microsecond=0))
            if self.office_last_seen else None,
            "suppress_auto_checkin": self.suppress_auto_checkin,
            "paused": self.paused,
            "declined_departure_for": self.declined_departure_for,
        }

    def _save_state(self) -> None:
        data = json.dumps(self._state_dict(), indent=1)
        if data == self._saved:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self._state_path)
            self._saved = data
        except OSError as exc:
            log.warning("Could not save state: %s", exc)

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        k = raw.get("known")
        if isinstance(k, dict):
            # Shown until the first sync; never used to act without re-reading Odoo.
            self.known = AttendanceStatus(bool(k.get("checked_in")), k.get("open_attendance_id"),
                                          _dt(k.get("open_check_in")), _dt(k.get("first_check_in_today")))
        p = raw.get("pending")
        if isinstance(p, dict) and _dt(p.get("at")) and _dt(p.get("created")):
            self.pending = Pending(p["action"], _dt(p["at"]), _dt(p["created"]), p.get("source", "auto"))
        self.synced_at = None
        self.office_last_seen = _dt(raw.get("office_last_seen"))
        self.suppress_auto_checkin = bool(raw.get("suppress_auto_checkin"))
        self.paused = bool(raw.get("paused"))
        self.declined_departure_for = raw.get("declined_departure_for")
