#!/bin/zsh
# Local Studio installer: engines (mlx-dspark, mflux, llama.cpp) and the default models.
#   ./install.sh              install everything (~27 GB of downloads)
#   ./install.sh --no-models  install the tools only; download models later from the UI
set -e
cd "${0:A:h}"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Local Studio needs a Mac with Apple Silicon (M1 or newer)." >&2; exit 1
fi
mem_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
(( mem_gb < 32 )) && echo "Warning: ${mem_gb} GB RAM detected; 32 GB+ is recommended (48 GB tested)."

if ! command -v uv >/dev/null 2>&1 && [[ ! -x ~/.local/bin/uv ]]; then
  echo "==> Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV=$(command -v uv || echo ~/.local/bin/uv)

echo "==> Installing mlx-dspark (chat engine) and mflux (image engine)"
$UV tool install --python 3.12 --upgrade mlx-dspark
$UV tool install --python 3.12 --upgrade "mflux>=0.20"

if command -v brew >/dev/null 2>&1; then
  echo "==> Installing llama.cpp (runs GGUF models)"
  brew list llama.cpp >/dev/null 2>&1 || brew install llama.cpp
else
  echo "   Homebrew not found: skipping llama.cpp (GGUF models). Install https://brew.sh, then: brew install llama.cpp"
fi

if [[ "${1:-}" != "--no-models" ]]; then
  HF=~/.local/share/uv/tools/mlx-dspark/bin/hf
  for repo in mlx-community/Qwen3.8-27B-4bit incoai/Qwen3.8-27B-DFlash2 mlx-community/Qwen-Image-2.1-mflux-q4; do
    echo "==> Downloading $repo"
    $HF download "$repo"
  done
fi

mkdir -p logs gallery uploads data/chats
chmod +x start.sh stop.sh uninstall.sh
echo
echo "Done. Start Local Studio with:  ./start.sh"
echo "Then open http://127.0.0.1:7860"
