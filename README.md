# Odoo Auto Attendance

A small tray (Windows) / menu-bar (macOS) app that checks you in and out of **Odoo Attendance** based on whether you are actually at work. It is a personal utility for one user, not a company attendance system.

- **Checks you in** when you unlock your computer at the office and start using it.
- **Checks you out** when you finish: when you close the lid or shut down at the office after work, when you confirm the end-of-day question, or when you click *Check Out Now*.
- **Never invents attendance.** A lock or an idle PC alone never checks you out. When a situation is unclear, the app asks you.
- Every change is read back from Odoo to confirm it. Duplicate check-ins and check-outs are impossible.

| Icon | Meaning |
|---|---|
| 🟢 Green | Checked in |
| ⚪ Gray | Checked out (or a day off) |
| 🟡 Yellow | Waiting: confirming presence, retrying Odoo, or waiting for your answer |
| 🔴 Red | Connection or login problem (hover for details) |

Tray menu: status · today's check-in time · **Check In Now** · **Check Out Now** · Refresh Status · Pause Automation · Settings… · View Logs · Exit

---

## 1. Install

### Windows 10/11

1. Open the **[latest release](../../releases/latest)** and download **`OdooAutoAttendance.exe`** and **`install.ps1`** into the same folder.
2. Right-click in that folder and choose *Open in Terminal*. Then run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   This copies the app to `%LOCALAPPDATA%\Programs\OdooAutoAttendance\`, creates an **Odoo Auto Attendance** desktop shortcut, and starts it.
3. If Windows SmartScreen warns about an unknown publisher, click **More info → Run anyway**. The exe is not code-signed.
4. Settings opens automatically. Continue with [Connect your Odoo account](#2-connect-your-odoo-account).

Manual install, without the script: put `OdooAutoAttendance.exe` in any permanent folder and double-click it. To have it start with Windows, tick **Start at login** in Settings.

### macOS 12+

1. Download **`OdooAutoAttendance-macOS.zip`** from the **[latest release](../../releases/latest)** and unzip it.
2. Drag **OdooAutoAttendance.app** into **Applications**.
3. The first time, **right-click the app → Open → Open**. The app is ad-hoc signed, not notarized, so a normal double-click is blocked once. If macOS still refuses, run `xattr -dr com.apple.quarantine /Applications/OdooAutoAttendance.app`.
4. A clock icon appears in the menu bar (no Dock icon), and Settings opens.
5. When asked, allow Keychain access (**Always Allow**) and notifications for *Script Editor*. Notifications and questions are shown through `osascript`.

### From source (either OS)

```bash
git clone https://github.com/hawkeyyyy/odoo-auto-attendance.git
cd odoo-auto-attendance
python -m pip install -r requirements.txt      # Python 3.12+, with tkinter
python main.py                                 # Windows: pythonw main.py (no console)
```

---

## 2. Connect your Odoo account

Open **Settings** (tray menu → Settings…) and fill in **Odoo account**:

| Field | What to enter |
|---|---|
| URL | `https://yourcompany.odoo.com`. HTTPS is required. |
| Database | Leave empty to auto-detect. On Odoo Online the database is the subdomain. |
| Login | Your Odoo login (email). |
| Password / API key | See below. It is stored in **Windows Credential Manager** or the **macOS Keychain**, never in a file. |
| Method | `auto` (recommended) or `web`, `json2`, `jsonrpc`. |
| Employee ID | Leave empty. Set it only if your user is linked to several employees. |

Then click **Test connection**. It is read-only and never changes your attendance. A good result looks like:

> OK — Odoo 19.0 via Web session; employee 'Jane Doe' (id 26); records with systray check-in button. Currently checked out.

**Password or API key?**

| You are… | Enter | How Odoo records the time |
|---|---|---|
| A normal employee (most people), any plan | Your **password**, method `auto` or `web` | The same check-in/out button as the Odoo web systray. Odoo stores the **current time**. |
| An Attendance Officer or manager, on a Custom plan with API access | An **API key** (Odoo → My Preferences → Account Security → New API Key) | Direct records with the **exact time** of the event. |
| On Odoo 16 or older | Password or API key | The "My Attendances" check-in action. |
| Using two-factor authentication | An API key; this only works with Officer rights or Odoo ≤ 16 | Password login cannot finish with 2FA. |

## 3. Set up presence detection

