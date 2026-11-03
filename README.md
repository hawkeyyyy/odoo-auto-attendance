# Odoo Auto Attendance

[![Build](https://github.com/hawkeyyyy/odoo-auto-attendance/actions/workflows/build.yml/badge.svg)](https://github.com/hawkeyyyy/odoo-auto-attendance/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/hawkeyyyy/odoo-auto-attendance)](https://github.com/hawkeyyyy/odoo-auto-attendance/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A lightweight tray (Windows) and menu-bar (macOS) application that checks you in and out of **Odoo Attendance** automatically, based on your actual presence at work.

- **Automatic check-in** when you arrive at the office and start using your computer.
- **Automatic check-out** when you leave: you close the lid or shut down after work, or confirm the end-of-day prompt.
- **No false records.** Locking the screen or stepping away never checks you out. When a situation is unclear, the app asks you.
- **Safe by design.** Every change is verified against Odoo, duplicates are prevented, and credentials stay in the operating system's secure store.

---

## Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Getting started](#getting-started)
4. [How it works](#how-it-works)
5. [Settings](#settings)
6. [Security and privacy](#security-and-privacy)
7. [Uninstalling](#uninstalling)
8. [Troubleshooting](#troubleshooting)
9. [Building from source](#building-from-source)

---

## Requirements

### Computer

| | Windows | macOS |
|---|---|---|
| Operating system | Windows 10 or 11, 64-bit | macOS 12 Monterey or later |
| Processor | x64 | Apple Silicon (M1 or later). For Intel Macs, see [Building from source](#building-from-source). |
| Other | None. Python is not required. | None. Python is not required. |

### Odoo

- **Odoo Online, Odoo.sh or on-premise**, version **16, 17, 18 or 19** (including SaaS versions).
- The **Attendances** app is installed, and your user is **linked to an employee**.
- One of the following:
  - **Your Odoo password.** This is the right choice for most employees and works on every Odoo plan. Two-factor authentication must be **off** for this account.
  - **An API key.** Only for Attendance Officers or Administrators on plans with external API access (the Odoo Online *Custom* plan, Odoo.sh, or on-premise).

### Office network

- A Wi-Fi network or wired connection used at your office. The app identifies it by Wi-Fi name and/or router address. No changes to the network are needed.

---

## Installation

Download the latest version from the **[Releases page](https://github.com/hawkeyyyy/odoo-auto-attendance/releases/latest)**.

### Windows

1. Download **`OdooAutoAttendance.exe`** and **`install.ps1`** and save both in the same folder (for example, *Downloads*).
2. Open that folder in File Explorer. Right-click an empty area and choose **Open in Terminal**.
3. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   The installer:
   - copies the app to `%LOCALAPPDATA%\Programs\OdooAutoAttendance`
   - creates an **Odoo Auto Attendance** shortcut on your desktop
   - starts the app
4. If Windows SmartScreen shows *"Windows protected your PC"*, click **More info → Run anyway**. The app is not code-signed.
5. The Settings window opens. Continue with [Getting started](#getting-started).

### macOS

1. Download **`OdooAutoAttendance-macOS.zip`**.
2. Double-click the zip file to extract **OdooAutoAttendance.app**.
3. Drag **OdooAutoAttendance.app** into your **Applications** folder.
4. Open the app for the first time. The app is not notarized by Apple, so macOS blocks it once:
   - **macOS 15 Sequoia and later:** double-click the app and click **Done** in the warning. Then open **System Settings → Privacy & Security**, scroll to *Security*, click **Open Anyway** next to *OdooAutoAttendance*, and confirm with your password.
   - **macOS 12–14:** right-click (or Control-click) the app, choose **Open**, then click **Open** again.
   - **Alternative (any version):** in Terminal, run:
     ```bash
     xattr -dr com.apple.quarantine /Applications/OdooAutoAttendance.app
     ```
     Then open the app normally.
5. A clock icon appears in the **menu bar** at the top of the screen. The app has no Dock icon. The Settings window opens.
6. When macOS asks:
   - **Keychain access:** click **Always Allow**, so the app can read your saved password.
   - **Notifications for "Script Editor":** click **Allow**. The app shows its notifications and questions through this system component.
7. Optional: to start the app automatically, tick **Start at login** in Settings.

---

## Getting started

### 1. Connect your Odoo account

Open **Settings** from the tray or menu-bar icon and complete the **Odoo account** section:

| Field | Value |
|---|---|
| URL | Your Odoo address, e.g. `https://yourcompany.odoo.com` |
| Database | Leave empty. It is detected automatically. |
| Login | The email address you use to sign in to Odoo |
| Password / API key | Your Odoo password (or API key, see [Requirements](#requirements)) |
| Method | `auto` |

Click **Test connection**. The test only reads data and never changes your attendance. A successful result shows your Odoo version, your employee name, and your current attendance status.

### 2. Register your office network

While connected at the office, click **Add current** in the **Office network** section. Do this from the office, not from home.

### 3. Set your schedule

Select your **workdays** and enter your usual **start** and **finish** times. Click **Save**.

The icon shows your status:

| Color | Status |
|---|---|
| Green | Checked in |
| Gray | Checked out, or a day off |
| Yellow | Waiting: confirming your presence, retrying Odoo, or waiting for your answer |
| Red | Connection or sign-in problem. Hover over the icon for details. |

Right-click the icon (Windows) or click it (macOS) to see your status and today's check-in time. From the same menu you can check in or out manually, pause automation, or open Settings and logs.

---

## How it works

### Check-in

You are checked in automatically when all of these are true:

- It is a **workday**, between 2 hours before your start time and your finish time. Arriving late is fine.
- Your computer is **unlocked** and **in use**.
- You are on the **office network**.
- These conditions hold for **2 minutes**.

If you are already checked in, nothing happens. After a manual **Check Out Now**, automatic check-in pauses until the next time you lock and unlock your computer. This keeps a manual check-out from being undone.

### Check-out

You are checked out in the following situations only:

| Situation | What happens |
|---|---|
| You click **Check Out Now** | Checked out immediately. |
| You **close the lid or shut down** after your finish time while at the office *(enable in Settings)* | Checked out immediately, before the computer goes offline. The recorded time is the moment you left. |
| **End-of-day prompt** at your finish time | The app asks *"Workday finished? Check out now?"*. **Yes** checks you out. **No** keeps you checked in. If you answer more than 15 minutes later, nothing is recorded, to avoid saving an incorrect time. |
| You **leave the office network** for more than 15 minutes | The app asks for confirmation before checking you out. |

These never check you out:

- Locking the screen, or leaving the computer idle.
- Closing the lid **before** your finish time (for example at lunch), or away from the office.
- Short Wi-Fi interruptions.
- Days when you work remotely and are never on the office network.

If you are still checked in an hour after your finish time, you get a reminder.

> **Note:** for most employees, Odoo records the time at which the check-out is sent. Enable **Check out when the PC sleeps or shuts down** for the most accurate end-of-day time.

### Offline and sleep

- If Odoo cannot be reached, actions are retried automatically. If an action can't be sent within a few minutes, the app asks you before recording it, rather than saving a late, incorrect time.
- After sleep, restart, or unlock, the app re-reads your status from Odoo before doing anything.

### Days off

On non-workdays the app checks your status once, then stays idle: no Odoo requests and no network checks. **Check In Now** remains available if you work that day.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| Workdays, Start, Finish | Mon–Fri, 09:00–18:00 | When automation is active |
| Office network | — | Wi-Fi names and/or router IDs (`gw:…`), comma-separated |
| Automatic check-in | On | Check in when you arrive |
| …only on the office network | On | Turn off to also check in automatically when working remotely |
| Automatic check-out when I leave the office network | On | Leaving the office for 15 minutes triggers a confirmation |
| Ask before checking out | On | Always confirm network-based check-outs |
| At finish time, ask whether to check out | On | End-of-day prompt |
| Check out when the PC sleeps or shuts down at the office after finish time | Off | Instant, exact check-out when you leave |
| Sleep on days off | On | No activity on non-workdays |
| Check-in delay / Check-out grace | 2 / 15 min | Confirmation periods |
| Notifications, Reminders | On | |
| Start at login | Off | Launch automatically when you sign in |

---

## Security and privacy

- Your password or API key is stored in **Windows Credential Manager** or the **macOS Keychain**, never in a file.
- All communication with Odoo uses **HTTPS** with certificate verification.
- The app uses only your own account and respects your Odoo permissions.
- To detect presence, the app reads only: whether the screen is locked, the time since the last keyboard or mouse input, and the current network. It never records keystrokes, screenshots, or application content.
- Log files never contain passwords, keys, or session data.
- After a failed sign-in, the app stops retrying until you update Settings, so your Odoo account is not locked.

---

## Uninstalling

1. In **Settings**, untick **Start at login** and click **Save**.
2. Choose **Exit** from the tray or menu-bar menu.
3. Remove the files:

| | Windows | macOS |
|---|---|---|
| Application | `%LOCALAPPDATA%\Programs\OdooAutoAttendance` and the desktop shortcut | `/Applications/OdooAutoAttendance.app` |
| Settings and logs | `%APPDATA%\OdooAutoAttendance` and `%LOCALAPPDATA%\OdooAutoAttendance` | `~/Library/Application Support/OdooAutoAttendance` and `~/Library/Logs/OdooAutoAttendance` |
| Saved password | Credential Manager → Windows Credentials → `OdooAutoAttendance` | Keychain Access → `OdooAutoAttendance` |

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Red icon: "Login failed" | Check your login and password in Settings. API keys expire after at most three months. |
| "may only read attendances through the external API" | Set **Method** to `web` and enter your password instead of an API key. |
| "two-factor authentication is enabled" | Password sign-in is not possible with 2FA. Use an API key with Attendance Officer rights, or disable 2FA for this account. |
| Not checking in automatically | Confirm your workdays and times, register the office network from the office (**Add current**), and make sure automation is not paused. |
| Office Wi-Fi not detected | Use the router ID (`gw:…`) added by **Add current**. It works without Location permission. |
| Not checking out when closing the lid | Enable the sleep/shutdown option. It applies only after your finish time, on a workday, at the office. |
| macOS: "app is damaged" or "can't be opened" | Follow step 4 of the [macOS installation](#macos). |
| Need more detail | Choose **View Logs** from the menu. Every decision the app makes is logged. |

---

## Building from source

Requires **Python 3.12+** with tkinter.

```bash
git clone https://github.com/hawkeyyyy/odoo-auto-attendance.git
cd odoo-auto-attendance
python -m pip install -r requirements-dev.txt
python -m pytest -q          # tests use a simulated Odoo server
python main.py               # run without packaging
```

Package the application:

```powershell
# Windows → dist\OdooAutoAttendance.exe
powershell -ExecutionPolicy Bypass -File .\build.ps1
```
```bash
# macOS (Apple Silicon or Intel) → dist/OdooAutoAttendance.app
./build_mac.sh
```

Releases are built automatically by GitHub Actions when a version tag (`v*`) is pushed.

---

## License

Released under the [MIT License](LICENSE).
