#!/bin/zsh
# Builds the app and packages it as a drag-to-install DMG in build/.
#   macos/release.sh             build, sign and (when credentials exist) notarize build/Local-Studio-<version>.dmg
#   macos/release.sh --dry-run   dependency audits and signature checks of the existing build only
#   macos/release.sh --publish   the same as a plain run, then publish GitHub release v<version> with the DMG
# For Macs other than your own, the DMG must be signed with a Developer ID Application
# certificate and notarized. One-time setup:
#   1. developer.apple.com → Certificates → "+" → Developer ID Application → install it
#   2. xcrun notarytool store-credentials notary --apple-id YOU@icloud.com --team-id TEAMID
#      (uses an app-specific password from appleid.apple.com)
# Then this script notarizes and staples automatically.
#
# --publish refuses anything a download must not be: a build that is not notarized, not from main as pushed, from a dirty
# tree, a version already released, or one without a dated CHANGELOG.md entry. It uploads the DMG twice, as
# Local-Studio-<version>.dmg and as Local-Studio.dmg, so the README's releases/latest/download/Local-Studio.dmg link always
# fetches the newest release; the notes are that version's CHANGELOG entry plus the DMG's SHA-256.
set -e
HERE="${0:A:h}"; ROOT="${HERE:h}"
DRY_RUN=0; PUBLISH=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  --publish) PUBLISH=1 ;;
  *) echo "Usage: $0 [--dry-run | --publish]" >&2; exit 2 ;;
esac
VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/app.py")
fail() { echo "release: $*" >&2; exit 1; }

changelog_entry() {          # the CHANGELOG.md section for $VERSION, without its heading
  awk -v v="$VERSION" 'index($0, "## " v " ") == 1 { on = 1; next } on && /^## / { exit } on' "$ROOT/CHANGELOG.md"
}

if (( PUBLISH )); then
  echo "==> Checking that $VERSION can be published"
  command -v gh >/dev/null || fail "the GitHub CLI (gh) is not installed"
  REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) || fail "gh cannot read the repository (gh auth login?)"
  [[ "$(git -C "$ROOT" branch --show-current)" == main ]] || fail "publish from main, not $(git -C "$ROOT" branch --show-current)"
  [[ -z "$(git -C "$ROOT" status --porcelain)" ]] || fail "the working tree has uncommitted changes"
  git -C "$ROOT" fetch -q origin main
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$(git -C "$ROOT" rev-parse origin/main)" ]] || fail "main is not the same as origin/main: pull or push first"
  if gh release view "v$VERSION" --repo "$REPO" >/dev/null 2>&1; then fail "v$VERSION is already released: bump APP_VERSION"; fi
  heading=$(grep -m1 "^## $VERSION " "$ROOT/CHANGELOG.md" || true)
  [[ -n "$heading" ]] || fail "CHANGELOG.md has no '## $VERSION (date)' entry"
  [[ "$heading" != *unreleased* ]] || fail "date the CHANGELOG.md entry first: $heading"
  [[ -n "$(changelog_entry | tr -d '[:space:]')" ]] || fail "the CHANGELOG.md entry for $VERSION is empty"
  security find-identity -v -p codesigning | /usr/bin/grep -q 'Developer ID Application' || fail "no Developer ID Application certificate in the keychain"
  xcrun notarytool history --keychain-profile notary >/dev/null 2>&1 || fail "no notarization credentials: run the notarytool store-credentials step above"
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
DMG="$ROOT/build/Local-Studio-$VERSION.dmg"
STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG"; mkdir -p "$STAGE"
ditto "$ROOT/build/Local Studio.app" "$STAGE/Local Studio.app"
ln -s /Applications "$STAGE/Applications"
echo "==> Creating $DMG"
hdiutil create -volname "Local Studio $VERSION" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG" >/dev/null
rm -rf "$STAGE"

NOTARIZED=0
ID=$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/ {print $2; exit}')
if [[ -n "$ID" ]]; then
  codesign --force --sign "$ID" --timestamp "$DMG"
  if xcrun notarytool history --keychain-profile notary >/dev/null 2>&1; then
    echo "==> Notarizing (a few minutes)"
    xcrun notarytool submit "$DMG" --keychain-profile notary --wait
    xcrun stapler staple "$DMG"
    NOTARIZED=1
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

if (( PUBLISH )); then
  echo "==> Publishing v$VERSION to $REPO"
  (( NOTARIZED )) || fail "the DMG was not notarized; nothing was published"
  xcrun stapler validate "$DMG"
  spctl -a -vv -t open --context context:primary-signature "$DMG"
  cp "$DMG" "$ROOT/build/Local-Studio.dmg"
  SUM=$(shasum -a 256 "$DMG" | awk '{print $1}')
  NOTES="$ROOT/build/release-notes-$VERSION.md"
  { changelog_entry; print; print "**Download:** \`Local-Studio-$VERSION.dmg\` (or \`Local-Studio.dmg\`, the same file). Signed with a Developer ID and notarized by Apple."
    print; print "SHA-256: \`$SUM\`"; } > "$NOTES"
  gh release create "v$VERSION" "$DMG" "$ROOT/build/Local-Studio.dmg" --repo "$REPO" --target "$(git -C "$ROOT" rev-parse HEAD)" \
    --title "Local Studio $VERSION" --notes-file "$NOTES"
  echo "Published: https://github.com/$REPO/releases/tag/v$VERSION"
fi