- **Office network:** at the office, click **Add current**. This adds your Wi-Fi name and a `gw:` ID (your router's hardware address). The `gw:` ID also works on cable and is identical on Windows and macOS. Windows 11 and recent macOS versions only reveal the Wi-Fi name to apps with Location permission, so the `gw:` ID is the reliable choice.
- **Work schedule:** your workdays plus start and finish times. The schedule controls when automation is active. It never creates attendance by itself.
- Click **Save**. The running app picks up the change within 15 seconds.

---

## 4. What happens at check-in

Automatic check-in happens only when **all** of these are true:

1. It is a workday, between 2 hours before your start time and your finish time. Arriving late is fine: if your start is 09:00 and you arrive at 11:00, it still works.
2. Your computer is **unlocked** and you are **using it**: there was input when presence was first seen, and again after the confirmation delay.
3. You are on the **office network**. This can be turned off if you also want auto check-in on remote days.
4. This stays true for the **confirmation delay** (default 2 min). Locking the PC or leaving the network during that time restarts the wait. The icon is yellow while waiting.

Then the app reads Odoo. **If you are already checked in, nothing happens.** Otherwise it checks you in, reads Odoo again to confirm, and shows a notification.

- Clicking *Check Out Now* turns off auto check-in until you next lock and unlock the PC. This way a manual check-out isn't undone 2 minutes later, but coming back from lunch still works.
- On a remote day, with "only on the office network" on, you get one notification ("Working remotely? Use Check In Now") and nothing is recorded.

## 5. What happens at check-out

The app checks you out in exactly these situations. Everything else leaves you checked in.

| # | Trigger | What happens | Time recorded |
|---|---|---|---|
| 1 | You click **Check Out Now** | Checks Odoo, checks you out, confirms. Auto check-in pauses until your next unlock. | Now |
| 2 | **Lid closed (sleep) or shutdown** on a workday, **after finish time**, while **on the office network** (*Check out when the PC sleeps or shuts down…*, off by default) | Checks you out immediately, before the computer goes offline. | The moment you closed the lid or shut down |
| 3 | **Finish-time question** (*At finish time, ask…*, on by default): at your finish time, if you're still checked in, a dialog asks "Workday finished? Check out now?" | **Yes** checks you out. **No** keeps you checked in. If you answer **more than 15 min later** (you had already left), nothing is recorded and a notification tells you to fix it, so a wrong time is never saved. | When you click Yes |
| 4 | **Left the office network** (*Automatic check-out when I leave…*): gone for the grace period (default 15 min, plus 2 min for Wi-Fi to settle) | Usually **asks first**: "You left the office network at 17:32. Check out?". It checks out without asking only if the PC was locked or idle, never slept, Odoo can record exact times, and *Ask before checking out* is off. | Exact-time accounts: when you left. Others: when you click Yes. |

**What never checks you out**

- Locking the screen, or the computer sitting idle, for any length of time.
- Closing the lid **before** finish time (lunch or a meeting), or anywhere away from the office.
- A brief Wi-Fi drop shorter than the grace period.
- A remote day: if you were never on the office network during this attendance, leaving it can't be a "departure".
- Waking up at home after the laptop slept on the way. You get asked instead.

**If Odoo can't be reached at check-out time** (for example, the Wi-Fi died first), the check-out is queued and retried with increasing delays (30 s up to 15 min). If it can't be sent soon (5 min, or 15 min for exact-time accounts), it is **not** replayed silently. You get asked, because recording it late would give a wrong time.

**Reminders:** if you're still checked in an hour after finish time, you get one notification. If an attendance is still open from an earlier day, you get a notification telling you to fix it in Odoo.

> **Normal employees, note:** Odoo's check-in button always records the *current* time. That is why check-out #2 (on sleep or shutdown) is the most accurate automatic option. Turn it on in Settings if you close your laptop when leaving the office.

## 6. Days off

On non-workdays the app reads Odoo once, then **sleeps**: no Odoo calls, no network scans, and only a date check every 10 minutes. The icon is gray and reads "day off, automation sleeping". **Check In Now** still works if you work that day, and automation then runs normally until you check out. The app stays running so it's ready on Monday even if the computer only slept over the weekend.

---

## 7. All settings

| Setting | Default | Meaning |
|---|---|---|
| Workdays, Start, Finish | Mon–Fri, 09:00–18:00 | When automation is active. Also used for the finish-time question and the sleep check-out. |
| Office network | – | Wi-Fi names and/or `gw:` IDs, comma-separated. |
| Automatic check-in | on | See §4. |
| …only on the office network | on | Turn off to auto check-in on remote days too. |
| Automatic check-out when I leave the office network | on | See §5 #4. |
| Ask before checking out | on | Always ask for #4. |
| At finish time, ask whether to check out | on | See §5 #3. |
| Check out when the PC sleeps or shuts down at the office after finish time | off | See §5 #2. |
| Sleep on days off | on | See §6. |
| Check-in delay / Check-out grace | 2 / 15 min | Confirmation times. |
| Notify on check-in/out, Reminders | on | Notifications. |
| Start at login | off | Windows: `HKCU\...\Run`. macOS: `~/Library/LaunchAgents/com.odooautoattendance.app.plist`. |

Advanced (edit `config.json` only): `earliest_before_start_min` (120), `sync_interval_min` (5), `poll_seconds` (15).

## 8. Privacy and security

- The app only reads whether the screen is locked, seconds since the last input (no keystrokes or content), the Wi-Fi name, and the router address. No screenshots, keystrokes, or app content.
- The password or API key is kept in the OS credential store. The config file has no secrets, and logs never contain passwords, keys, or cookies.
- HTTPS only, certificates verified. It uses your own account and Odoo's normal permissions.
- Login failures stop all retries until you change Settings or click a menu action, so Odoo's lockout is never triggered.

## 9. Files and uninstall

| | Windows | macOS |
|---|---|---|
| App | `%LOCALAPPDATA%\Programs\OdooAutoAttendance\` | `/Applications/OdooAutoAttendance.app` |
| Config | `%APPDATA%\OdooAutoAttendance\config.json` | `~/Library/Application Support/OdooAutoAttendance/config.json` |
| State | `%LOCALAPPDATA%\OdooAutoAttendance\state.json` | `~/Library/Application Support/OdooAutoAttendance/state.json` |
| Logs | `%LOCALAPPDATA%\OdooAutoAttendance\logs\app.log` | `~/Library/Logs/OdooAutoAttendance/app.log` |
| Secret | Credential Manager → Windows Credentials → `OdooAutoAttendance` | Keychain Access → `OdooAutoAttendance` |

**Uninstall:** untick *Start at login* and click Save, then choose **Exit** from the tray. Delete the app, the desktop shortcut and the folders above, and remove the stored secret.

## 10. Build it yourself

```powershell
# Windows: runs the tests, then builds dist\OdooAutoAttendance.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
```
```bash
# macOS (must be built on a Mac): builds dist/OdooAutoAttendance.app
./build_mac.sh
```
GitHub Actions builds both on every push and attaches them to the release when a `v*` tag is pushed (`.github/workflows/build.yml`).

**Tests:** `python -m pytest -q`. They use a fake in-memory Odoo server and a fake clock and presence, and never contact a real Odoo.

## 11. How it talks to Odoo

| Method | Endpoint | Notes |
|---|---|---|
| `json2` | `POST /json/2/<model>/<method>`, bearer API key | Odoo 19+ external API. Custom plan only. |
| `jsonrpc` | `POST /jsonrpc` (`execute_kw`) | Legacy external API. Custom plan only. Deprecated; Odoo plans to remove it in Odoo 22 / Online 21.1. |
| `web` | `/web/session/authenticate`, `/web/dataset/call_kw`, `/hr_attendance/systray_check_in_out` | The web client's own session. Works on every plan; needs the password, no 2FA. |

On Odoo 17+, normal employees may only *read* their attendance through the external APIs, so `auto` falls back to the web session and its systray check-in button.

## 12. Verification status

- **Windows + Odoo Online (saas~19.3), web session:** connection, status reading, check-in and check-out have been confirmed against a real account. The sleep-signal plumbing has been exercised on Windows.
- **macOS:** CI runs the tests and the build on macOS. Menu-bar behaviour, lock and idle detection, dialogs, and sleep check-out on a real Mac are not verified yet. Please report what you see (View Logs).
- **Officer / API-key (`direct`) mode:** tested against the mock server only.

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| Red icon, "Login failed" | Wrong password or expired API key, or a password used with `json2`. Fix it in Settings. |
| "may only read attendances through the external API" | Use method `web` with your password. |
| "…two-factor authentication" | See the 2FA row in §2. |
| Never checks in automatically | Check the workdays and times, the office network (click **Add current** at the office), and that automation isn't paused. **View Logs** shows every decision. |
| Office Wi-Fi not detected | Use the `gw:` ID, or allow Location for desktop apps (Windows) or for the app (macOS). |
| Didn't check out when I closed the lid | The *sleep or shutdown* option must be on, it must be after finish time on a workday, and you must be on the office network. Check View Logs for "System is going to sleep". |
| "Already running" | Only one copy runs. Look in the tray overflow (Windows) or the menu bar (macOS). |

## License

MIT, see [LICENSE](LICENSE).
