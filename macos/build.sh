#!/bin/zsh
# Builds a self-contained "Local Studio.app" (code bundled in Contents/Resources/app).
#   macos/build.sh            build into build/
#   macos/build.sh --install  build and copy to /Applications
# Signs with a "Developer ID Application" identity when available (needed for other Macs),
# else your Apple Development identity, else ad-hoc.
set -e
HERE="${0:A:h}"; ROOT="${HERE:h}"
APP="$ROOT/build/Local Studio.app"
VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/app.py")
BUILD=$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 1)

rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

echo "==> Checking UI script"
if command -v node >/dev/null 2>&1; then node "$HERE/check_ui.js" "$ROOT/index.html"; else echo "   (node not found, skipped)"; fi
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$ROOT/app.py" && echo "   app.py OK"
UV=$(command -v uv || echo ~/.local/bin/uv)
$UV run -q --no-project --python 3.12 --with numpy python "$HERE/check_search.py"
$UV run -q --no-project --python 3.12 --with-requirements "$ROOT/requirements-app.txt" python "$HERE/check_app.py"
$UV run -q --no-project --python 3.12 --with-requirements "$ROOT/requirements-app.txt" python "$HERE/check_agent.py"

echo "==> Compiling"
xcrun swiftc -O -target arm64-apple-macos14.0 -framework Cocoa -framework WebKit \
  "$HERE/main.swift" -o "$APP/Contents/MacOS/LocalStudio"
xcrun swiftc -O -target arm64-apple-macos14.0 -framework AppKit -framework PDFKit -framework Vision \
  "$HERE/document_reader.swift" -o "$APP/Contents/MacOS/DocumentReader"
cp "$APP/Contents/MacOS/DocumentReader" "$ROOT/build/DocumentReader"
$UV run -q --no-project --python 3.12 --with reportlab --with pillow python "$HERE/check_document.py" "$APP/Contents/MacOS/DocumentReader"

echo "==> Bundling code"
for f in app.py index.html catalog.json requirements-app.txt search.py search-requirements.txt setup.sh start.sh stop.sh uninstall.sh; do
  cp "$ROOT/$f" "$APP/Contents/Resources/app/"
done
chmod +x "$APP/Contents/Resources/app/"*.sh

echo "==> Icon"
ICONSET="$ROOT/build/AppIcon.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
$UV run -q --no-project --python 3.12 --with pillow python "$HERE/logos.py" B "$ROOT/build/icon_1024.png" >/dev/null
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
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${BUILD}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSDesktopFolderUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSFileProviderDomainUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSHumanReadableCopyright</key><string>Runs AI models locally on Apple Silicon.</string>
</dict></plist>
PLIST

echo "==> Signing"
IDS=$(security find-identity -v -p codesigning 2>/dev/null)
IDENTITY=$(print -r -- "$IDS" | awk -F'"' '/Developer ID Application/ {print $2; exit}')
[[ -z "$IDENTITY" ]] && IDENTITY=$(print -r -- "$IDS" | awk -F'"' '/Apple Development/ {print $2; exit}')
TS="--timestamp=none"; [[ "$IDENTITY" == Developer\ ID* ]] && TS="--timestamp"
codesign --force --deep --options runtime $TS --sign "${IDENTITY:--}" "$APP"
codesign --verify --deep --strict "$APP" && echo "   signed with: ${IDENTITY:-ad-hoc}"

if [[ "${1:-}" == "--install" ]]; then
  echo "==> Installing to /Applications"
  rm -rf "/Applications/Local Studio.app"
  ditto "$APP" "/Applications/Local Studio.app"
  echo "   /Applications/Local Studio.app"
fi
echo "Done: $APP ($VERSION, build $BUILD)"
