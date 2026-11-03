# Builds dist\OdooAutoAttendance.exe (single file, no console, no Python required).
# Usage:  powershell -ExecutionPolicy Bypass -File .\build.ps1 [-SkipTests]
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --disable-pip-version-check -q -r requirements-dev.txt
if (-not $SkipTests) {
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
}

New-Item -ItemType Directory -Force assets | Out-Null
python -c "from app.icons import write_ico; write_ico('assets/icon.ico')"

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name OdooAutoAttendance `
    --icon assets\icon.ico `
    --hidden-import pystray._win32 `
    --hidden-import keyring.backends.Windows `
    --exclude-module pytest `
    --exclude-module numpy --exclude-module matplotlib --exclude-module IPython `
    --exclude-module gevent --exclude-module pygments --exclude-module prompt_toolkit `
    --exclude-module jedi --exclude-module rich --exclude-module setuptools `
    --exclude-module jupyter_client --exclude-module ipykernel --exclude-module zmq `
    --exclude-module nbformat --exclude-module jinja2 --exclude-module markdown_it `
    --exclude-module cryptography --exclude-module urllib3 --exclude-module wheel `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "Built: $PSScriptRoot\dist\OdooAutoAttendance.exe"
