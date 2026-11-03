"""Automation engine tests with a fake clock, fake presence and fake Odoo client."""
from datetime import datetime, timedelta, timezone

import pytest

from app.attendance import AttendanceEngine, Status
from app.config import Config
from app.odoo_client import AttendanceStatus, OdooAuthError, OdooError, OdooNetworkError
from app.presence import PresenceSnapshot

TZ = timezone(timedelta(hours=2))
MONDAY_0800 = datetime(2026, 11, 2, 8, 0, tzinfo=TZ)
OFFICE = "OfficeWiFi"


class Clock:
    def __init__(self, t=MONDAY_0800):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class Presence:
    def __init__(self):
        self.locked, self.idle, self.nets = False, 5.0, {OFFICE}

    def snapshot(self):
        return PresenceSnapshot(self.locked, self.idle, frozenset(self.nets))


class FakeClient:
    def __init__(self, clock, explicit=True):
        self.clock = clock
        self.supports_explicit_time = explicit
        self.checked_in = False
        self.check_in_at = None
        self.writes: list[tuple[str, datetime | None]] = []
        self.reads = 0
        self.fail: Exception | None = None
        self.fail_writes: Exception | None = None

    def get_status(self):
        self.reads += 1
        if self.fail:
            raise self.fail
        return AttendanceStatus(self.checked_in, 1 if self.checked_in else None,
                                self.check_in_at if self.checked_in else None, self.check_in_at)

    def set_checked_in(self, want, at=None):
        st = self.get_status()
        if st.checked_in == want:
            return st, False
        if self.fail_writes:
            raise self.fail_writes
        self.writes.append(("in" if want else "out", at))
        self.checked_in = want
        if want:
            self.check_in_at = at or self.clock()
        return self.get_status(), True

    def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    presence = Presence()
    fake = FakeClient(clock)
    notes, questions = [], []
    cfg = Config(url="https://x.odoo.com", login="me", trusted_networks=[OFFICE],
                 confirm_checkout=False, start_time="09:00", finish_time="18:00")

    def make(**cfg_overrides):
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
        return AttendanceEngine(cfg, lambda c: fake, presence, tmp_path / "state.json",
                                notify=lambda t, m: notes.append((t, m)),
                                confirm=lambda t, m, cb: questions.append((t, m, cb)),
                                clock=clock)

    class Env:
        pass
    e = Env()
    e.clock, e.presence, e.client, e.notes, e.questions, e.make, e.cfg = clock, presence, fake, notes, questions, make, cfg
    e.path = tmp_path / "state.json"
    return e


def run(engine, clock, minutes, step_s=15):
    for _ in range(int(minutes * 60 / step_s)):
        clock.advance(seconds=step_s)
        engine.tick()


def answer(engine, env, yes):
    _, _, cb = env.questions.pop()
    cb(yes)
    engine.tick()


# ---- check-in -------------------------------------------------------------

def test_auto_check_in_after_confirmation_delay(env):
    eng = env.make()
    eng.tick()
    assert eng.view().status == Status.WAITING
    run(eng, env.clock, 1)
    assert env.client.writes == []
    run(eng, env.clock, 1.5)
    assert env.client.writes == [("in", MONDAY_0800)]      # records when presence began
    assert eng.view().status == Status.CHECKED_IN
    assert any("Checked in" in t for t, _ in env.notes)


def test_already_checked_in_does_nothing(env):
    env.client.checked_in, env.client.check_in_at = True, MONDAY_0800 - timedelta(minutes=30)
    eng = env.make()
    run(eng, env.clock, 5)
    assert env.client.writes == []
    assert eng.view().status == Status.CHECKED_IN


def test_no_check_in_while_locked_or_idle(env):
    eng = env.make()
    env.presence.locked = True
    run(eng, env.clock, 5)
    env.presence.locked, env.presence.idle = False, 600
    run(eng, env.clock, 5)
    assert env.client.writes == []


def test_activity_must_continue_during_confirmation(env):
    eng = env.make()
    eng.tick()                               # candidate starts (idle 5 s)
    env.presence.idle = 10_000               # nobody touches the PC afterwards
    run(eng, env.clock, 4)
    assert env.client.writes == []


def test_lock_during_confirmation_resets(env):
    eng = env.make()
    eng.tick()
    run(eng, env.clock, 1)
    env.presence.locked = True
    eng.tick()
    env.presence.locked = False
    run(eng, env.clock, 1)
    assert env.client.writes == []           # confirmation restarted after unlock
    run(eng, env.clock, 1.5)
    assert len(env.client.writes) == 1


