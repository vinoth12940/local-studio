#!/bin/zsh
# Local Studio setup: checks this Mac and installs the engines. Used by the Mac app's
# first-run screen and safe to run by hand. Prints a simple line protocol for the app:
#   @check <ok|warn|fail|missing> <name> <detail>   @step <n> <total> <title>
#   @log <text>   @done   @error <text>
#
#   setup.sh --check     report status; exit 0 = ready, 1 = needs install, 2 = unsupported Mac
#   setup.sh --install   install everything that's missing (no models; pick those in the app)
#   setup.sh --llama     install or refresh the app-managed llama.cpp build
#   setup.sh --search    install the search components (run by the app when Search is turned on)
#   setup.sh --migrate   copy images/chats/settings from ~/local-studio (first run of the app)
set -u
CODE="${0:A:h}"
SUPPORT="$HOME/Library/Application Support/Local Studio"
STATE="${LOCAL_STUDIO_HOME:-$SUPPORT}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export UV_NO_MODIFY_PATH=1              # never touch the user's shell profile
UV="$HOME/.local/bin/uv"
[[ -x "$UV" ]] || UV=$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")
APP_DEPS=(--with fastapi --with uvicorn --with httpx --with python-multipart --with pillow
          --with pypdf --with pypdfium2 --with python-docx --with openpyxl)

say() { print -r -- "$*"; }
log_lines() { while IFS= read -r line; do say "@log ${line//$'\r'/}"; done; }

NO_BREW="${LOCAL_STUDIO_NO_BREW:-}"     # testing: behave as if Homebrew weren't installed
llama_server() {
  local cands=("$SUPPORT/llama.cpp/llama-server")
  [[ -z "$NO_BREW" ]] && cands=("$(command -v llama-server 2>/dev/null)" /opt/homebrew/bin/llama-server $cands)
  for p in $cands; do
    [[ -n "$p" && -x "$p" ]] && { print -r -- "$p"; return 0; }
  done
  return 1
}

check() {
  local rc=0 arch mac major ram_gb free_gb
  arch=$(uname -m); mac=$(sw_vers -productVersion); major=${mac%%.*}
  ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  free_gb=$(( $(df -k "$HOME" | awk 'NR==2 {print $4}') / 1048576 ))
  if [[ $arch != arm64 ]]; then say "@check fail Mac Needs Apple Silicon (M1 or newer)"; return 2; fi
  say "@check ok Mac Apple Silicon · macOS $mac"
  if (( major < 14 )); then say "@check fail macOS Needs macOS 14 or newer"; return 2; fi
  if (( ram_gb < 16 )); then say "@check warn Memory ${ram_gb} GB: only the smallest models will run"
  else say "@check ok Memory ${ram_gb} GB"; fi
  if (( free_gb < 20 )); then say "@check warn Disk ${free_gb} GB free: models need 3–25 GB each"
  else say "@check ok Disk ${free_gb} GB free"; fi
  [[ -x "$UV" ]] && say "@check ok uv Installed" || { say "@check missing uv Python package manager"; rc=1; }
  [[ -x "$HOME/.local/bin/mlx-dspark" ]] && say "@check ok mlx-dspark Installed" || { say "@check missing mlx-dspark Chat engine (MLX)"; rc=1; }
  [[ -x "$HOME/.local/bin/mflux-generate-qwen-2.1" ]] && say "@check ok mflux Installed" || { say "@check missing mflux Image engine"; rc=1; }
  llama_server >/dev/null && say "@check ok llama.cpp Installed" || { say "@check missing llama.cpp GGUF engine"; rc=1; }
  return $rc
}

