#!/bin/zsh
# Builds the app and packages it as a drag-to-install DMG in build/.
# For Macs other than your own, the DMG must be signed with a Developer ID Application
# certificate and notarized. One-time setup:
#   1. developer.apple.com → Certificates → "+" → Developer ID Application → install it
#   2. xcrun notarytool store-credentials notary --apple-id YOU@icloud.com --team-id TEAMID
#      (uses an app-specific password from appleid.apple.com)
# Then this script notarizes and staples automatically.
set -e
HERE="${0:A:h}"; ROOT="${HERE:h}"
"$HERE/build.sh"
VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/app.py")
DMG="$ROOT/build/Local-Studio-$VERSION.dmg"
STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
ditto "$ROOT/build/Local Studio.app" "$STAGE/Local Studio.app"
ln -s /Applications "$STAGE/Applications"
echo "==> Creating $DMG"
hdiutil create -volname "Local Studio $VERSION" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG" >/dev/null
rm -rf "$STAGE"

ID=$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/ {print $2; exit}')
if [[ -n "$ID" ]]; then
  codesign --force --sign "$ID" --timestamp "$DMG"
  if xcrun notarytool history --keychain-profile notary >/dev/null 2>&1; then
    echo "==> Notarizing (a few minutes)"
    xcrun notarytool submit "$DMG" --keychain-profile notary --wait
    xcrun stapler staple "$DMG"
    echo "   notarized: opens on any Mac without warnings"
  else
    echo "   signed with Developer ID, NOT notarized: run the notarytool store-credentials step above"
  fi
else
  echo "   No Developer ID certificate: this DMG opens without warnings only on your own Macs."
  echo "   On other Macs: right-click the app → Open, or System Settings → Privacy & Security → Open Anyway."
fi
echo "Done: $DMG ($(du -h "$DMG" | cut -f1))"
