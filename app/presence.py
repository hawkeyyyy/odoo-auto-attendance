"""Presence signals on Windows and macOS: session lock, input idle time, network identity.

Only coarse signals are read: whether the screen is locked, the time since the last input event
(no keystrokes or content), the connected Wi-Fi SSID and the default gateway's MAC address.
The gateway ID ("gw:aa-bb-cc-dd-ee-ff") has the same format on both systems.
"""
from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PresenceSnapshot:
    locked: bool
    idle_seconds: float
    network_ids: frozenset[str]


# ---- parsers (pure, tested on every OS) -----------------------------------

_SSID_RE = re.compile(r"^\s*SSID\s*:\s*(.*?)\s*$", re.MULTILINE)


def parse_ssids(output: str) -> list[str]:
    """SSIDs from `netsh wlan show interfaces` (Windows) or `ipconfig getsummary` (macOS)."""
    return [s for s in _SSID_RE.findall(output or "") if s and s != "<redacted>"]


def parse_route_gateway(output: str) -> str | None:
    """Gateway IP from macOS `route -n get default`."""
    m = re.search(r"^\s*gateway:\s*(\S+)", output or "", re.MULTILINE)
    return m.group(1) if m else None


def parse_arp_mac(output: str) -> str | None:
    """MAC from macOS `arp -n <ip>` ('? (10.0.0.1) at 0:1a:2b:3c:4d:5e on en0 ...')."""
    m = re.search(r"\bat\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\b", output or "")
    return "-".join(p.zfill(2) for p in m.group(1).lower().split(":")) if m else None


def parse_wifi_devices(output: str) -> list[str]:
    """Wi-Fi device names (en0, ...) from `networksetup -listallhardwareports`."""
    return re.findall(r"Hardware Port:\s*(?:Wi-Fi|AirPort)\s*\nDevice:\s*(\S+)", output or "")


def match_trusted(network_ids: frozenset[str], trusted: list[str]) -> bool | None:
    """True/False when trusted networks are configured, None when not configured."""
    wanted = {t.strip().lower() for t in trusted if t.strip()}
    if not wanted:
        return None
    return any(n.lower() in wanted for n in network_ids)


def _run(cmd: list[str], timeout: float = 8) -> str:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("oem" if sys.platform == "win32" else "utf-8", errors="replace")


# ---- Windows ---------------------------------------------------------------

if sys.platform == "win32":
    import socket
    import struct
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _iphlp = ctypes.WinDLL("iphlpapi")

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _MIB_IPFORWARDROW(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in (
            "dest", "mask", "policy", "next_hop", "if_index", "type", "proto", "age",
            "next_hop_as", "m1", "m2", "m3", "m4", "m5")]

    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _iphlp.GetBestRoute.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_MIB_IPFORWARDROW)]
    _iphlp.SendARP.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]

    def is_session_locked() -> bool:
        """Locked (or on the secure desktop) when the input desktop isn't 'Default'."""
        h = _user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not h:
            return True
        try:
            buf = ctypes.create_unicode_buffer(64)
            needed = wintypes.DWORD()
            if not _user32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                return False
            return buf.value.lower() != "default"
        finally:
            _user32.CloseDesktop(h)

    def idle_seconds() -> float:
        lii = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
        if not _user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return ((_kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0

    def gateway_id() -> str | None:
        row = _MIB_IPFORWARDROW()
        dest = struct.unpack("<I", socket.inet_aton("1.1.1.1"))[0]
        if _iphlp.GetBestRoute(dest, 0, ctypes.byref(row)) != 0 or not row.next_hop:
            return None
        mac = (ctypes.c_ubyte * 8)()
        size = wintypes.ULONG(8)
        if _iphlp.SendARP(row.next_hop, 0, mac, ctypes.byref(size)) != 0 or size.value == 0:
            return None
        return "gw:" + "-".join(f"{b:02x}" for b in mac[:size.value])

    def wifi_ssids() -> list[str]:
        return parse_ssids(_run(["netsh", "wlan", "show", "interfaces"]))

    NETWORK_HINT = ("On Windows 11 the Wi-Fi name needs Settings > Privacy > Location > "
                    "'Let desktop apps access your location'; the gw: ID works without it.")

# ---- macOS -----------------------------------------------------------------

elif sys.platform == "darwin":
    _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    _cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
    _cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    _cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cf.CFBooleanGetValue.restype = ctypes.c_bool
    _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _UTF8 = 0x08000100
    _HID_SYSTEM_STATE, _ANY_INPUT = 1, 0xFFFFFFFF

    def is_session_locked() -> bool:
        session = _cg.CGSessionCopyCurrentDictionary()
        if not session:
            return True  # no GUI session (login window / fast user switching)
        try:
            key = _cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", _UTF8)
            try:
                value = _cf.CFDictionaryGetValue(session, key)
                return bool(value) and bool(_cf.CFBooleanGetValue(value))
            finally:
                _cf.CFRelease(key)
        finally:
            _cf.CFRelease(session)

    def idle_seconds() -> float:
        return float(_cg.CGEventSourceSecondsSinceLastEventType(_HID_SYSTEM_STATE, _ANY_INPUT))

    def gateway_id() -> str | None:
        ip = parse_route_gateway(_run(["/sbin/route", "-n", "get", "default"]))
        if not ip:
            return None
        mac = parse_arp_mac(_run(["/usr/sbin/arp", "-n", ip]))
        return f"gw:{mac}" if mac else None

    def wifi_ssids() -> list[str]:
        devices = parse_wifi_devices(_run(["/usr/sbin/networksetup", "-listallhardwareports"])) or ["en0"]
        ssids: list[str] = []
        for dev in devices:
            ssids += parse_ssids(_run(["/usr/sbin/ipconfig", "getsummary", dev]))
        return ssids

    NETWORK_HINT = ("Recent macOS versions hide the Wi-Fi name from apps without Location "
                    "access; the gw: ID works without it.")

# ---- other (development only) ----------------------------------------------

else:  # pragma: no cover
    def is_session_locked() -> bool:
        return False

    def idle_seconds() -> float:
        return 0.0

    def gateway_id() -> str | None:
        return None

    def wifi_ssids() -> list[str]:
        return []

    NETWORK_HINT = "Network detection is not supported on this OS."


def current_network_ids() -> frozenset[str]:
    ids = set(wifi_ssids())
    gw = gateway_id()
    if gw:
        ids.add(gw)
    return frozenset(ids)


class PlatformPresence:
    """Cheap snapshot provider; the network lookup (spawns a process) is cached."""

    def __init__(self, network_refresh_s: float = 60.0):
        self._refresh = network_refresh_s
        self._net: frozenset[str] = frozenset()
        self._net_at = 0.0

    def invalidate_network(self) -> None:
        self._net_at = 0.0

    def snapshot(self) -> PresenceSnapshot:
        now = time.monotonic()
        if not self._net_at or now - self._net_at >= self._refresh:
            try:
                self._net = current_network_ids()
            except Exception as exc:
                log.warning("Network detection failed: %s", exc)
                self._net = frozenset()
            self._net_at = now
        return PresenceSnapshot(is_session_locked(), idle_seconds(), self._net)


WindowsPresence = PlatformPresence  # backwards-compatible name
