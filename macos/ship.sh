#!/bin/zsh
# One command for a Local Studio release: the steps 1.7.0 to 1.7.5 were shipped with by hand.
#
#   macos/ship.sh 1.7.6 "a short summary"            check, bump, retake the README screenshots, run every check, open and
#                                                     merge the release PR, publish the notarized DMG, install it here
#   macos/ship.sh 1.7.6 "a short summary" --dry-run  only the checks that can stop a release before anything changes
#   macos/ship.sh --install                          only the last step: build/Local Studio.app into /Applications
#   macos/ship.sh --screenshots                      only retake the README screenshots (any UI change; nothing committed)
#
#   --no-screenshots   keep the README screenshots (a release whose UI did not change)
#   --no-install       stop after publishing
#
#   SHIP_COMMIT_TRAILER  appended to the release commit's message (e.g. a Co-Authored-By line)
#   SHIP_PR_FOOTER       appended to the release PR's description
#   LOCAL_STUDIO_NOTARY_ENV and the rest: release.sh's notarization credentials (see the top of macos/release.sh)
#
# Before it: the work is merged into main as pushed, and CHANGELOG.md (local) has a dated "## 1.7.6 (YYYY-MM-DD)" entry.
# release.sh publishes that entry as the release notes, so it lists everything since the last published release.
# It stops at the first failure and forces nothing: a version that exists or is not newer, a dirty or unpushed main, no
# CHANGELOG entry, a dependency audit finding (bump that pin through its own PR, then run this again), a failing check, or
# Local Studio still running when it is time to install (quit it, then run macos/ship.sh --install).
# The README screenshots come from an isolated demo server (macos/readme_screenshots.py: its own home, ports 7872, 8096,
# 8097 and 8087, attach-only), never from the Local Studio you use.
set -e
SELF="${0:A}"; HERE="${SELF:h}"; ROOT="${HERE:h}"      # $0 is the function's name inside a zsh function
APP="/Applications/Local Studio.app"; BUILT="$ROOT/build/Local Studio.app"
DEMO_PORT=7872; DEMO_SEARCH_PORT=8096; DEMO_VOICE_PORT=8097; DEMO_DECIDE_PORT=8087
fail() { echo "ship: $*" >&2; exit 1; }
usage() { sed -n '3,10p' "$SELF" | sed 's/^# \{0,1\}//' >&2; exit 2; }
plist() { /usr/libexec/PlistBuddy -c "Print $2" "$1/Contents/Info.plist" 2>/dev/null; }
app_running() {                       # the app, its backend, or anything answering on its port
  pgrep -f "Local Studio.app/Contents" >/dev/null || curl -s -m 2 -o /dev/null "http://127.0.0.1:7860/"
}

install_app() {
  [[ -d "$BUILT" ]] || fail "there is no build/Local Studio.app: publish first"
  local v="$(plist "$BUILT" CFBundleShortVersionString) (build $(plist "$BUILT" CFBundleVersion))"
  echo "==> Installing $v into /Applications"
  spctl -a "$BUILT" 2>/dev/null || fail "build/Local Studio.app is not notarized: install what release.sh published"
  if app_running; then
    echo "ship: Local Studio is running. Quit it (Local Studio → Quit Local Studio), then run: macos/ship.sh --install" >&2
    exit 3
  fi
  rm -rf "$APP"
  ditto "$BUILT" "$APP"
  codesign --verify --deep --strict "$APP" || fail "the installed app's signature does not verify"
  spctl -a "$APP" || fail "macOS does not accept the installed app"
  echo "   $APP: $(plist "$APP" CFBundleShortVersionString) (build $(plist "$APP" CFBundleVersion)), notarized"
}

