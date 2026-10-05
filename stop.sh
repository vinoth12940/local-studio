#!/bin/zsh
# Stops the engine and the UI and frees their memory.
cd "${0:A:h}"
for name in llm vlm ui; do
  if [[ -f logs/$name.pid ]]; then
    kill $(cat logs/$name.pid) 2>/dev/null && echo "stopped $name"
    rm -f logs/$name.pid
  fi
done
pkill -f "^[^ ]*python[^ ]* [^ ]*mlx-dspark serve" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* -m mlx_vlm server" 2>/dev/null
pkill -f "^[^ ]*uv run .*uvicorn app:app --host 127.0.0.1 --port 7860" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* [^ ]*uvicorn app:app --host 127.0.0.1 --port 7860" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* [^ ]*mflux-generate-qwen" 2>/dev/null
true
