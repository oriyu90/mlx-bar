#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP="$PROJECT_DIR/dist/MLXBar.app"
DMG="$PROJECT_DIR/dist/MLXBar-${VERSION:-1.4.1}.dmg"

test -x "$APP/Contents/MacOS/MLXBar"
test -x "$APP/Contents/MacOS/MLXBarCoordinator"
test -x "$APP/Contents/Resources/cli/MLXBarCLI"
test -x "$APP/Contents/Resources/MLXBar_MLXBar.bundle/mlxbarctl"
test -f "$APP/Contents/Resources/AppIcon.icns"
test -f "$APP/Contents/Library/LaunchAgents/com.yukiorita.MLXBar.Coordinator.plist"
test -f "$APP/Contents/Resources/README.md"
test -f "$APP/Contents/Resources/licence.md"
plutil -lint "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Library/LaunchAgents/com.yukiorita.MLXBar.Coordinator.plist"
codesign --verify --deep --strict --verbose=2 "$APP"
test -z "$(find "$APP" -type d -name __pycache__ -print -quit)"
if [ -f "$DMG" ]; then hdiutil verify "$DMG"; fi