screenshots_ready() {
  [[ -f "$ROOT/build/readme-state/data/settings.json" && -d "$ROOT/build/search_samples/files" ]] \
    || fail "the README demo is not set up: see the top of macos/readme_screenshots.py (or pass --no-screenshots)"
  [[ -d "/Applications/Google Chrome.app" ]] || fail "readme_screenshots.py drives Google Chrome, which is not installed"
  for port in $DEMO_PORT $DEMO_SEARCH_PORT; do
    curl -s -m 2 -o /dev/null "http://127.0.0.1:$port/" && fail "port $port is in use: the demo server needs it"
  done
  return 0
}
retake_screenshots() {                # every README screenshot, from the isolated demo server
  screenshots_ready
  echo "==> Retaking the README screenshots (demo server on $DEMO_PORT)"
  local log="$ROOT/build/ship-demo.log"
  (cd "$ROOT" && env LOCAL_STUDIO_HOME=build/readme-state LOCAL_STUDIO_SEARCH_PORT=$DEMO_SEARCH_PORT \
      LOCAL_STUDIO_VOICE_PORT=$DEMO_VOICE_PORT LOCAL_STUDIO_DECIDE_PORT=$DEMO_DECIDE_PORT \
      LOCAL_STUDIO_SEARCH_ROOTS="$ROOT/build/search_samples/files" LOCAL_STUDIO_UI_PORT=$DEMO_PORT LOCAL_STUDIO_EVAL_ATTACH=1 \
      "$UV" run -q --no-project --python 3.12 --with-requirements requirements-app.txt \
      uvicorn app:app --host 127.0.0.1 --port $DEMO_PORT > "$log" 2>&1) &
  DEMO=$!
  trap stop_demo EXIT
  for i in {1..120}; do curl -s -m 2 -o /dev/null "http://127.0.0.1:$DEMO_PORT/api/features" && break; sleep 1; done
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$DEMO_PORT/api/features" || fail "the demo server did not start (see $log)"
  curl -s -m 30 -X POST "http://127.0.0.1:$DEMO_PORT/api/search/start" >/dev/null
  for i in {1..90}; do                # the files screenshot needs search ready ("idle": indexed and waiting)
    curl -s -m 3 "http://127.0.0.1:$DEMO_PORT/api/search/status" | grep -q '"state": *"idle"' && break; sleep 2
  done
  (cd "$ROOT" && "$UV" run -q --no-project --python 3.12 --with playwright==1.63.0 python macos/readme_screenshots.py)
  stop_demo; trap - EXIT
}
stop_demo() {
  [[ -n "${DEMO:-}" ]] && pkill -P $DEMO 2>/dev/null; kill ${DEMO:-} 2>/dev/null
  pkill -f "uvicorn app:app --host 127.0.0.1 --port $DEMO_PORT" 2>/dev/null; pkill -f "search.py --port $DEMO_SEARCH_PORT" 2>/dev/null
  return 0
}

UV=$(command -v uv || echo ~/.local/bin/uv)
VERSION=""; SUMMARY=""; DRY=0; SHOTS=1; INSTALL=1
for arg in "$@"; do
  case "$arg" in
    --install) install_app; exit 0 ;;
    --screenshots) retake_screenshots; echo "ship: screenshots retaken in docs/screenshots (not committed)"; exit 0 ;;
    --dry-run) DRY=1 ;;
    --no-screenshots) SHOTS=0 ;;
    --no-install) INSTALL=0 ;;
    -*) usage ;;
    *) if [[ -z "$VERSION" ]]; then VERSION="$arg"; elif [[ -z "$SUMMARY" ]]; then SUMMARY="$arg"; else usage; fi ;;
  esac
done
[[ "$VERSION" =~ '^[0-9]+\.[0-9]+\.[0-9]+$' && -n "$SUMMARY" ]] || usage
cd "$ROOT"
CURRENT=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' app.py)
changelog_entry() { awk -v v="$VERSION" 'index($0, "## " v " ") == 1 { on = 1; next } on && /^## / { exit } on' CHANGELOG.md; }

