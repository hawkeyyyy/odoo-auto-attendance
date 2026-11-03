"""Sleep / shutdown notifications, so check-out can happen while Odoo is still reachable.

Windows: hidden top-level window receiving WM_POWERBROADCAST (PBT_APMSUSPEND, also for Modern
Standby via RegisterSuspendResumeNotification) and WM_QUERYENDSESSION / WM_ENDSESSION.
macOS: NSWorkspace will-sleep / will-power-off notifications (pyobjc, installed with pystray).

``callback(kind)`` runs synchronously and should return within a few seconds.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)
_keep = []   # prevent garbage collection of callbacks / observers


def start_power_listener(callback: Callable[[str], None]) -> None:
    last = [0.0]

    def fire(kind: str) -> None:
        if time.monotonic() - last[0] < 60:   # QUERYENDSESSION + ENDSESSION -> once
            return
        last[0] = time.monotonic()
        try:
            callback(kind)
        except Exception:
            log.exception("Power event handler failed")

    try:
        if sys.platform == "win32":
            threading.Thread(target=_windows_loop, args=(fire,), name="power", daemon=True).start()
        elif sys.platform == "darwin":
            _macos_observe(fire)
    except Exception:
        log.exception("Power notifications unavailable")


def _windows_loop(fire: Callable[[str], None]) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.RegisterSuspendResumeNotification.restype = wintypes.HANDLE
    user32.RegisterSuspendResumeNotification.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    WM_POWERBROADCAST, PBT_APMSUSPEND = 0x0218, 0x0004
    WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016

    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST and wparam == PBT_APMSUSPEND:
            fire("sleep")
            return 1
        if msg == WM_QUERYENDSESSION:
            fire("shutdown")
            return 1
        if msg == WM_ENDSESSION and wparam:
            fire("shutdown")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(proc)
    _keep.append(wndproc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName="OdooAutoAttendancePower")
    if not user32.RegisterClassW(ctypes.byref(wc)):
        log.error("Power listener: RegisterClassW failed (%s)", ctypes.get_last_error())
        return
    # A hidden *top-level* window: message-only windows don't get session/power broadcasts.
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "OdooAutoAttendancePower", 0,
                                  0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        log.error("Power listener: CreateWindowExW failed (%s)", ctypes.get_last_error())
        return
    user32.RegisterSuspendResumeNotification(hwnd, 0)   # DEVICE_NOTIFY_WINDOW_HANDLE
    log.info("Power listener ready")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _macos_observe(fire: Callable[[str], None]) -> None:
    from AppKit import NSWorkspace
    from Foundation import NSObject

    class _Observer(NSObject):
        def willSleep_(self, _note):
            fire("sleep")

        def willPowerOff_(self, _note):
            fire("shutdown")

    obs = _Observer.alloc().init()
    _keep.append(obs)
    center = NSWorkspace.sharedWorkspace().notificationCenter()
    center.addObserver_selector_name_object_(obs, "willSleep:", "NSWorkspaceWillSleepNotification", None)
    center.addObserver_selector_name_object_(obs, "willPowerOff:", "NSWorkspaceWillPowerOffNotification", None)
    log.info("Power listener ready")
