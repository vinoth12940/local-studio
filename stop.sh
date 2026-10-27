#!/bin/zsh
# Stops the engines and the UI and frees their memory.
CODE="${0:A:h}"
STATE="${LOCAL_STUDIO_HOME:-$CODE}"
STATE="${STATE:a}"          # absolute, as the agent daemon's socket path is
for name in llm vlm gguf search voice ui; do
  if [[ -f "$STATE/logs/$name.pid" ]]; then
    kill $(cat "$STATE/logs/$name.pid") 2>/dev/null && echo "stopped $name"
    rm -f "$STATE/logs/$name.pid"
  fi
done
pkill -f "^[^ ]*python[^ ]* [^ ]*mlx-dspark serve" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* -m mlx_vlm server" 2>/dev/null
pkill -f "^[^ ]*llama-server .*--port 8082" 2>/dev/null
pkill -f "^[^ ]*uv run .*python search.py --port 8083" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* search.py --port 8083" 2>/dev/null
pkill -f "^[^ ]*uv run .*python voice.py --port 8084" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* voice.py --port 8084" 2>/dev/null
pkill -f "cua-driver serve --socket $STATE/data/agent/cua.sock" 2>/dev/null
pkill -f "^[^ ]*uv run .*uvicorn app:app .*--port 7860" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* [^ ]*uvicorn app:app .*--port 7860" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* [^ ]*mflux-generate-" 2>/dev/null
true