def test_outside_schedule_or_weekend_no_auto_check_in(env):
    env.clock.t = MONDAY_0800.replace(hour=5)            # before start - 2h
    eng = env.make()
    run(eng, env.clock, 5)
    env.clock.t = MONDAY_0800 + timedelta(days=5)        # Saturday
    run(eng, env.clock, 5)
    assert env.client.writes == []


def test_requires_office_network_and_hints_remote(env):
    env.presence.nets = {"HomeWiFi"}
    eng = env.make()
    run(eng, env.clock, 5)
    assert env.client.writes == []
    assert any("office network" in t for t, _ in env.notes)
    eng.command("check_in")                              # remote day: manual check-in works
    eng.tick()
    assert env.client.writes[-1][0] == "in"


def test_remote_auto_check_in_when_allowed(env):
    env.presence.nets = {"HomeWiFi"}
    eng = env.make(checkin_requires_office=False)
    run(eng, env.clock, 3)
    assert len(env.client.writes) == 1


def test_manual_check_out_suppresses_auto_check_in_until_unlock(env):
    eng = env.make()
    run(eng, env.clock, 3)
    eng.command("check_out")
    eng.tick()
    run(eng, env.clock, 10)
    assert [w[0] for w in env.client.writes] == ["in", "out"]
    env.presence.locked = True
    eng.tick()
    env.presence.locked = False
    run(eng, env.clock, 3)
    assert [w[0] for w in env.client.writes] == ["in", "out", "in"]


def test_duplicate_manual_clicks_one_write(env):
    eng = env.make(auto_checkin=False)
    for _ in range(3):
        eng.command("check_in")
    eng.tick()
    assert len(env.client.writes) == 1


# ---- check-out ------------------------------------------------------------

def checked_in_at_office(env, **cfg):
    eng = env.make(**cfg)
    run(eng, env.clock, 3)
    assert env.client.checked_in
    run(eng, env.clock, 60)
    return eng


def test_lock_or_idle_never_checks_out(env):
    eng = checked_in_at_office(env)
    env.presence.locked, env.presence.idle = True, 50_000
    run(eng, env.clock, 120)
    assert env.client.checked_in and env.questions == []


def test_confirmed_departure_checks_out_at_departure_time(env):
    eng = checked_in_at_office(env)
    left = env.clock.t
    env.presence.locked, env.presence.idle = True, 5_000
    env.presence.nets = set()                            # laptop left office, still awake
    run(eng, env.clock, 20)
    out = env.client.writes[-1]
    assert out[0] == "out" and left <= out[1] <= left + timedelta(seconds=15)
    assert env.questions == []


def test_brief_network_drop_within_grace_is_ignored(env):
    eng = checked_in_at_office(env)
    env.presence.nets = set()
    run(eng, env.clock, 10)
    env.presence.nets = {OFFICE}
    run(eng, env.clock, 30)
    assert env.client.checked_in and env.questions == []


def test_departure_while_still_active_asks(env):
    eng = checked_in_at_office(env)
    env.presence.nets = {"Hotspot"}                      # still typing: ambiguous
    run(eng, env.clock, 20)
    assert env.client.checked_in and len(env.questions) == 1
    answer(eng, env, False)                              # "still working"
    run(eng, env.clock, 60)
    assert env.client.checked_in and env.questions == []  # not asked again


def test_departure_confirmation_yes_records_departure_time(env):
    eng = checked_in_at_office(env, confirm_checkout=True)
    left = env.clock.t
    env.presence.locked = True
    env.presence.nets = set()
    run(eng, env.clock, 20)
    assert len(env.questions) == 1 and env.client.checked_in
    assert eng.view().status == Status.WAITING
    answer(eng, env, True)
    assert not env.client.checked_in
    assert left <= env.client.writes[-1][1] <= left + timedelta(seconds=15)


def test_remote_day_never_auto_checks_out(env):
    env.presence.nets = {"HomeWiFi"}
    eng = env.make()
    eng.command("check_in")
    eng.tick()
    env.presence.locked = True
    run(eng, env.clock, 120)
    assert env.client.checked_in and env.questions == []


