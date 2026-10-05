#!/bin/zsh
# Removes Local Studio's engine, image tool and models.
#   ./uninstall.sh               asks for confirmation; keeps this folder (images, chats)
#   ./uninstall.sh --remove-app  also deletes this folder
#   ./uninstall.sh --yes         no prompt
APP_DIR="${0:A:h}"
YES=0; REMOVE_APP=0
for a in "$@"; do
  case $a in --yes|-y) YES=1;; --remove-app) REMOVE_APP=1;; esac
done
if (( ! YES )); then
  echo "This stops Local Studio, uninstalls mlx-dspark, mflux and llama.cpp, and deletes every model it downloaded."
  (( REMOVE_APP )) && echo "It also deletes $APP_DIR (your images and chats)."
  read "ans?Continue? [y/N] "; [[ $ans == [yY]* ]] || exit 1
fi

echo "==> Stopping servers"
"$APP_DIR/stop.sh" >/dev/null 2>&1

echo "==> Uninstalling tools"
UV=$(command -v uv || echo ~/.local/bin/uv)
$UV tool uninstall mlx-dspark 2>/dev/null
$UV tool uninstall mflux 2>/dev/null
if command -v brew >/dev/null && brew list llama.cpp >/dev/null 2>&1; then
  echo "==> Uninstalling llama.cpp"
  brew uninstall llama.cpp >/dev/null 2>&1
fi

echo "==> Deleting models"
APP_DIR="$APP_DIR" python3 - <<'PY'
import json, os, shutil
from pathlib import Path
app = Path(os.environ["APP_DIR"])
hub = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache/huggingface/hub"))
repos = {"Qwen/Qwen-Image-2.1"}
try:
    cat = json.loads((app / "catalog.json").read_text())
    entries = cat["chat"] + cat["image"]
except Exception:
    entries = []
try:
    entries += json.loads((app / "data/settings.json").read_text()).get("custom", [])
except Exception:
    pass
for m in entries:
    repos.add(m["repo"])
    if m.get("drafter"):
        repos.add(m["drafter"])
def files(d):
    out = set()
    for p in d.rglob("*"):
        try:
            r = p.resolve()
            if r.is_file(): out.add(r)
        except OSError: pass
    return out
targets = [hub / ("models--" + r.replace("/", "--")) for r in sorted(repos)]
targets = [t for t in targets if t.exists()]
keep = set()
for d in hub.glob("models--*"):
    if d not in targets: keep |= files(d)
freed = 0
for d in targets:
    for f in (files(d) | set(d.rglob("*.incomplete"))) - keep:
        if hub in f.parents and f.is_file():
            freed += f.stat().st_size; f.unlink(missing_ok=True)
    shutil.rmtree(d, ignore_errors=True)
    print("   removed", d.name)
print(f"   freed {freed / 2**30:.1f} GB")
PY
rm -rf ~/.cache/mlx_dspark
if [[ -d "/Applications/Local Studio.app" ]]; then
  echo "==> Removing /Applications/Local Studio.app"
  rm -rf "/Applications/Local Studio.app"
fi

if (( REMOVE_APP )); then
  echo "==> Deleting $APP_DIR"
  rm -rf "$APP_DIR"
fi
echo "Uninstall complete."
