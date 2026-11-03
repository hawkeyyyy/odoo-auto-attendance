import logging
import sys

import pytest

from app import config as cfgmod
from app.presence import match_trusted, parse_ssids
from app.presence import parse_arp_mac, parse_route_gateway, parse_wifi_devices
from app.system import RedactFilter

MAC_ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
"""
MAC_ARP = "? (192.168.1.1) at 02:00:5e:10:20:30 on en0 ifscope [ethernet]\n"
MAC_ARP_SHORT = "? (10.0.0.1) at 0:1a:2b:3c:4d:5 on en0 ifscope [ethernet]\n"
MAC_PORTS = """
Hardware Port: Ethernet
Device: en1
Ethernet Address: aa:bb:cc:dd:ee:ff

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 11:22:33:44:55:66
"""
MAC_SUMMARY = """<dictionary> {
  BSSID : aa:bb:cc:dd:ee:ff
  InterfaceType : WiFi
  SSID : Office-WiFi-5G
  Security : WPA2_PSK
}"""


def test_macos_gateway_parsing_matches_windows_id_format():
    assert parse_route_gateway(MAC_ROUTE) == "192.168.1.1"
    assert parse_arp_mac(MAC_ARP) == "02-00-5e-10-20-30"        # same as Windows gw: ID
    assert parse_arp_mac(MAC_ARP_SHORT) == "00-1a-2b-3c-4d-05"   # macOS drops leading zeros
    assert parse_arp_mac("? (10.0.0.1) at (incomplete) on en0") is None
    assert parse_route_gateway("route: writing to routing socket: not in table") is None


def test_macos_wifi_parsing():
    assert parse_wifi_devices(MAC_PORTS) == ["en0"]
    assert parse_ssids(MAC_SUMMARY) == ["Office-WiFi-5G"]
    assert parse_ssids("  SSID : <redacted>\n") == []        # macOS without Location access

NETSH = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : Office WiFi 5G
    AP BSSID               : aa:bb:cc:dd:ee:ff
    BSSID                  : aa:bb:cc:dd:ee:ff
    Network type           : Infrastructure
"""


def test_parse_ssids_ignores_bssid():
    assert parse_ssids(NETSH) == ["Office WiFi 5G"]
    assert parse_ssids("") == []


def test_match_trusted():
    ids = frozenset({"Office WiFi 5G", "gw:00-11-22-33-44-55"})
    assert match_trusted(ids, []) is None
    assert match_trusted(ids, ["office wifi 5g"]) is True
    assert match_trusted(ids, ["GW:00-11-22-33-44-55"]) is True
    assert match_trusted(frozenset(), ["Office"]) is False


def test_config_roundtrip_and_validation(tmp_path):
    p = tmp_path / "c.json"
    c = cfgmod.Config(url="https://a.odoo.com", login="me", trusted_networks=["X"])
    cfgmod.save(c, p)
    loaded = cfgmod.load(p)
    assert loaded == c
    assert "password" not in p.read_text().lower()
    bad = cfgmod.Config(start_time="18:00", finish_time="09:00", url="http://insecure.example.com")
    assert len(bad.validate()) == 2


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert cfgmod.load(p) == cfgmod.Config()


def test_redaction():
    f = RedactFilter()
    f.secrets.add("hunter22")
    rec = logging.LogRecord("x", logging.INFO, "", 0,
                            "Authorization: bearer abc123 session_id=xyz; password=hunter22 %s", ("hunter22",), None)
    f.filter(rec)
    msg = rec.getMessage()
    assert "abc123" not in msg and "xyz" not in msg and "hunter22" not in msg


def test_platform_paths_and_launch_agent(monkeypatch, tmp_path):
    from app import system
    monkeypatch.setattr(cfgmod.sys, "platform", "darwin")
    monkeypatch.setattr(cfgmod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cfgmod, "_MAC_SUPPORT", tmp_path / "Library" / "Application Support")
    assert cfgmod.config_path() == tmp_path / "Library/Application Support/OdooAutoAttendance/config.json"
    assert cfgmod.log_path() == tmp_path / "Library/Logs/OdooAutoAttendance/app.log"
    assert cfgmod.secret_store_name() == "macOS Keychain"

    import plistlib
    monkeypatch.setattr(system, "IS_WIN", False)
    monkeypatch.setattr(system, "IS_MAC", True)
    monkeypatch.setattr(system.Path, "home", lambda: tmp_path)
    system.set_start_at_login(True)
    agent = tmp_path / "Library/LaunchAgents/com.odooautoattendance.app.plist"
    data = plistlib.loads(agent.read_bytes())
    assert data["RunAtLoad"] is True and data["ProgramArguments"][-1].endswith("main.py")
    system.set_start_at_login(False)
    assert not agent.exists()


def test_single_instance_lock():
    from app.system import acquire_single_instance
    first = acquire_single_instance("TestLock")
    assert first is not None
    assert acquire_single_instance("TestLock") is None


def test_presence_smoke_on_this_os():
    from app.presence import PlatformPresence
    snap = PlatformPresence().snapshot()
    assert isinstance(snap.locked, bool) and snap.idle_seconds >= 0
