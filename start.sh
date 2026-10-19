#!/bin/zsh
# Starts the Local Studio UI (port 7860) and the engine for the active chat model.
#   ./start.sh          UI + engine
#   ./start.sh --ui     UI only (start the engine later from the app to save memory)
# Code lives next to this script; user data goes to $LOCAL_STUDIO_HOME (the Mac app sets it to
# ~/Library/Application Support/Local Studio) or, when unset, to this folder.
CODE="${0:A:h}"
STATE="${LOCAL_STUDIO_HOME:-$CODE}"
export LOCAL_STUDIO_HOME="$STATE"
# Runtime imports must not add __pycache__ files inside the signed app bundle.
export PYTHONDONTWRITEBYTECODE=1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# A dev port is opt-in; the production launch/stop patterns retain port 7860.
UI_PORT="${LOCAL_STUDIO_UI_PORT:-7860}"
UI_URL="http://127.0.0.1:$UI_PORT"
mkdir -p "$STATE/logs"
cd "$CODE"
if ! curl -sf "$UI_URL/api/system" >/dev/null; then
  UV=$(command -v uv || echo ~/.local/bin/uv)
  nohup $UV run --no-project --python 3.12 --with-requirements "$CODE/requirements-app.txt" \
    uvicorn app:app --app-dir "$CODE" \
    --host 127.0.0.1 --port "$UI_PORT" > "$STATE/logs/ui.log" 2>&1 &
  echo $! > "$STATE/logs/ui.pid"
  echo "UI starting at $UI_URL"
  for i in {1..120}; do curl -sf "$UI_URL/api/system" >/dev/null && break; sleep 0.5; done
fi
AUTH=()
if [[ -r "$STATE/data/.api-token" ]]; then
  AUTH=(-H "Authorization: Bearer $(<"$STATE/data/.api-token")")
fi
if [[ -r "$STATE/data/.api-bootstrap" ]]; then
  echo "Open Local Studio: $UI_URL/?t=$(<"$STATE/data/.api-bootstrap")"
fi
curl -sf "${AUTH[@]}" -X POST "$UI_URL/api/search/start" >/dev/null      # does nothing unless Search is turned on
if [[ "${1:-}" != "--ui" ]]; then
  curl -sf "${AUTH[@]}" -X POST "$UI_URL/api/engine/start" >/dev/null && echo "Engine starting (active model)"
fi
true
