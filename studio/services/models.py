"""Models on disk, the catalog and downloads.

Moved from app.py in the refactor's P1 split (docs/refactor-progress.json).
"""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import HTTPException

from studio.infra.logs import open_log
from studio import config
from studio.config import CATALOG, HF_CLI, HF_HUB, LOGS
from studio.storage.settings import load_settings, save_settings


def chat_models():
    return CATALOG["chat"] + [m for m in load_settings()["custom"]]


def chat_model(mid=None):
    mid = mid or load_settings()["chat_model"]
    m = next((m for m in chat_models() if m["id"] == mid), None)
    return m or next(m for m in CATALOG["chat"] if m.get("default"))


def image_model(mid=None):
    mid = mid or load_settings()["image_model"]
    return next((m for m in CATALOG["image"] if m["id"] == mid), CATALOG["image"][0])


def search_model():
    return CATALOG["search"][0]


def all_models():
    """Everything the app can download: chat models (catalog and custom), image models, the search embedder."""
    return chat_models() + CATALOG["image"] + CATALOG["search"]


# ---------------------------------------------------------------- models on disk

def repo_dir(repo: str) -> Path:
    return HF_HUB / ("models--" + repo.replace("/", "--"))


def _repo_files(d: Path) -> set[Path]:
    """Every real file a cached repo uses. Snapshot symlinks resolve into the repo's own
    blobs/ or the hub-wide shared blobs/ store, so resolve them all."""
    files = set()
    for p in d.rglob("*"):
        try:
            r = p.resolve()
            if r.is_file():
                files.add(r)
        except OSError:
            pass
    return files


def _repo_for_dir(name: str) -> str:
    return name[len("models--"):].replace("--", "/", 1)


def snapshot_file(repo, name):
    """Path of `name` inside the newest snapshot of a cached repo, or None."""
    snaps = sorted((repo_dir(repo) / "snapshots").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in snaps:
        p = s / name
        if p.exists():
            return p
    return None


def native_context(m):
    """The longest window the model itself supports (tokens), or None when unknown.

    A catalog/custom `context` wins; otherwise `max_position_embeddings` from the model's cached config.json (a vision model keeps
    it under `text_config`). A GGUF repo usually ships no config.json, so it stays None and is not clamped."""
    declared = m.get("context")
    if type(declared) is int and declared > 0:
        return declared
    cfg = snapshot_file(m["repo"], "config.json") if m.get("repo") else None
    try:
        data = json.loads(cfg.read_text()) if cfg else {}
    except (OSError, ValueError):
        return None
    for section in (data.get("text_config"), data):
        value = section.get("max_position_embeddings") if isinstance(section, dict) else None
        if type(value) is int and value > 0:
            return value
    return None


def effective_context(m):
    """The window to start this model with and budget its prompts against: the setting, never beyond what the model supports.

    The setting is a ceiling for every model (default 128k), so a model whose native limit is lower runs at its own limit instead
    of being fed prompts it will silently truncate."""
    native = native_context(m)
    return min(config.CONTEXT, native) if native else config.CONTEXT


def _repo_bytes(repo):
    """Bytes on disk for a repo, counting partial downloads."""
    d = repo_dir(repo)
    if not d.exists():
        return 0
    total = sum(f.stat().st_size for f in _repo_files(d) if not f.name.endswith(".incomplete"))
    total += sum(p.stat().st_size for p in d.rglob("*.incomplete") if p.is_file())
    return total


def _incomplete(repo):
    d = repo_dir(repo)
    return d.exists() and any(d.rglob("*.incomplete"))


def model_parts(m):
    """What a catalog entry needs on disk: [(repo, [files] or None for whole repo)]."""
    if m.get("backend") == "gguf":
        return [(m["repo"], [f for f in (m.get("file"), m.get("mmproj")) if f])]
    parts = [(m["repo"], None)]
    if m.get("drafter"):
        parts.append((m["drafter"], None))
    return parts


def model_installed(m):
    for repo, files in model_parts(m):
        if _incomplete(repo):
            return False
        if files:
            if any(snapshot_file(repo, f) is None for f in files):
                return False
        elif not (repo_dir(repo).exists() and list((repo_dir(repo) / "snapshots").glob("*/*"))):
            return False
    return True


def model_bytes(m):
    return sum(_repo_bytes(repo) for repo, _ in model_parts(m))


downloads: dict[str, dict] = {}


def _download(mid, parts, kind=None):
    job = downloads[mid]
    log = open_log(LOGS / f"download-{mid}.log", "w")
    for repo, files in parts:
        cmd = [str(HF_CLI), "download", repo] + (files or [])
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        job["pid"] = proc.pid
        rc = proc.wait()
        if job.get("cancelled"):
            job.update(status="cancelled")
            return
        if rc != 0:
            job.update(status="error", returncode=rc)
            return
    job.update(status="done", finished=time.time())
    _activate_first(mid, kind)


def _activate_first(mid, kind):
    """First model of its kind on this Mac: make it the active one automatically."""
    if kind == "search":                              # the embedder is never the chat or image model
        return
    s = load_settings()
    key = "image_model" if kind == "image" else "chat_model"
    current = image_model(s[key]) if kind == "image" else chat_model(s[key])
    if not model_installed(current) or current["id"] == mid:
        s[key] = mid
        save_settings(s)


def start_download(m):
    mid = m["id"]
    if downloads.get(mid, {}).get("status") == "running":
        return downloads[mid]
    downloads[mid] = {"id": mid, "status": "running", "started": time.time()}
    kind = next((k for k in ("image", "search") if any(i["id"] == mid for i in CATALOG[k])), "chat")
    threading.Thread(target=_download, args=(mid, model_parts(m), kind), daemon=True).start()
    return downloads[mid]


def _row(m, kind, active_id, running):
    dl = downloads.get(m["id"], {})
    size = model_bytes(m)
    expected = int((m.get("size_gb") or 0) * 1e9)
    inst = model_installed(m)
    row = {**m, "kind": kind, "installed": inst, "size_bytes": size, "expected_bytes": expected,
           "downloading": dl.get("status") == "running", "download_error": dl.get("status") == "error",
           "progress": min(0.99, size / expected) if dl.get("status") == "running" and expected else None,
           "active": m["id"] == active_id and inst}
    if kind == "chat":
        row["running"] = running
    return row


def _find_any(mid):
    m = next((m for m in all_models() if m["id"] == mid), None)
    if not m:
        raise HTTPException(404, "unknown model")
    return m


def _delete_repo(repo):
    d = repo_dir(repo)
    if not d.exists():
        return 0
    mine = _repo_files(d) | {p for p in d.rglob("*.incomplete") if p.is_file()}
    others = set()
    for od in HF_HUB.glob("models--*"):
        if od != d:
            others |= _repo_files(od)
    freed = 0
    for f in mine - others:
        if HF_HUB in f.parents:                     # never touch anything outside the cache
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
    shutil.rmtree(d, ignore_errors=True)
    return freed
