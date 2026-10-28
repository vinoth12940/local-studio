#!/bin/zsh
# Builds build/Local-Studio-<version>.dmg, signed and notarized, the same way Murmur's release.sh does: the app is
# notarized and stapled, then the DMG that holds it is signed, notarized and stapled too, and build/Local-Studio-<version>.dmg.sha256
# is written next to it. See docs/RELEASING.md.
#
#   macos/release.sh             build, sign, notarize and package
#   macos/release.sh --dry-run   dependency audits and signature checks of the existing build only
#   macos/release.sh --publish   the same as a plain run, then publish GitHub release v<version> with the DMG and its .sha256
#
#   LOCAL_STUDIO_NOTARY_PROFILE  a `notarytool store-credentials` profile name (default: notary)
#   LOCAL_STUDIO_NOTARY_KEY, LOCAL_STUDIO_NOTARY_KEY_ID, LOCAL_STUDIO_NOTARY_ISSUER
#                                alternative to the profile: an App Store Connect API key (.p8 path, key id, issuer id)
# The signing identity is the Developer ID Application certificate build.sh finds in the keychain.
#
# --publish refuses anything a download must not be: a build that is not notarized, not from main as pushed, from a dirty
# tree, a version already released, or one without a dated CHANGELOG.md entry. The notes are that entry plus the SHA-256.
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
DMG="$ROOT/build/Local-Studio-$VERSION.dmg"
NOTES="$ROOT/build/release-notes-$VERSION.md"
fail() { echo "release: $*" >&2; exit 1; }

if [[ -n "${LOCAL_STUDIO_NOTARY_KEY:-}" && -n "${LOCAL_STUDIO_NOTARY_KEY_ID:-}" && -n "${LOCAL_STUDIO_NOTARY_ISSUER:-}" ]]; then
  NOTARY_AUTH=(--key "$LOCAL_STUDIO_NOTARY_KEY" --key-id "$LOCAL_STUDIO_NOTARY_KEY_ID" --issuer "$LOCAL_STUDIO_NOTARY_ISSUER")
else
  NOTARY_AUTH=(--keychain-profile "${LOCAL_STUDIO_NOTARY_PROFILE:-notary}")
fi
notary_ready() { xcrun notarytool history "${NOTARY_AUTH[@]}" >/dev/null 2>&1; }
developer_id() { security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/ {print $2; exit}'; }

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
  [[ -n "$(developer_id)" ]] || fail "no Developer ID Application certificate in the keychain"
  notary_ready || fail "no working notarization credentials (${NOTARY_AUTH[*]}): see docs/RELEASING.md"
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

ID=$(developer_id)
NOTARIZE=0
if [[ -n "$ID" ]] && notary_ready; then NOTARIZE=1; fi
if (( NOTARIZE )); then
  # The app first, so the copy people drag to Applications carries its own ticket (it opens even offline).
  echo "==> Notarizing the app (a few minutes)"
  ZIP="$ROOT/build/Local-Studio-notarize.zip"
  rm -f "$ZIP"; ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" "${NOTARY_AUTH[@]}" --wait
  xcrun stapler staple "$APP"
  rm -f "$ZIP"
fi

STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG" "$DMG.sha256"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Local Studio.app"
ln -s /Applications "$STAGE/Applications"
echo "==> Creating $DMG"
hdiutil create -volname "Local Studio" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG" >/dev/null
rm -rf "$STAGE"

if [[ -n "$ID" ]]; then codesign --force --sign "$ID" --timestamp "$DMG"; fi
if (( NOTARIZE )); then
  echo "==> Notarizing the DMG (a few minutes)"
  xcrun notarytool submit "$DMG" "${NOTARY_AUTH[@]}" --wait
  xcrun stapler staple "$DMG"
  # Check it the way a user's Mac will.
  xcrun stapler validate "$DMG"
  spctl -a -vv -t open --context context:primary-signature "$DMG"
fi
# Assess after notarization, so a new Developer ID build can obtain its ticket first.
assess_distribution
(cd "$ROOT/build" && shasum -a 256 "${DMG:t}") > "$DMG.sha256"
SUM=$(awk '{print $1}' "$DMG.sha256")
if [[ -n "$(changelog_entry | tr -d '[:space:]')" ]]; then
  { changelog_entry; print; print "**Download** \`${DMG:t}\`, open it and drag Local Studio to Applications. Signed with a Developer ID and notarized by Apple."
    print; print "SHA-256: \`$SUM\`"; } > "$NOTES"
fi
if (( NOTARIZE )); then
  echo "Built $DMG (signed and notarized), $(du -h "$DMG" | cut -f1)"
elif [[ -n "$ID" ]]; then
  echo "Built $DMG (signed but NOT notarized: store notarization credentials, see docs/RELEASING.md)"
else
  echo "Built $DMG (not signed with a Developer ID: it opens without warnings only on your own Macs)"
fi

if (( PUBLISH )); then
  (( NOTARIZE )) || fail "the DMG was not notarized; nothing was published"
  echo "==> Publishing v$VERSION to $REPO"
  gh release create "v$VERSION" "$DMG" "$DMG.sha256" --repo "$REPO" --target "$(git -C "$ROOT" rev-parse HEAD)" \
    --title "Local Studio $VERSION" --notes-file "$NOTES"
  echo "Published: https://github.com/$REPO/releases/tag/v$VERSION"
fi
