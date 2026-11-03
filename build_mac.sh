#!/usr/bin/env bash
# Builds dist/OdooAutoAttendance.app (menu-bar only, no Dock icon). Run on a Mac.
# Usage:  ./build_mac.sh [--skip-tests]
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
"$PY" -m pip install --disable-pip-version-check -q -r requirements-dev.txt
if [[ "${1:-}" != "--skip-tests" ]]; then
    "$PY" -m pytest -q
fi

mkdir -p assets
"$PY" -c "from app.icons import write_png; write_png('assets/icon.png')"

"$PY" -m PyInstaller --noconfirm --clean --windowed \
    --name OdooAutoAttendance \
    --icon assets/icon.png \
    --osx-bundle-identifier com.odooautoattendance.app \
    --hidden-import pystray._darwin \
    --hidden-import keyring.backends.macOS \
    --exclude-module pytest --exclude-module numpy --exclude-module matplotlib \
    --exclude-module IPython \
    main.py

APP="dist/OdooAutoAttendance.app"
PLIST="$APP/Contents/Info.plist"
# Menu-bar app: no Dock icon, no app switcher entry.
/usr/bin/plutil -replace LSUIElement -bool true "$PLIST"
# Ad-hoc signature (required after editing Info.plist on Apple Silicon).
/usr/bin/codesign --force --deep --sign - "$APP"

echo "Built: $(pwd)/$APP"
echo "Install: cp -R \"$APP\" /Applications/   (first launch: right-click > Open)"
