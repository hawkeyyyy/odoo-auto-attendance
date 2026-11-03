"""Minimal tkinter Settings window.

Runs in its own short-lived process (``OdooAutoAttendance.exe --settings``) on that process's
main thread: Tk must never be touched from other threads. The tray process notices the saved
config file and reloads it.
"""
from __future__ import annotations

import copy
import logging
import queue
import threading
import tkinter as tk
from tkinter import ttk

from . import config as cfgmod
from .odoo_client import OdooClient, OdooError
from .presence import NETWORK_HINT, current_network_ids

log = logging.getLogger(__name__)
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def save_settings(new_cfg: cfgmod.Config, secret: str | None) -> None:
    from .system import set_start_at_login
    old = cfgmod.load()
    if secret:
        cfgmod.set_secret(new_cfg, secret)
        if old.url and old.login and (old.url, old.login) != (new_cfg.url, new_cfg.login):
            cfgmod.delete_secret(old)
    if new_cfg.start_with_windows != old.start_with_windows or new_cfg.start_with_windows:
        set_start_at_login(new_cfg.start_with_windows)   # also refreshes the exe path
    cfgmod.save(new_cfg)                                       # tray reloads on file change
    log.info("Settings saved")


def run_settings() -> None:
    SettingsWindow(cfgmod.load(), save_settings).run()