def test_sleep_and_wake_at_home_asks_instead_of_acting(env):
    eng = checked_in_at_office(env)
    env.clock.advance(hours=2)                           # laptop slept, woke at home
    env.presence.nets = {"HomeWiFi"}
    run(eng, env.clock, 5)
    assert env.client.checked_in
    assert len(env.questions) == 1


def test_wake_in_office_before_wifi_reconnects_is_not_departure(env):
    eng = checked_in_at_office(env)
    env.clock.advance(hours=1)                           # lunch sleep
    env.presence.nets = set()                            # Wi-Fi still reconnecting
    run(eng, env.clock, 1)
    env.presence.nets = {OFFICE}
    run(eng, env.clock, 30)
    assert env.client.checked_in and env.questions == []


def test_now_only_odoo_always_asks_before_checkout(env):
    env.client.supports_explicit_time = False
    eng = checked_in_at_office(env)
    env.presence.locked = True
    env.presence.nets = set()
    run(eng, env.clock, 20)
    assert env.client.checked_in and len(env.questions) == 1
    assert "current time" in env.questions[0][1]


# ---- connectivity / errors ------------------------------------------------

def test_offline_check_in_is_queued_and_replayed_on_reconnect(env):
    eng = env.make()
    env.client.fail = OdooNetworkError("offline")
    run(eng, env.clock, 3)
    assert eng.pending and eng.pending.action == "check_in"
    assert eng.view().status == Status.ERROR
    env.client.fail = None
    run(eng, env.clock, 6)                               # next backoff retry succeeds
    first_seen = MONDAY_0800 + timedelta(seconds=15)
    assert env.client.writes == [("in", first_seen)] and eng.pending is None


def test_stale_offline_action_requires_confirmation(env):
    eng = env.make()
    env.client.fail = OdooNetworkError("offline")
    run(eng, env.clock, 3)
    run(eng, env.clock, 30)                              # offline for a long time
    env.client.fail = None
    run(eng, env.clock, 16)
    assert env.client.writes == []
    assert len(env.questions) == 1
    answer(eng, env, True)
    assert env.client.writes == [("in", MONDAY_0800 + timedelta(seconds=15))]  # original time


def test_backoff_limits_api_calls_while_offline(env):
    eng = env.make(auto_checkin=False)
    env.client.fail = OdooNetworkError("offline")
    run(eng, env.clock, 30)
    assert env.client.reads <= 8                         # 30s, 60s, 120s, ... not every tick


def test_auth_failure_stops_retrying_until_settings_change(env):
    eng = env.make()
    env.client.fail = OdooAuthError("bad password")
    run(eng, env.clock, 30)
    assert env.client.reads == 1
    assert eng.view().status == Status.ERROR
    assert sum("login failed" in t.lower() for t, _ in env.notes) == 1
    env.client.fail = None
    eng.command("reload", env.cfg)
    run(eng, env.clock, 3)
    assert env.client.checked_in


def test_failed_attendance_operation_is_reported_not_repeated(env):
    eng = env.make()
    env.client.fail_writes = OdooError("constraint failed")
    run(eng, env.clock, 20)
    assert sum(t == "Attendance not recorded" for t, _ in env.notes) == 1
    assert not env.client.checked_in


def test_periodic_sync_picks_up_changes_made_in_odoo(env):
    eng = env.make(auto_checkin=False)
    eng.tick()
    env.client.checked_in, env.client.check_in_at = True, env.clock.t
    run(eng, env.clock, 6)
    assert eng.view().status == Status.CHECKED_IN


def test_restart_reconciles_with_odoo_and_restores_pending(env):
    eng = env.make()
    env.client.fail = OdooNetworkError("offline")
    run(eng, env.clock, 3)
    eng._save_state()
    env.client.fail = None
    env.client.checked_in, env.client.check_in_at = True, env.clock.t   # done elsewhere meanwhile
    eng2 = env.make()
    assert eng2.pending is not None
    eng2.tick()
    assert eng2.pending is None and env.client.writes == []               # nothing duplicated
    assert eng2.view().status == Status.CHECKED_IN


def test_pause_blocks_automation_but_not_manual(env):
    eng = env.make()
    eng.command("pause", True)
    run(eng, env.clock, 5)
    assert env.client.writes == []
    eng.command("check_in")
    eng.tick()
    assert env.client.checked_in


