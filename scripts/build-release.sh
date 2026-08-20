#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR="$PROJECT_DIR/.build/arm64-apple-macosx/release"
DIST_DIR="$PROJECT_DIR/dist"
APP_DIR="$DIST_DIR/MLXBar.app"
CONTENTS="$APP_DIR/Contents"
PYINSTALLER_DIR="$PROJECT_DIR/.release-python"
DMG_STAGE="$PROJECT_DIR/.dmg-stage"
VERSION=1.3.6

export SWIFT_MODULE_CACHE_PATH="$PROJECT_DIR/.build/module-cache"
export CLANG_MODULE_CACHE_PATH="$PROJECT_DIR/.build/clang-cache"
export UV_CACHE_DIR="$PROJECT_DIR/.uv-cache"
export PYINSTALLER_CONFIG_DIR="$PROJECT_DIR/.pyinstaller-config"

cd "$PROJECT_DIR"
swift build --disable-sandbox -c release

"$PROJECT_DIR/Coordinator/.venv/bin/python" -m PyInstaller --noconfirm --clean --onedir \
  --name MLXBarCoordinator --paths "$PROJECT_DIR/Coordinator" \
  --add-data "$PROJECT_DIR/Workers:Workers" \
  --distpath "$PYINSTALLER_DIR" --workpath "$PROJECT_DIR/.pyinstaller-work/coordinator" \
  --specpath "$PROJECT_DIR/.pyinstaller-work" "$PROJECT_DIR/scripts/coordinator_entry.py"
"$PROJECT_DIR/Coordinator/.venv/bin/python" -m PyInstaller --noconfirm --clean --onedir \
  --name MLXBarCLI --paths "$PROJECT_DIR/Coordinator" \
  --distpath "$PYINSTALLER_DIR" --workpath "$PROJECT_DIR/.pyinstaller-work/cli" \
  --specpath "$PROJECT_DIR/.pyinstaller-work" "$PROJECT_DIR/scripts/cli_entry.py"

rm -rf "$APP_DIR"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources/coordinator" "$CONTENTS/Resources/cli" "$CONTENTS/Library/LaunchAgents"
cp "$BUILD_DIR/MLXBar" "$CONTENTS/MacOS/MLXBar"
cp "$PROJECT_DIR/Packaging/Info.plist" "$CONTENTS/Info.plist"
test -f "$PROJECT_DIR/Packaging/icon.ico"
sips -s format icns "$PROJECT_DIR/Packaging/icon.ico" \
  --out "$CONTENTS/Resources/AppIcon.icns" >/dev/null
cp -R "$BUILD_DIR/MLXBar_MLXBar.bundle" "$CONTENTS/Resources/MLXBar_MLXBar.bundle"
cp -R "$PYINSTALLER_DIR/MLXBarCoordinator/." "$CONTENTS/Resources/coordinator/"
cp -R "$PYINSTALLER_DIR/MLXBarCLI/." "$CONTENTS/Resources/cli/"
cp "$PROJECT_DIR/Packaging/com.yukiorita.MLXBar.Coordinator.plist" "$CONTENTS/Library/LaunchAgents/"

if command -v uv >/dev/null 2>&1; then
  cp "$(command -v uv)" "$CONTENTS/Resources/MLXBar_MLXBar.bundle/uv"
fi

cp "$PROJECT_DIR/README.md" "$CONTENTS/Resources/README.md"
cp "$PROJECT_DIR/licence.md" "$CONTENTS/Resources/licence.md"
clang -mmacosx-version-min=14.0 "$PROJECT_DIR/Packaging/release/coordinator_launcher.c" \
  -o "$CONTENTS/MacOS/MLXBarCoordinator"
chmod +x "$CONTENTS/MacOS/MLXBar" "$CONTENTS/MacOS/MLXBarCoordinator" \
  "$CONTENTS/Resources/MLXBar_MLXBar.bundle/mlxbarctl" \
  "$CONTENTS/Resources/MLXBar_MLXBar.bundle/start-service" \
  "$CONTENTS/Resources/cli/MLXBarCLI"

# Worker sources are bundled as PyInstaller data. Exclude bytecode caches that
# may have been created by local tests before signing the app bundle.
find "$CONTENTS/Resources/coordinator" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$CONTENTS/Resources/coordinator" -type f -name '*.py[co]' -delete

IDENTITY=${DEVELOPER_ID_APPLICATION:--}
if [ "$IDENTITY" = "-" ]; then
  codesign --force --sign - "$CONTENTS/Resources/coordinator/MLXBarCoordinator"
  codesign --force --sign - "$CONTENTS/Resources/cli/MLXBarCLI"
  codesign --force --sign - "$CONTENTS/MacOS/MLXBarCoordinator"
  codesign --force --deep --sign - "$APP_DIR"
else
  # Only the bundled Python runtimes load MLX's unsigned C extensions, so the
  # library-validation and JIT relaxations stay scoped to them. Signing the app
  # bundle last, without --deep, keeps those entitlements from being applied to
  # the SwiftUI executable, which needs neither.
  codesign --force --options runtime --entitlements "$PROJECT_DIR/Packaging/entitlements.plist" \
    --sign "$IDENTITY" "$CONTENTS/Resources/coordinator/MLXBarCoordinator"
  codesign --force --options runtime --entitlements "$PROJECT_DIR/Packaging/entitlements.plist" \
    --sign "$IDENTITY" "$CONTENTS/Resources/cli/MLXBarCLI"
  codesign --force --options runtime --sign "$IDENTITY" "$CONTENTS/MacOS/MLXBarCoordinator"
  codesign --force --options runtime --entitlements "$PROJECT_DIR/Packaging/entitlements-app.plist" \
    --sign "$IDENTITY" "$CONTENTS/MacOS/MLXBar"
  codesign --force --options runtime --entitlements "$PROJECT_DIR/Packaging/entitlements-app.plist" \
    --sign "$IDENTITY" "$APP_DIR"
fi
codesign --verify --deep --strict --verbose=2 "$APP_DIR"

rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE/Source code"
cp -R "$APP_DIR" "$DMG_STAGE/MLXBar.app"
ln -s /Applications "$DMG_STAGE/Applications"
for item in README.md licence.md CHANGELOG.md SECURITY.md Package.swift Coordinator Workers CLI Sources Tests Packaging scripts '設計書_v2.1.md'; do
  cp -R "$PROJECT_DIR/$item" "$DMG_STAGE/Source code/"
done
rm -rf "$DMG_STAGE/Source code/Coordinator/.venv" "$DMG_STAGE/Source code/Coordinator/.pytest_cache"
find "$DMG_STAGE/Source code" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$DMG_STAGE/Source code" -type f -name '*.py[co]' -delete

rm -f "$DIST_DIR/MLXBar-$VERSION.dmg"
if [ "${SKIP_DMG:-0}" = "1" ]; then
  echo "$APP_DIR"
  exit 0
fi
hdiutil create -volname "MLXBar $VERSION" -srcfolder "$DMG_STAGE" -ov \
  -format UDZO "$DIST_DIR/MLXBar-$VERSION.dmg"
hdiutil verify "$DIST_DIR/MLXBar-$VERSION.dmg"

echo "$APP_DIR"
echo "$DIST_DIR/MLXBar-$VERSION.dmg"
