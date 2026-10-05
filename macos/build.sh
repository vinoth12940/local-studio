#!/bin/zsh
# Builds "Local Studio.app" from main.swift.
#   macos/build.sh            build into build/
#   macos/build.sh --install  build and copy to /Applications
set -e
HERE="${0:A:h}"; ROOT="${HERE:h}"
APP="$ROOT/build/Local Studio.app"
VERSION=$(git -C "$ROOT" describe --tags --always 2>/dev/null || echo 1.0)

rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> Compiling"
xcrun swiftc -O -target arm64-apple-macos13.0 -framework Cocoa -framework WebKit \
  "$HERE/main.swift" -o "$APP/Contents/MacOS/LocalStudio"

echo "==> Icon"
ICONSET="$ROOT/build/AppIcon.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
UV=$(command -v uv || echo ~/.local/bin/uv)
$UV run -q --python 3.12 --with pillow python "$HERE/make_icon.py" "$ROOT/build/icon_1024.png" >/dev/null
for s in 16 32 128 256 512; do
  sips -z $s $s "$ROOT/build/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$ROOT/build/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Local Studio</string>
  <key>CFBundleDisplayName</key><string>Local Studio</string>
  <key>CFBundleIdentifier</key><string>com.localstudio.app</string>
  <key>CFBundleExecutable</key><string>LocalStudio</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSHumanReadableCopyright</key><string>Runs Qwen models locally with MLX.</string>
  <key>LSAppRoot</key><string>${ROOT}</string>
</dict></plist>
PLIST

echo "==> Signing"
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Apple Development/ {print $2; exit}')
codesign --force --deep --sign "${IDENTITY:--}" "$APP"
codesign --verify --deep "$APP" && echo "   signed with: ${IDENTITY:-ad-hoc}"

if [[ "${1:-}" == "--install" ]]; then
  echo "==> Installing to /Applications"
  rm -rf "/Applications/Local Studio.app"
  cp -R "$APP" /Applications/
  echo "   /Applications/Local Studio.app"
fi
echo "Done: $APP"