def at_finish(env, **cfg):
    env.clock.t = MONDAY_0800.replace(hour=9)
    eng = env.make(finish_time="17:00", **cfg)
    run(eng, env.clock, 3)
    assert env.client.checked_in
    env.clock.t = MONDAY_0800.replace(hour=16, minute=59)
    run(eng, env.clock, 2)
    return eng


def test_finish_time_asks_never_checks_out_by_itself(env):
    eng = at_finish(env)
    assert len(env.questions) == 1 and "17:00" in env.questions[0][1]
    run(eng, env.clock, 60)
    assert env.client.checked_in and len(env.questions) == 1   # asked once, no record


def test_finish_question_yes_checks_out_now(env):
    eng = at_finish(env)
    answer(eng, env, True)
    assert not env.client.checked_in


def test_finish_question_answered_late_does_not_record(env):
    eng = at_finish(env)
    env.clock.advance(hours=2)                                  # answered after a long absence
    answer(eng, env, True)
    assert env.client.checked_in
    assert any(t == "Not checked out" for t, _ in env.notes)


def test_finish_question_can_be_disabled(env):
    at_finish(env, ask_checkout_at_finish=False)
    assert env.questions == []


def test_config_file_change_reloads(env, tmp_path):
    from app import config as cfgmod
    import main
    path = tmp_path / "config.json"
    cfgmod.save(env.cfg, path)
    watch = main.config_watcher(path)
    assert watch() is None
    import os
    st = path.stat()
    cfgmod.save(Config(url="https://y.odoo.com", login="me", start_time="07:00"), path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    new = watch()
    assert new is not None and new.start_time == "07:00"
    assert watch() is None


def power(eng, kind="sleep"):
    import threading
    done = threading.Event()
    eng.command("power", (kind, done))
    eng.tick()
    assert done.is_set()


def test_sleep_at_office_after_finish_checks_out_instantly(env):
    eng = at_finish(env, checkout_on_sleep_after_finish=True, ask_checkout_at_finish=False)
    env.clock.t = MONDAY_0800.replace(hour=17, minute=20)
    eng.tick()
    power(eng, "sleep")
    assert not env.client.checked_in
    assert env.client.writes[-1] == ("out", MONDAY_0800.replace(hour=17, minute=20))


def test_shutdown_after_finish_checks_out(env):
    eng = at_finish(env, checkout_on_sleep_after_finish=True, ask_checkout_at_finish=False)
    power(eng, "shutdown")
    assert not env.client.checked_in


def test_sleep_before_finish_keeps_checked_in(env):
    eng = env.make(checkout_on_sleep_after_finish=True)
    run(eng, env.clock, 3)
    power(eng, "sleep")                                         # lunch: lid closed at 08:03
    assert env.client.checked_in


def test_sleep_after_finish_away_from_office_does_nothing(env):
    eng = at_finish(env, checkout_on_sleep_after_finish=True, ask_checkout_at_finish=False)
    env.presence.nets = {"HomeWiFi"}
    run(eng, env.clock, 10)
    power(eng, "sleep")
    assert env.client.checked_in


def test_sleep_checkout_is_opt_in(env):
    eng = at_finish(env, ask_checkout_at_finish=False)
    power(eng, "sleep")
    assert env.client.checked_in


def test_days_off_go_dormant_after_one_sync(env):
    env.clock.t = MONDAY_0800 + timedelta(days=5)               # Saturday
    eng = env.make()
    eng.tick()
    reads = env.client.reads
    assert eng.dormant and "day off" in eng.view().status_text
    for _ in range(12):                                         # a day of 10-minute ticks
        env.clock.advance(minutes=10)
        eng.tick()
    assert env.client.reads == reads and env.client.writes == []
    eng.command("check_in")                                     # weekend work still possible
    eng.tick()
    assert env.client.checked_in and not eng.dormant


def test_dormant_ends_on_workday(env):
    env.clock.t = MONDAY_0800 + timedelta(days=6, hours=-1)     # Sunday 07:00
    eng = env.make()
    eng.tick()
    assert eng.dormant
    env.clock.t = MONDAY_0800 + timedelta(days=7)               # Monday 08:00
    run(eng, env.clock, 3)
    assert not eng.dormant and env.client.checked_in


def test_not_configured_shows_error(env, tmp_path):
    eng = AttendanceEngine(Config(), lambda c: None, Presence(), tmp_path / "s.json")
    eng.tick()
    assert eng.view().status == Status.ERROR and "Settings" in eng.view().status_text