install_llama() {
  if [[ -z "$NO_BREW" ]] && command -v brew >/dev/null 2>&1; then
    brew list llama.cpp >/dev/null 2>&1 && brew upgrade llama.cpp 2>&1 | log_lines || brew install llama.cpp 2>&1 | log_lines
    return 0
  fi
  # No Homebrew: use llama.cpp's official macOS arm64 build from GitHub.
  local url tmp
  url=$("$UV" run --no-project --python 3.12 python - <<'PY'
import json, urllib.request
rels = json.load(urllib.request.urlopen("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15", timeout=30))
for r in rels:
    for a in r.get("assets", []):
        if a["name"].endswith("-bin-macos-arm64.tar.gz") or a["name"].endswith("-bin-macos-arm64.zip"):
            print(a["browser_download_url"]); raise SystemExit
PY
)
  [[ -n "$url" ]] || { say "@error Couldn't find a llama.cpp macOS build"; return 1; }
  say "@log Downloading ${url##*/}"
  tmp=$(mktemp -d)
  curl -fsSL "$url" -o "$tmp/llama.archive" || { say "@error llama.cpp download failed"; return 1; }
  mkdir -p "$tmp/x"
  if [[ $url == *.zip ]]; then ditto -x -k "$tmp/llama.archive" "$tmp/x"; else tar -xzf "$tmp/llama.archive" -C "$tmp/x"; fi
  local bin; bin=$(find "$tmp/x" -name llama-server -type f | head -1)
  [[ -n "$bin" ]] || { say "@error llama-server not found in the download"; return 1; }
  rm -rf "$SUPPORT/llama.cpp"; mkdir -p "$SUPPORT/llama.cpp"
  cp -R "${bin:h}/." "$SUPPORT/llama.cpp/"
  xattr -dr com.apple.quarantine "$SUPPORT/llama.cpp" 2>/dev/null
  rm -rf "$tmp"
  "$SUPPORT/llama.cpp/llama-server" --version 2>&1 | head -1 | log_lines
}

install_search() {
  # Search's Python dependencies (torchvision and a pinned mlx-vlm). Not part of --install: they
  # are fetched only when the user turns Search on.
  [[ -x "$UV" ]] || { say "@error uv is not installed"; return 1; }
  say "@log Installing search components"
  ( cd "$CODE" && "$UV" run --no-project --python 3.12 --with-requirements search-requirements.txt python -c \
      "import mlx_vlm, torchvision, soundfile, pillow_heif, pypdf, cryptography, pypdfium2, docx, openpyxl, fastapi, uvicorn; from transformers import EmbeddingGemma2Processor; print('search components OK')" ) 2>&1 | log_lines
  [[ ${pipestatus[1]} -eq 0 ]] || { say "@error Search components failed to install"; return 1; }
  mkdir -p "$STATE/data"
  shasum -a 256 "$CODE/search-requirements.txt" | awk '{print $1}' > "$STATE/data/search-deps.ok"
  say "@done"
}

migrate() {
  # Bring images, chats and settings from a source checkout (~/local-studio) on first run.
  local old="$HOME/local-studio"
  [[ "$STATE" == "$old" || ! -d "$old/data" ]] && return 0
  [[ -e "$STATE/data/settings.json" || -n "$(ls -A "$STATE/data/chats" 2>/dev/null)" ]] && return 0
  mkdir -p "$STATE"
  for d in gallery uploads data; do
    [[ -d "$old/$d" ]] && { mkdir -p "$STATE/$d"; cp -R "$old/$d/." "$STATE/$d/"; }
  done
  say "@log Copied your images, chats and settings from $old"
}

install_all() {
  local total=6 n=0
  step() { n=$((n + 1)); say "@step $n $total $1"; }
  step "Checking this Mac"
  check >/dev/null; [[ $? -eq 2 ]] && { check | grep '@check fail'; say "@error This Mac isn't supported"; return 2; }

  step "Installing uv (Python package manager)"
  if [[ ! -x "$UV" ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>&1 | log_lines
    UV="$HOME/.local/bin/uv"
    [[ -x "$UV" ]] || { say "@error uv install failed"; return 1; }
  else say "@log Already installed"; fi

  step "Installing the chat engine (mlx-dspark, MLX)"
  "$UV" tool install --python 3.12 --upgrade mlx-dspark 2>&1 | log_lines
  [[ -x "$HOME/.local/bin/mlx-dspark" ]] || { say "@error mlx-dspark install failed"; return 1; }

  step "Installing the image engine (mflux)"
  "$UV" tool install --python 3.12 --upgrade "mflux>=0.20" 2>&1 | log_lines
  [[ -x "$HOME/.local/bin/mflux-generate-qwen-2.1" ]] || { say "@error mflux install failed"; return 1; }

  step "Installing llama.cpp (GGUF engine)"
  if llama_server >/dev/null; then say "@log Already installed"; else install_llama || return 1; fi

  step "Preparing Local Studio"
  "$UV" run --no-project --python 3.12 "${APP_DEPS[@]}" python -c "import fastapi, uvicorn, pypdf, docx, openpyxl" 2>&1 | log_lines
  mkdir -p "$STATE"/{gallery,uploads,logs,data/chats}
  migrate
  say "@done"
}

case "${1:---check}" in
  --check) check; exit $? ;;
  --install) install_all; exit $? ;;
  --llama) install_llama; exit $? ;;
  --search) install_search; exit $? ;;
  --migrate) migrate; exit 0 ;;
  *) say "usage: setup.sh [--check|--install|--llama|--search]"; exit 64 ;;
esac
