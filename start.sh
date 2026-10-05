#!/bin/zsh
# Starts the Qwen3.8 + DFlash engine (port 8080) and the Local Studio UI (port 7860).
#   ./start.sh          engine + UI
#   ./start.sh --ui     UI only (start the engine later from the UI to save memory)
cd "${0:A:h}"
mkdir -p logs
if [[ "${1:-}" != "--ui" ]] && ! curl -sf http://127.0.0.1:8080/health >/dev/null; then
  nohup ~/.local/bin/mlx-dspark serve --model mlx-community/Qwen3.8-27B-4bit \
    --kv-bits 8 --context-window 32768 --host 127.0.0.1 --port 8080 \
    > logs/llm.log 2>&1 &
  echo $! > logs/llm.pid
  echo "Engine starting (log: logs/llm.log)"
fi
if ! curl -sf http://127.0.0.1:7860/api/system >/dev/null; then
  UV=$(command -v uv || echo ~/.local/bin/uv)
  nohup $UV run --python 3.12 --with fastapi --with uvicorn --with httpx \
    --with python-multipart --with pillow --with pypdf --with pypdfium2 \
    --with python-docx --with openpyxl uvicorn app:app --host 127.0.0.1 --port 7860 \
    > logs/ui.log 2>&1 &
  echo $! > logs/ui.pid
  echo "UI starting at http://127.0.0.1:7860"
fi
