"""Notifications and native Yes/No confirmations (Windows: tray toast + MessageBox,
macOS: osascript). Text is passed to AppleScript as argv, never interpolated into code."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"
_TITLE = "Odoo Attendance"

# Windows MessageBox flags
_MB_YESNO, _MB_ICONQUESTION, _MB_TOPMOST, _MB_SETFOREGROUND = 0x4, 0x20, 0x40000, 0x10000
_IDYES = 6


def _osascript(lines: list[str], *argv: str, timeout: float | None = None) -> str:
    cmd = ["/usr/bin/osascript"]
    for line in ["on run argv", *lines, "end run"]:
        cmd += ["-e", line]
    out = subprocess.run(cmd + list(argv), capture_output=True, text=True, timeout=timeout)
    return out.stdout.strip()


class Notifier:
    def __init__(self):
        self.icon = None   # pystray.Icon, attached by the tray (used on Windows)

    def notify(self, title: str, message: str) -> None:
        log.info("Notification: %s — %s", title, message)
        try:
            if IS_MAC:
                _osascript(["display notification (item 2 of argv) with title (item 1 of argv)"],
                           title[:60], message[:250], timeout=10)
            elif self.icon is not None:
                self.icon.notify(message[:250], title[:60])
        except Exception as exc:
            log.warning("Notification failed: %s", exc)


def confirm(title: str, message: str, callback) -> None:
    """Show a Yes/No question without blocking the caller; callback(bool) on answer."""
    def run():
        yes = False
        try:
            if IS_MAC:
                out = _osascript([
                    'display dialog (item 2 of argv) with title (item 1 of argv) '
                    'buttons {"No", "Yes"} default button "Yes" with icon note'],
                    f"{_TITLE} — {title}", message)
                yes = out.endswith("Yes")
            else:
                import ctypes
                flags = _MB_YESNO | _MB_ICONQUESTION | _MB_TOPMOST | _MB_SETFOREGROUND
                yes = ctypes.windll.user32.MessageBoxW(None, message, f"{_TITLE} — {title}", flags) == _IDYES
        except Exception:
            log.exception("Confirmation dialog failed")
        callback(yes)

    threading.Thread(target=run, name="confirm", daemon=True).start()


def message(title: str, text: str) -> None:
    try:
        if IS_MAC:
            _osascript(["display alert (item 1 of argv) message (item 2 of argv)"], title, text, timeout=60)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | _MB_TOPMOST)
    except Exception:
        pass
