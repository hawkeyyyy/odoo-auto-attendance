"""Configuration (JSON file per user) and credential storage (Windows Credential Manager /
macOS Keychain via ``keyring``)."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

APP_NAME = "OdooAutoAttendance"
KEYRING_SERVICE = APP_NAME
TRANSPORTS = ("auto", "json2", "jsonrpc", "web")


_MAC_SUPPORT = Path.home() / "Library" / "Application Support"


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    if sys.platform == "darwin":
        return _MAC_SUPPORT / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME


def data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
    if sys.platform == "darwin":
        return _MAC_SUPPORT / APP_NAME
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def state_path() -> Path:
    return data_dir() / "state.json"


def log_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME / "app.log"
    return data_dir() / "logs" / "app.log"


def secret_store_name() -> str:
    return {"win32": "Windows Credential Manager", "darwin": "macOS Keychain"}.get(
        sys.platform, "the system keyring")


_HM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class Config:
    # Connection (the secret itself is never stored here)
    url: str = ""
    database: str = ""
    login: str = ""
    transport: str = "auto"          # auto | json2 | jsonrpc | web
    employee_id: int = 0             # 0 = detect from the logged-in user

    # Schedule: guides *when* detection is active, never creates records by itself
    workdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday = 0
    start_time: str = "09:00"
    finish_time: str = "18:00"
    earliest_before_start_min: int = 120

    # Office network identifiers: Wi-Fi SSIDs and/or "gw:<gateway MAC>"
    trusted_networks: list[str] = field(default_factory=list)
    checkin_requires_office: bool = True

    # Automation
    auto_checkin: bool = True
    auto_checkout: bool = True       # only acts on confirmed departure from the office network
    confirm_checkout: bool = True
    ask_checkout_at_finish: bool = True   # a question at finish time, never an automatic record
    # Check out the moment the PC sleeps / shuts down at the office after finish time
    # (while Odoo is still reachable, so the time is exact).
    checkout_on_sleep_after_finish: bool = False
    dormant_on_days_off: bool = True      # non-workdays: one sync, then no polling
    checkin_delay_min: float = 2
    checkout_grace_min: float = 15

    # Notifications / system
    notify_actions: bool = True
    notify_reminders: bool = True
    start_with_windows: bool = False   # "start at login" (name kept for existing config files)
    sync_interval_min: float = 5
    poll_seconds: float = 15

    def validate(self) -> list[str]:
        errors = []
        if self.url:
            try:
                normalize_url(self.url)
            except ValueError as exc:
                errors.append(str(exc))
        if self.transport not in TRANSPORTS:
            errors.append(f"Unknown connection method '{self.transport}'.")
        times_ok = True
        for label, value in (("Start time", self.start_time), ("Finish time", self.finish_time)):
            if not _HM.match(value or ""):
                errors.append(f"{label} must be HH:MM.")
                times_ok = False
        if times_ok and parse_hm(self.start_time) >= parse_hm(self.finish_time):
            errors.append("Finish time must be after start time.")
        if any(d not in range(7) for d in self.workdays):
            errors.append("Invalid workday.")
        if self.checkin_delay_min < 0 or self.checkout_grace_min < 1:
            errors.append("Delays must be positive (grace period at least 1 minute).")
        return errors


def parse_hm(value: str) -> tuple[int, int]:
    m = _HM.match(value.strip())
    if not m:
        raise ValueError(f"Invalid time '{value}'")
    return int(m.group(1)), int(m.group(2))


def normalize_url(url: str) -> str:
    """Return scheme://host[:port]. HTTPS is mandatory except for localhost testing."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Odoo URL is empty.")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("Odoo URL is invalid.")
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("Odoo URL must use https://")
    return f"{parsed.scheme}://{parsed.netloc}"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        log.warning("Config unreadable (%s); using defaults", exc)
        return cfg
    known = {f.name: f for f in fields(Config)}
    for key, value in raw.items():
        if key not in known:
            continue
        default = getattr(cfg, key)
        try:
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            elif isinstance(default, list):
                value = list(value)
            elif isinstance(default, str):
                value = str(value)
        except (TypeError, ValueError):
            continue
        setattr(cfg, key, value)
    return cfg


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---- credentials ---------------------------------------------------------

def _keyring():
    import keyring
    if os.name == "nt":
        # Set the backend explicitly so frozen builds don't depend on entry points.
        from keyring.backends.Windows import WinVaultKeyring
        if not isinstance(keyring.get_keyring(), WinVaultKeyring):
            keyring.set_keyring(WinVaultKeyring())
    return keyring


def credential_user(cfg: Config) -> str:
    host = urlparse(normalize_url(cfg.url)).netloc if cfg.url else ""
    return f"{cfg.login}@{host}"


def get_secret(cfg: Config) -> str | None:
    if not (cfg.url and cfg.login):
        return None
    try:
        return _keyring().get_password(KEYRING_SERVICE, credential_user(cfg))
    except Exception as exc:  # keyring errors must not crash the app
        log.error("Credential Manager read failed: %s", exc.__class__.__name__)
        return None


def set_secret(cfg: Config, secret: str) -> None:
    _keyring().set_password(KEYRING_SERVICE, credential_user(cfg), secret)


def delete_secret(cfg: Config) -> None:
    try:
        _keyring().delete_password(KEYRING_SERVICE, credential_user(cfg))
    except Exception:
        pass