class SettingsWindow:
    def __init__(self, cfg: cfgmod.Config, on_saved):
        self.cfg, self.on_saved = cfg, on_saved
        self.root = tk.Tk()
        self.root.title("Odoo Auto Attendance — Settings")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.v: dict[str, tk.Variable] = {}
        self._results: queue.Queue = queue.Queue()   # worker threads -> Tk thread
        self._build()

    def run(self):
        self.root.after(200, self._poll)
        self.root.mainloop()

    def _poll(self):
        try:
            while True:
                self.test_msg.set(self._results.get_nowait())
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    # ---- layout -----------------------------------------------------------
    def _entry(self, parent, row, label, key, value, width=40, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=2)
        var = tk.StringVar(value=str(value))
        ttk.Entry(parent, textvariable=var, width=width, show=show).grid(
            row=row, column=1, columnspan=3, sticky="we", padx=6, pady=2)
        self.v[key] = var
        return var

    def _check(self, parent, row, label, key, value, col=0):
        var = tk.BooleanVar(value=value)
        ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=col, columnspan=4,
                                                               sticky="w", padx=6, pady=1)
        self.v[key] = var

    def _build(self):
        c = self.cfg
        pad = {"padx": 8, "pady": 4, "sticky": "we"}
        has_secret = bool(cfgmod.get_secret(c)) if c.url and c.login else False

        conn = ttk.LabelFrame(self.root, text="Odoo account")
        conn.grid(row=0, column=0, **pad)
        self._entry(conn, 0, "URL", "url", c.url)
        self._entry(conn, 1, "Database", "database", c.database)
        self._entry(conn, 2, "Login (email)", "login", c.login)
        self._entry(conn, 3, "Password / API key", "secret", "", show="•")
        store = cfgmod.secret_store_name()
        ttk.Label(conn, text=(f"Stored in {store} — leave blank to keep" if has_secret else f"Stored in {store}"),
                  foreground="gray").grid(row=4, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Label(conn, text="Method").grid(row=5, column=0, sticky="w", padx=6)
        self.v["transport"] = tk.StringVar(value=c.transport)
        ttk.Combobox(conn, textvariable=self.v["transport"], values=cfgmod.TRANSPORTS,
                     state="readonly", width=10).grid(row=5, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(conn, text="Employee ID").grid(row=5, column=2, sticky="e")
        self.v["employee_id"] = tk.StringVar(value=str(c.employee_id or ""))
        ttk.Entry(conn, textvariable=self.v["employee_id"], width=8).grid(row=5, column=3, sticky="w", padx=6)
        ttk.Button(conn, text="Test connection", command=self._test).grid(row=6, column=0, padx=6, pady=4)
        self.test_msg = tk.StringVar(value="Read-only test: never changes attendance.")
        ttk.Label(conn, textvariable=self.test_msg, wraplength=330, foreground="gray").grid(
            row=6, column=1, columnspan=3, sticky="w", padx=6)

        sch = ttk.LabelFrame(self.root, text="Work schedule")
        sch.grid(row=1, column=0, **pad)
        self.days = []
        for i, name in enumerate(_DAYS):
            var = tk.BooleanVar(value=i in c.workdays)
            ttk.Checkbutton(sch, text=name, variable=var).grid(row=0, column=i, padx=2)
            self.days.append(var)
        row = ttk.Frame(sch)
        row.grid(row=1, column=0, columnspan=7, sticky="w", padx=4, pady=2)
        for j, (label, key, val) in enumerate((("Start", "start_time", c.start_time),
                                               ("Finish", "finish_time", c.finish_time))):
            ttk.Label(row, text=label).grid(row=0, column=2 * j, padx=4)
            self.v[key] = tk.StringVar(value=val)
            ttk.Entry(row, textvariable=self.v[key], width=6).grid(row=0, column=2 * j + 1, padx=4)

        net = ttk.LabelFrame(self.root, text="Office network (SSID or gw:<gateway MAC>, comma-separated)")
        net.grid(row=2, column=0, **pad)
        self.v["trusted_networks"] = tk.StringVar(value=", ".join(c.trusted_networks))
        ttk.Entry(net, textvariable=self.v["trusted_networks"], width=46).grid(row=0, column=0, padx=6, pady=2)
        ttk.Button(net, text="Add current", command=self._detect_network).grid(row=0, column=1, padx=6)
        self.net_msg = tk.StringVar(value="")
        ttk.Label(net, textvariable=self.net_msg, foreground="gray", wraplength=400).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6)

        auto = ttk.LabelFrame(self.root, text="Automation")
        auto.grid(row=3, column=0, **pad)
        self._check(auto, 0, "Automatic check-in", "auto_checkin", c.auto_checkin)
        self._check(auto, 1, "…only on the office network", "checkin_requires_office", c.checkin_requires_office)
        self._check(auto, 2, "Automatic check-out when I leave the office network", "auto_checkout", c.auto_checkout)
        self._check(auto, 3, "Ask before checking out", "confirm_checkout", c.confirm_checkout)
        self._check(auto, 4, "At finish time, ask whether to check out", "ask_checkout_at_finish",
                    c.ask_checkout_at_finish)
        self._check(auto, 9, "Check out when the PC sleeps or shuts down at the office after finish time",
                    "checkout_on_sleep_after_finish", c.checkout_on_sleep_after_finish)
        self._check(auto, 10, "Sleep on days off (no Odoo calls on non-workdays)", "dormant_on_days_off",
                    c.dormant_on_days_off)
        nums = ttk.Frame(auto)
        nums.grid(row=5, column=0, columnspan=4, sticky="w", padx=4)
        for j, (label, key, val) in enumerate((("Check-in delay (min)", "checkin_delay_min", c.checkin_delay_min),
                                               ("Check-out grace (min)", "checkout_grace_min", c.checkout_grace_min))):
            ttk.Label(nums, text=label).grid(row=0, column=2 * j, padx=4)
            self.v[key] = tk.StringVar(value=f"{val:g}")
            ttk.Entry(nums, textvariable=self.v[key], width=5).grid(row=0, column=2 * j + 1, padx=4)
        self._check(auto, 6, "Notify on check-in/out", "notify_actions", c.notify_actions)
        self._check(auto, 7, "Reminders (remote day, still checked in late)", "notify_reminders", c.notify_reminders)
        self._check(auto, 8, "Start at login", "start_with_windows", c.start_with_windows)

        bar = ttk.Frame(self.root)
        bar.grid(row=4, column=0, sticky="e", padx=8, pady=8)
        self.err = tk.StringVar()
        ttk.Label(bar, textvariable=self.err, foreground="red", wraplength=300).grid(row=0, column=0, padx=8)
        ttk.Button(bar, text="Cancel", command=self.root.destroy).grid(row=0, column=1, padx=4)
        ttk.Button(bar, text="Save", command=self._save).grid(row=0, column=2, padx=4)

    # ---- actions ----------------------------------------------------------
    def _collect(self) -> cfgmod.Config:
        c = copy.deepcopy(self.cfg)
        g = lambda k: self.v[k].get()
        c.url, c.database, c.login = g("url").strip(), g("database").strip(), g("login").strip()
        c.transport = g("transport")
        emp = g("employee_id").strip()
        c.employee_id = int(emp) if emp.isdigit() else 0
        c.workdays = [i for i, var in enumerate(self.days) if var.get()]
        c.start_time, c.finish_time = g("start_time").strip(), g("finish_time").strip()
        c.trusted_networks = [s.strip() for s in g("trusted_networks").split(",") if s.strip()]
        for key in ("auto_checkin", "checkin_requires_office", "auto_checkout", "confirm_checkout",
                    "ask_checkout_at_finish", "checkout_on_sleep_after_finish", "dormant_on_days_off", "notify_actions", "notify_reminders", "start_with_windows"):
            setattr(c, key, bool(g(key)))
        for key in ("checkin_delay_min", "checkout_grace_min"):
            try:
                setattr(c, key, float(g(key)))
            except ValueError:
                setattr(c, key, -1.0)
        if c.url:
            try:
                c.url = cfgmod.normalize_url(c.url)
            except ValueError:
                pass
        return c

    def _secret_for(self, c: cfgmod.Config) -> str | None:
        return self.v["secret"].get() or cfgmod.get_secret(c)

    def _test(self):
        c = self._collect()
        errors = c.validate()
        secret = self._secret_for(c)
        if errors or not (c.url and c.login and secret):
            self.test_msg.set("; ".join(errors) or "Enter URL, login and password / API key first.")
            return
        self.test_msg.set("Testing…")

        def work():
            try:
                client = OdooClient(c.url, c.database, c.login, secret, c.transport, c.employee_id)
                try:
                    desc = client.connect()
                    st = client.get_status()
                    msg = f"OK — {desc}. Currently {'checked in' if st.checked_in else 'checked out'}."
                    if client.database and not c.database:
                        msg += f" (database '{client.database}')"
                finally:
                    client.close()
            except (OdooError, ValueError) as exc:
                msg = f"Failed — {exc}"
            except Exception as exc:
                log.exception("Connection test crashed")
                msg = f"Failed — {exc.__class__.__name__}"
            self._results.put(msg[:600])

        threading.Thread(target=work, daemon=True).start()

    def _detect_network(self):
        ids = sorted(current_network_ids())
        if not ids:
            self.net_msg.set(f"No network detected. {NETWORK_HINT}")
            return
        current = [s.strip() for s in self.v["trusted_networks"].get().split(",") if s.strip()]
        merged = current + [i for i in ids if i not in current]
        self.v["trusted_networks"].set(", ".join(merged))
        self.net_msg.set("Current: " + ", ".join(ids))

    def _save(self):
        c = self._collect()
        errors = c.validate()
        if errors:
            self.err.set(" ".join(errors))
            return
        secret = self.v["secret"].get() or None
        if c.url and c.login and not (secret or cfgmod.get_secret(c)):
            self.err.set("Enter your password or API key.")
            return
        try:
            self.on_saved(c, secret)
        except Exception as exc:
            log.exception("Saving settings failed")
            self.err.set(f"Could not save: {exc}")
            return
        self.root.destroy()
