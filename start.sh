#!/bin/zsh
# Starts the Local Studio UI (port 7860) and the engine for the active chat model.
#   ./start.sh          UI + engine
#   ./start.sh --ui     UI only (start the engine later from the app to save memory)
# Code lives next to this script; user data goes to $LOCAL_STUDIO_HOME (the Mac app sets it to
# ~/Library/Application Support/Local Studio) or, when unset, to this folder.
CODE="${0:A:h}"
STATE="${LOCAL_STUDIO_HOME:-$CODE}"
export LOCAL_STUDIO_HOME="$STATE"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$STATE/logs"
cd "$CODE"
if ! curl -sf http://127.0.0.1:7860/api/system >/dev/null; then
  UV=$(command -v uv || echo ~/.local/bin/uv)
  nohup $UV run --no-project --python 3.12 --with fastapi --with uvicorn --with httpx \
    --with python-multipart --with pillow --with pypdf --with pypdfium2 \
    --with python-docx --with openpyxl uvicorn app:app --app-dir "$CODE" \
    --host 127.0.0.1 --port 7860 > "$STATE/logs/ui.log" 2>&1 &
  echo $! > "$STATE/logs/ui.pid"
  echo "UI starting at http://127.0.0.1:7860"
  for i in {1..120}; do curl -sf http://127.0.0.1:7860/api/system >/dev/null && break; sleep 0.5; done
fi
curl -sf -X POST http://127.0.0.1:7860/api/search/start >/dev/null      # does nothing unless Search is turned on
if [[ "${1:-}" != "--ui" ]]; then
  curl -sf -X POST http://127.0.0.1:7860/api/engine/start >/dev/null && echo "Engine starting (active model)"
fi
true