echo "==> Checking that $VERSION can ship"
command -v gh >/dev/null || fail "the GitHub CLI (gh) is not installed"
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) || fail "gh cannot read the repository (gh auth login?)"
[[ "$(git branch --show-current)" == main ]] || fail "ship from main, not $(git branch --show-current)"
[[ -z "$(git status --porcelain)" ]] || fail "the working tree has uncommitted changes"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "main is not the same as origin/main: pull or push first"
[[ "$VERSION" != "$CURRENT" && "$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | tail -1)" == "$VERSION" ]] \
  || fail "$VERSION is not newer than $CURRENT"
if gh release view "v$VERSION" --repo "$REPO" >/dev/null 2>&1; then fail "v$VERSION is already released"; fi
heading=$(grep -m1 "^## $VERSION " CHANGELOG.md 2>/dev/null || true)
[[ -n "$heading" && "$heading" != *unreleased* ]] || fail "CHANGELOG.md needs a dated '## $VERSION (YYYY-MM-DD)' entry: it becomes the release notes"
[[ -n "$(changelog_entry | tr -d '[:space:]')" ]] || fail "the CHANGELOG.md entry for $VERSION is empty"
python3 "$HERE/audit_dependencies.py" --uv "$UV" || fail "a pinned dependency has a known vulnerability: bump it through its own PR, then run this again"
(( SHOTS )) && screenshots_ready
if (( DRY )); then echo "ship: $CURRENT → $VERSION can ship (nothing was changed)"; exit 0; fi

BRANCH="release-$VERSION"
echo "==> Bumping $CURRENT → $VERSION on $BRANCH"
git switch -q -c "$BRANCH"
sed -i '' "s/^APP_VERSION = \"$CURRENT\"$/APP_VERSION = \"$VERSION\"/" app.py
sed -i '' "s/Local-Studio-${CURRENT//./\\.}\\.dmg/Local-Studio-$VERSION.dmg/g" README.md
grep -q "^APP_VERSION = \"$VERSION\"$" app.py || fail "app.py was not bumped"
grep -q "Local-Studio-$VERSION.dmg" README.md || fail "the README's DMG line was not bumped"

(( SHOTS )) && retake_screenshots

echo "==> Running every check (macos/build.sh)"
TMPDIR=/tmp "$HERE/build.sh" > "$ROOT/build/ship-checks.log" 2>&1 || { tail -30 "$ROOT/build/ship-checks.log" >&2; fail "a check failed (build/ship-checks.log)"; }
grep -E "checks OK|^Done" "$ROOT/build/ship-checks.log" | sed 's/^/   /'

echo "==> Release PR"
git add app.py README.md docs/screenshots
git commit -q -F - <<EOF
Local Studio $VERSION: $SUMMARY

$(changelog_entry | sed '/^$/N;/^\n$/D')
${SHIP_COMMIT_TRAILER:+
$SHIP_COMMIT_TRAILER}
EOF
git push -q -u origin "$BRANCH"
PR_URL=$(gh pr create --base main --head "$BRANCH" --title "Local Studio $VERSION: $SUMMARY" \
  --body "$(changelog_entry)${SHIP_PR_FOOTER:+

$SHIP_PR_FOOTER}")
echo "   $PR_URL"
PR=${PR_URL##*/}
gh pr merge "$PR" --merge --subject "Local Studio $VERSION: $SUMMARY (#$PR)" || fail "PR #$PR did not merge: merge it, then run macos/release.sh --publish"
git switch -q main
git pull -q --ff-only origin main
git branch -q -d "$BRANCH" 2>/dev/null || true

echo "==> Publishing v$VERSION (macos/release.sh --publish: audit, build, notarize, release)"
TMPDIR=/tmp "$HERE/release.sh" --publish

(( INSTALL )) && install_app
echo "ship: Local Studio $VERSION is out: https://github.com/$REPO/releases/tag/v$VERSION"
