"""System tray icon and menu (pystray)."""
from __future__ import annotations

import logging

import pystray
from pystray import Menu, MenuItem as Item

from .attendance import AttendanceEngine, View
from .icons import make_icon

log = logging.getLogger(__name__)


class TrayApp:
    def __init__(self, engine: AttendanceEngine, notifier, open_settings, open_logs):
        self.engine = engine
        self._open_settings = open_settings
        self._open_logs = open_logs
        self._images = {}
        self._view: View = engine.view()
        self.icon = pystray.Icon("OdooAutoAttendance", self._image(self._view.status.value),
                                 self._view.tooltip, menu=self._menu())
        notifier.icon = self.icon
        engine.on_change = self._on_change

    def _image(self, status: str):
        if status not in self._images:
            self._images[status] = make_icon(status)
        return self._images[status]

    def _menu(self) -> Menu:
        e = self.engine
        return Menu(
            Item(lambda _: self._view.status_text, None, enabled=False),
            Item(lambda _: self._view.checkin_text, None, enabled=False),
            Menu.SEPARATOR,
            Item("Check In Now", lambda: e.command("check_in")),
            Item("Check Out Now", lambda: e.command("check_out")),
            Item("Refresh Status", lambda: e.command("refresh"), default=True),
            Item("Pause Automation", lambda: e.command("pause", not self._view.paused),
                 checked=lambda _: self._view.paused),
            Menu.SEPARATOR,
            Item("Settings…", lambda: self._open_settings()),
            Item("View Logs", lambda: self._open_logs()),
            Item("Exit", self._exit),
        )

    def _on_change(self, view: View) -> None:
        self._view = view
        try:
            self.icon.icon = self._image(view.status.value)
            self.icon.title = view.tooltip
            self.icon.update_menu()
        except Exception:
            log.exception("Tray update failed")

    def _exit(self) -> None:
        log.info("Exit requested")
        self.engine.stop()
        self.icon.stop()

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            self.engine.start()
        self.icon.run(setup=setup)
