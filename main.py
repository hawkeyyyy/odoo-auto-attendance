"""Odoo Auto Attendance — tiny tray / menu-bar utility for Windows and macOS.

    OdooAutoAttendance              tray app (single instance; pystray on the main thread)
    OdooAutoAttendance --settings   settings window (separate process, own main thread)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import psutil

from app import config as cfgmod
from app.system import REDACT, acquire_single_instance, launch_detached, open_path, setup_logging

log = logging.getLogger("main")


def make_client(cfg: cfgmod.Config):
    from app.odoo_client import OdooClient
    if not (cfg.url and cfg.login):
        return None
    secret = cfgmod.get_secret(cfg)
    if not secret:
        return None
    REDACT.secrets.add(secret)
    return OdooClient(cfg.url, cfg.database, cfg.login, secret, cfg.transport, cfg.employee_id)


def config_watcher(path: Path):
    """Return a callable giving a fresh Config when the file changed since the last call."""
    def mtime():
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None
    last = [mtime()]

    def check():
        current = mtime()
        if current == last[0]:
            return None
        last[0] = current
        log.info("Config file changed; reloading")
        return cfgmod.load(path)
    return check


def launch_settings() -> None:
    launch_detached("--settings")


def run_settings_process() -> int:
    from app.notifications import message
    if acquire_single_instance("Settings") is None:
        return 0  # already open
    try:
        from app.settings import run_settings
        run_settings()
    except Exception:
        log.exception("Settings window failed")
        message("Odoo Auto Attendance", "The Settings window failed to open. See the log for details.")
        return 1
    return 0


def run_tray() -> int:
    from app.attendance import AttendanceEngine
    from app.notifications import Notifier, confirm, message
    from app.presence import PlatformPresence

    if acquire_single_instance() is None:
        message("Odoo Auto Attendance", "Odoo Auto Attendance is already running (see the tray).")
        return 0
    log.info("Starting (pid %s)", os.getpid())

    cfg = cfgmod.load()
    fresh_login = time.time() - psutil.boot_time() < 600
    notifier = Notifier()
    engine = AttendanceEngine(cfg, make_client, PlatformPresence(), cfgmod.state_path(),
                              notify=notifier.notify, confirm=confirm, fresh_login=fresh_login,
                              reload_config=config_watcher(cfgmod.config_path()))

    def open_logs():
        path = cfgmod.log_path()
        if path.exists():
            open_path(path)

    from app.power import start_power_listener
    start_power_listener(engine.on_power_event)

    from app.tray import TrayApp
    tray = TrayApp(engine, notifier, launch_settings, open_logs)
    if not (cfg.url and cfg.login):
        launch_settings()
    tray.run()
    log.info("Stopped")
    return 0


def main() -> int:
    setup_logging(cfgmod.log_path())
    if "--settings" in sys.argv[1:]:
        return run_settings_process()
    return run_tray()


if __name__ == "__main__":
    sys.exit(main())
