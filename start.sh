#!/bin/zsh
# Starts the Local Studio UI (port 7860) and the engine for the active chat model.
#   ./start.sh          UI + engine
#   ./start.sh --ui     UI only (start the engine later from the app to save memory)
cd "${0:A:h}"
mkdir -p logs
if ! curl -sf http://127.0.0.1:7860/api/system >/dev/null; then
  UV=$(command -v uv || echo ~/.local/bin/uv)
  nohup $UV run --python 3.12 --with fastapi --with uvicorn --with httpx \
    --with python-multipart --with pillow --with pypdf --with pypdfium2 \
    --with python-docx --with openpyxl uvicorn app:app --host 127.0.0.1 --port 7860 \
    > logs/ui.log 2>&1 &
  echo $! > logs/ui.pid
  echo "UI starting at http://127.0.0.1:7860"
  for i in {1..120}; do curl -sf http://127.0.0.1:7860/api/system >/dev/null && break; sleep 0.5; done
fi
if [[ "${1:-}" != "--ui" ]]; then
  curl -sf -X POST http://127.0.0.1:7860/api/engine/start >/dev/null && echo "Engine starting (active model)"
fi
