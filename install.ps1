# Installs Odoo Auto Attendance for the current user and creates a desktop shortcut.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 [-ExePath <path>] [-NoStart]
#
# Without -ExePath it uses OdooAutoAttendance.exe next to this script (release download),
# or dist\OdooAutoAttendance.exe (after .\build.ps1).
param([string]$ExePath, [switch]$NoStart)
$ErrorActionPreference = "Stop"

if (-not $ExePath) {
    foreach ($candidate in @("$PSScriptRoot\OdooAutoAttendance.exe", "$PSScriptRoot\dist\OdooAutoAttendance.exe")) {
        if (Test-Path $candidate) { $ExePath = $candidate; break }
    }
}
if (-not $ExePath -or -not (Test-Path $ExePath)) {
    throw "OdooAutoAttendance.exe not found. Put it next to install.ps1, pass -ExePath, or run .\build.ps1 first."
}
Unblock-File $ExePath -ErrorAction SilentlyContinue   # clear the "downloaded from the internet" mark

$dir = Join-Path $env:LOCALAPPDATA "Programs\OdooAutoAttendance"
$exe = Join-Path $dir "OdooAutoAttendance.exe"

# Stop a running copy so the exe can be replaced.
Get-Process OdooAutoAttendance -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force $dir | Out-Null
if ((Resolve-Path $ExePath).Path -ne $exe) { Copy-Item $ExePath $exe -Force }

# Desktop shortcut
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "Odoo Auto Attendance.lnk"))
$lnk.TargetPath = $exe
$lnk.WorkingDirectory = $dir
$lnk.IconLocation = "$exe,0"
$lnk.Description = "Odoo Auto Attendance (tray)"
$lnk.Save()

# If "Start at login" is on, point it at the installed copy.
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Get-ItemProperty $run -Name OdooAutoAttendance -ErrorAction SilentlyContinue) {
    Set-ItemProperty $run -Name OdooAutoAttendance -Value "`"$exe`""
}

Write-Host "Installed: $exe"
Write-Host "Shortcut:  $(Join-Path $desktop 'Odoo Auto Attendance.lnk')"
if (-not $NoStart) { Start-Process $exe }
