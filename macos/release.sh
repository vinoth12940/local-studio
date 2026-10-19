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
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--dry-run]" >&2; exit 2
fi
UV=$(command -v uv || echo ~/.local/bin/uv)
python3 "$HERE/audit_dependencies.py" --uv "$UV"
if (( ! DRY_RUN )); then "$HERE/build.sh"; fi
APP="$ROOT/build/Local Studio.app"
[[ -d "$APP" ]] || { echo "Build the app before a dry run: ./macos/build.sh" >&2; exit 1; }
echo "==> Verifying app signature"
codesign --verify --deep --strict --verbose=2 "$APP"
assess_distribution() {
  if codesign -dv --verbose=4 "$APP" 2>&1 | /usr/bin/grep -q '^Authority=Developer ID Application:'; then
    spctl -a -vv --type execute "$APP"
  else
    echo "   Gatekeeper distribution assessment requires a Developer ID signed build; current signature is local development."
  fi
}
if (( DRY_RUN )); then
  assess_distribution
  echo "Dry run passed: dependency audits and signature checks; no DMG or notarization submitted."
  exit 0
fi
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
# Assess after notarization, so a new Developer ID build can obtain its ticket first.
assess_distribution
echo "Done: $DMG ($(du -h "$DMG" | cut -f1))"
