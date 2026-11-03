"""OS helpers (Windows + macOS): single instance, start at login, open files, logging."""
from __future__ import annotations

import logging
import logging.handlers
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from .config import APP_NAME, data_dir

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LAUNCH_AGENT_ID = "com.odooautoattendance.app"
_held = []   # keep locks alive for the process lifetime


def acquire_single_instance(name: str = "SingleInstance"):
    """Return a lock token, or None if another process already holds it."""
    if IS_WIN:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}.{name}")
        if handle:
            _held.append(handle)
        if not handle or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    import fcntl
    path = data_dir() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    _held.append(fh)
    return fh


def launch_args(*extra: str) -> list[str]:
    """Command line that starts this app (frozen exe/app or the source checkout)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    exe = Path(sys.executable)
    if IS_WIN and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    main = Path(__file__).resolve().parent.parent / "main.py"
    return [str(exe), str(main), *extra]


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_AGENT_ID}.plist"


def set_start_at_login(enabled: bool) -> None:
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = " ".join(f'"{a}"' for a in launch_args())
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    elif IS_MAC:
        path = _launch_agent_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as fh:
                plistlib.dump({"Label": _LAUNCH_AGENT_ID, "ProgramArguments": launch_args(),
                               "RunAtLoad": True, "ProcessType": "Interactive"}, fh)
        else:
            path.unlink(missing_ok=True)
    else:
        log.warning("Start at login is not supported on this OS")


def launch_detached(*extra: str) -> None:
    # PYINSTALLER_RESET_ENVIRONMENT: start an independent instance of a frozen build.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen(launch_args(*extra), close_fds=True, env=env)


def open_path(path: Path) -> None:
    if IS_WIN:
        os.startfile(path)
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class RedactFilter(logging.Filter):
    """Last line of defence: scrub anything that looks like a credential."""
    _patterns = [
        (re.compile(r"(?i)(bearer\s+)[^\s'\"]+"), r"\1***"),
        (re.compile(r"(?i)(session_id=)[^;\s'\"]+"), r"\1***"),
        (re.compile(r"(?i)(['\"]?(password|api_?key|secret|token)['\"]?\s*[:=]\s*)['\"]?[^,'\"}\s]+"), r"\1***"),
    ]

    def __init__(self):
        super().__init__()
        self.secrets: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat, repl in self._patterns:
            msg = pat.sub(repl, msg)
        for s in self.secrets:
            if s and len(s) >= 4:
                msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


REDACT = RedactFilter()


def setup_logging(path: Path, level=logging.INFO) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=512_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(REDACT)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
