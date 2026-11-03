"""Odoo adapter tests against an in-memory fake server (no real Odoo is touched)."""
from datetime import datetime, timedelta

import pytest

from app.odoo_client import (OdooAccessError, OdooAuthError, OdooClient, OdooError,
                             OdooNetworkError, to_odoo)
from tests.fake_odoo import FakeOdoo


def client(fake: FakeOdoo, secret="pw", transport="auto", url="https://odoo.example.com", db="", **kw):
    return OdooClient(url, db, "me@example.com", secret, transport, http_transport=fake.transport(), **kw)


def test_rejects_plain_http():
    with pytest.raises(ValueError):
        OdooClient("http://odoo.example.com", "", "a", "b")


def test_version_and_database_detection():
    fake = FakeOdoo(version=(17, 0))
    c = client(fake)
    info = c.detect_server()
    assert (info.major, info.minor) == (17, 0)
    assert c.detect_database() == "prod"


def test_odoo_online_database_from_subdomain():
    fake = FakeOdoo(dbs=())
    c = client(fake, url="https://mycompany.odoo.com")
    assert c.detect_database() == "mycompany"


def test_v17_regular_employee_uses_web_session_systray():
    fake = FakeOdoo(version=(17, 0), can_write=False)
    c = client(fake)
    c.connect()
    assert c.t.kind == "web" and c.write_mode == "systray"
    assert not c.supports_explicit_time
    status, changed = c.set_checked_in(True)
    assert changed and status.checked_in and fake.writes == 1
    status, changed = c.set_checked_in(False)
    assert changed and not status.checked_in and fake.writes == 2


def test_already_checked_in_is_a_no_op():
    fake = FakeOdoo(version=(17, 0))
    c = client(fake)
    c.set_checked_in(True)
    writes = fake.writes
    status, changed = c.set_checked_in(True)
    assert status.checked_in and not changed and fake.writes == writes


def test_duplicate_requests_never_double_toggle():
    fake = FakeOdoo(version=(17, 0))
    c = client(fake)
    for _ in range(3):
        c.set_checked_in(True)
    assert fake.writes == 1
    assert sum(1 for a in fake.attendances if not a["check_out"]) == 1


def test_officer_uses_direct_write_with_exact_time():
    fake = FakeOdoo(version=(17, 0), can_write=True)
    c = client(fake)
    c.connect()
    assert c.t.kind == "jsonrpc" and c.write_mode == "direct"
    start = datetime.now().astimezone().replace(microsecond=0) - timedelta(minutes=5)
    c.set_checked_in(True, at=start)
    assert fake.attendances[0]["check_in"] == to_odoo(start)
    left = start + timedelta(minutes=2)
    status, _ = c.set_checked_in(False, at=left)
    assert fake.attendances[0]["check_out"] == to_odoo(left) and not status.checked_in


def test_checkout_before_checkin_is_refused():
    fake = FakeOdoo(version=(17, 0), can_write=True)
    c = client(fake)
    now = datetime.now().astimezone()
    c.set_checked_in(True, at=now - timedelta(minutes=1))
    with pytest.raises(OdooError):
        c.set_checked_in(False, at=now - timedelta(hours=1))


def test_v16_uses_attendance_manual_over_external_api():
    fake = FakeOdoo(version=(16, 0), can_write=False)
    c = client(fake)
    c.connect()
    assert c.t.kind == "jsonrpc" and c.write_mode == "manual"
    status, changed = c.set_checked_in(True)
    assert changed and status.checked_in
    assert "hr.employee.attendance_manual" in fake.calls


def test_v19_json2_with_api_key():
    fake = FakeOdoo(version=(19, 0), can_write=True)
    c = client(fake, secret="KEY")
    c.connect()
    assert c.t.kind == "json2" and c.t.uid == 7
    status, changed = c.set_checked_in(True)
    assert changed and status.checked_in


def test_v19_json2_read_only_user_falls_back_to_web_with_password():
    fake = FakeOdoo(version=(19, 0), can_write=False)
    c = client(fake, secret="pw")       # password: JSON-2 rejects it, web session works
    c.connect()
    assert c.t.kind == "web" and c.write_mode == "systray"


def test_api_not_on_plan_falls_back_to_web():
    fake = FakeOdoo(version=(18, 0), api_allowed=False, can_write=True)
    c = client(fake)
    c.connect()
    assert c.t.kind == "web" and c.write_mode == "direct"


def test_authentication_failure():
    fake = FakeOdoo(version=(17, 0))
    with pytest.raises(OdooAuthError):
        client(fake, secret="wrong").connect()


def test_two_factor_web_login_reports_auth_error():
    fake = FakeOdoo(version=(17, 0), api_allowed=False, web_2fa=True)
    with pytest.raises(OdooAuthError, match="two-factor"):
        client(fake).connect()


def test_api_key_only_read_access_explains_how_to_fix():
    fake = FakeOdoo(version=(17, 0), can_write=False)
    with pytest.raises(OdooAccessError, match="web"):
        client(fake, secret="KEY", transport="jsonrpc").connect()


def test_network_failure_raises_network_error():
    fake = FakeOdoo()
    fake.offline = True
    with pytest.raises(OdooNetworkError):
        client(fake).connect()


def test_session_expiry_reauthenticates_transparently():
    fake = FakeOdoo(version=(17, 0))
    c = client(fake)
    c.connect()
    fake.sessions.clear()               # server forgot the session
    status = c.get_status()
    assert status.checked_in is False and len(fake.sessions) == 1


def test_failed_write_is_reported_and_state_unchanged():
    fake = FakeOdoo(version=(17, 0))
    c = client(fake)
    c.connect()
    fake.fail_writes = "odoo.exceptions.UserError"
    with pytest.raises(OdooError):
        c.set_checked_in(True)
    assert c.get_status().checked_in is False


def test_status_reports_todays_first_check_in():
    fake = FakeOdoo(version=(17, 0), can_write=True)
    c = client(fake)
    now = datetime.now().astimezone().replace(microsecond=0)
    first = max(now - timedelta(hours=3), now.replace(hour=0, minute=1, second=0))
    c.set_checked_in(True, at=first)
    c.set_checked_in(False, at=first + timedelta(seconds=30))
    c.set_checked_in(True)
    st = c.get_status()
    assert st.checked_in and st.first_check_in_today == first
