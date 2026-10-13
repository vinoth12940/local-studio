# Local Search, Part 2: app integration, interface and chat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the search service from Part 1 into Local Studio: the app installs, starts and stops it, the UI gets a Search view and a Settings section, chat gets a "My files" switch, and the result ships as version 1.3.0.

**Architecture:** `app.py` is the only launcher of `search.py` and proxies `/api/search/*` to it on port 8083. The service's dependencies are installed by `setup.sh --search` the first time the user turns Search on, never during first-run setup. The UI polls one status route. Chat asks the service for passages and prepends them to the latest user message.

**Tech Stack:** FastAPI (existing `app.py`), vanilla JS in `index.html`, zsh lifecycle scripts, the Part 1 service.

**Spec:** `docs/superpowers/specs/2026-10-13-local-search-design.md`. Requires Part 1 (`docs/superpowers/plans/2026-10-13-local-search-1-service.md`) to be complete: `search.py`, `search-requirements.txt` and `build/search_samples/` must exist.

## Global Constraints

- Everything binds to `127.0.0.1` only. The app is port 7860, the search service 8083.
- User data paths resolve from `STATE`, never `ROOT`.
- Search is off by default. Nothing for Search is installed, downloaded or read until the user turns it on. Do not add search dependencies to `APP_DEPS` or to `setup.sh --install`.
- `app.py` launches the service with **relative** file names and `cwd=ROOT`. The bundled path (`/Applications/Local Studio.app/…`) contains a space, and the `pkill` patterns rely on `python search.py --port 8083` appearing literally.
- Keep `setup.sh`'s line protocol intact: `@log`, `@done`, `@error`.
- Any new runtime file must be added to the file list in `macos/build.sh`.
- The embedding model is never the chat or image model. Enabling Search must leave `chat_model` and `image_model` unchanged.
- The backend never opens a path taken from the browser. `/api/search/open` looks the path up in the index by id and requires it to match.
- **Never run `./stop.sh` or `./start.sh` during development, and never test on port 7860.** `stop.sh` kills the user's running Local Studio by process pattern, and port 7860 may be their live Mac app. Development servers in this plan run on port 7861 with a temporary `LOCAL_STUDIO_HOME` and `LOCAL_STUDIO_SEARCH_ROOTS` set to the sample folder, so no real file is read. Real folders are indexed only in Task 12's manual check, with the user.
- **A development server shares the chat engines with the user's live app** (ports 8080 to 8082 and the same process patterns). On port 7861 never click the Engine Stop button, never call `/api/engine/*`, and never send a chat to a model that is downloaded: `ensure_engine` would stop the engine the user is using. The chat checks in this plan pass `model_id` of a catalog model that is **not** downloaded, which returns an error event without loading anything. Check with `curl -s http://127.0.0.1:7861/api/models` which ids have `"installed": false` and use one of those (`lfm2.5-1.2b` on the machine this plan was written on).
- UI copy is plain and short, matching the existing Settings rows. No em dashes in UI text.
- Commit messages follow the repo's style (`app.py: …`, `index.html: …`), ending with the `Co-Authored-By` line the session provides. Continue on the `local-search` branch.
- Run every `uv run`, `curl` to localhost, and long-running command in the Terminal panel, not the sandboxed shell.

## Review Focus

1. **The user stops a chat reply mid-stream.** The busy counter must drop back to zero, or indexing stays paused forever. → Task 9, check `busy_counter`.
2. **Search is turned on before any chat model has been downloaded.** `chat_model` must not become the embedding model. → Task 8, check `embedder_is_not_a_chat_model`.
3. **The Mac app's path contains a space.** The `pkill` patterns must still find both the `uv` parent and the Python child. → Task 9, Step 12.
4. **An old chat's source chip is clicked after the index was rebuilt.** File ids are reused, so it must refuse rather than open a different file. → Task 9, check `open_requires_matching_path`.
5. **"My files" is on but Search is not running, or nothing matches.** The chat must proceed normally with the message unchanged. → Task 11, check `file_passages`.

## File Structure

| File | Change |
|---|---|
| `catalog.json` | New `search` list with the embedding model. |
| `app.py` | Model plumbing (Task 8); the search section, `/api/busy`, lifecycle and proxy routes (Task 9); `use_files` in `/api/chat` (Task 11); version (Task 12). |
| `macos/check_app.py` (new) | Checks for the app-side logic, run by `macos/build.sh`. |
| `setup.sh` | `--search` mode. |
| `start.sh`, `stop.sh`, `uninstall.sh` | Start, stop and uninstall awareness of the service. |
| `index.html` | Search view and Settings section (Task 10); "My files" pill and sources (Task 11). |
| `macos/build.sh` | Bundle list, `Info.plist` usage strings, run `check_app.py`. |
| `README.md`, `CLAUDE.md` | Documentation. |

---

### Task 8: Model plumbing and settings in `app.py`

**Files:**
- Modify: `catalog.json` (append a `search` list)
- Modify: `app.py` (imports; constants after `EDIT_MODEL`; `load_settings`; helpers after `image_model`; `_download`; `start_download`; `models`; `_find_any`; `delete_model`)
- Modify: `uninstall.sh:32`
- Create: `macos/check_app.py`
- Modify: `macos/build.sh`

**Interfaces:**
- Consumes: nothing from Part 1 except the file `search-requirements.txt`.
- Produces: `SEARCH_URL`, `SEARCH_DIR`, `SEARCH_REQS`, `SEARCH_DEPS_OK`, `SEARCH_DEFAULTS`; `search_model() -> dict`; `all_models() -> list[dict]`; `_activate_first(mid, kind)`; `load_settings()["search"]` always present with every default key; `/api/models` gains a `search` list. The check harness in `macos/check_app.py` (`@check` decorator, same shape as `check_search.py`).

- [ ] **Step 1: Write the failing checks**

Create `macos/check_app.py`:

```python
"""Build guard for the search-related logic in app.py. Run by macos/build.sh, or by hand:

    uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py [check_name ...]
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")      # never touch real user data
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")             # a Mac with no models downloaded
(TMP / "state").mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402

app.SEARCH_URL = "http://127.0.0.1:9"                     # nothing listens here: the service is "down"
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _fake_install(repo):
    d = app.HF_HUB / ("models--" + repo.replace("/", "--")) / "snapshots" / "abc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}")


@check
def settings_defaults():
    assert app.load_settings()["search"] == {"enabled": False, "folders": [], "removed": [], "paused": False, "on_battery": False}
    app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    app.SETTINGS.write_text(json.dumps({"search": {"enabled": True, "folders": ["/x"]}}))
    s = app.load_settings()["search"]
    assert s["enabled"] is True and s["folders"] == ["/x"] and s["on_battery"] is False, s
    app.SETTINGS.unlink()


@check
def embedder_is_not_a_chat_model():
    m = app.search_model()
    assert m["repo"] == "mlx-community/embeddinggemma-2-8bit" and app._find_any("embeddinggemma-2") is m
    assert m in app.all_models() and m not in app.chat_models()
    before = app.load_settings()["chat_model"]
    app._activate_first("embeddinggemma-2", "search")      # no chat model is installed on this "Mac"
    assert app.load_settings()["chat_model"] == before and not app.SETTINGS.exists()
    app._activate_first("gemma-4-12b", "chat")             # the same call for a chat model does switch: the check is live
    assert app.load_settings()["chat_model"] == "gemma-4-12b"
    app.SETTINGS.unlink()


@check
def embedder_is_a_known_model():
    seen = []
    real, app._download = app._download, lambda mid, parts, kind=None: seen.append((mid, kind, parts))
    try:
        app.start_download(app.search_model())
        app.start_download(app.CATALOG["image"][0])
        app.start_download(app.CATALOG["chat"][0])
        time.sleep(0.2)
    finally:
        app._download = real
        app.downloads.clear()
    assert [(m, k) for m, k, _ in seen] == [("embeddinggemma-2", "search"), (app.CATALOG["image"][0]["id"], "image"),
                                             (app.CATALOG["chat"][0]["id"], "chat")], seen
    assert seen[0][2] == [("mlx-community/embeddinggemma-2-8bit", None)]

    _fake_install("mlx-community/embeddinggemma-2-8bit")
    _fake_install("someone/unrelated-model")
    lib = app.models()
    assert [o["repo"] for o in lib["other"]] == ["someone/unrelated-model"], "the embedder is not an unknown cached repo"
    assert lib["search"][0]["id"] == "embeddinggemma-2" and lib["search"][0]["installed"] is True


if __name__ == "__main__":
    want = sys.argv[1:]
    for fn in CHECKS:
        if want and fn.__name__ not in want:
            continue
        fn()
        print(f"   ok {fn.__name__}")
    print("   app checks OK")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py`
Expected: FAIL with `KeyError: 'search'` from `settings_defaults`.

- [ ] **Step 3: Add the catalog entry**

In `catalog.json`, the file ends with the `image` list followed by `}`. Change the closing of the `image` list from

```json
      "blurb": "Black Forest Labs' distilled model: images in ~4 steps."
    }
  ]
}
```

to

```json
      "blurb": "Black Forest Labs' distilled model: images in ~4 steps."
    }
  ],
  "search": [
    {
      "id": "embeddinggemma-2",
      "name": "EmbeddingGemma 2",
      "repo": "mlx-community/embeddinggemma-2-8bit",
      "size_gb": 1.3,
      "blurb": "Finds your files by meaning: text, images and audio. Used by Search."
    }
  ]
}
```

- [ ] **Step 4: Add constants, settings defaults and helpers to `app.py`**

Add `import hashlib` to the imports, after `import base64`.

After the line `EDIT_MODEL = os.environ.get("EDIT_MODEL")` add:

```python
SEARCH_URL = "http://127.0.0.1:8083"                         # search.py, see the search section
SEARCH_DIR = DATA / "search"                                 # its index
SEARCH_REQS = ROOT / "search-requirements.txt"
SEARCH_DEPS_OK = DATA / "search-deps.ok"                     # written by setup.sh --search
SEARCH_DEFAULTS = {"enabled": False, "folders": [], "removed": [], "paused": False, "on_battery": False}
```

In `load_settings`, after `s.setdefault("custom", [])` add:

```python
    s["search"] = {**SEARCH_DEFAULTS, **(s.get("search") or {})}
```

After the `image_model` function add:

```python
def search_model():
    return CATALOG["search"][0]


def all_models():
    """Everything the app can download: chat models (catalog and custom), image models, the search embedder."""
    return chat_models() + CATALOG["image"] + CATALOG["search"]
```

- [ ] **Step 5: Teach the download code about the `search` kind**

In `_download`, replace

```python
    job.update(status="done", finished=time.time())
    # First model of its kind on this Mac: make it the active one automatically.
    s = load_settings()
    key = "image_model" if kind == "image" else "chat_model"
    current = image_model(s[key]) if kind == "image" else chat_model(s[key])
    if not model_installed(current) or current["id"] == mid:
        s[key] = mid
        save_settings(s)
```

with

```python
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
```

In `start_download`, replace

```python
    kind = "image" if any(i["id"] == mid for i in CATALOG["image"]) else "chat"
```

with

```python
    kind = next((k for k in ("image", "search") if any(i["id"] == mid for i in CATALOG[k])), "chat")
```

- [ ] **Step 6: Make every "known model" site use `all_models()`**

`chat_models() + CATALOG["image"]` appears four times in `app.py`: in `models()`, `_find_any()` and twice in `delete_model()`. Replace all four with `all_models()`. Confirm with `grep -c 'chat_models() + CATALOG\["image"\]' app.py`, which must print `1` (the one inside `all_models` itself).

In `models()`, change the return statement from

```python
    return {"chat": chat, "image": image, "other": other, "llama_cpp": Path(LLAMA_SERVER).exists(),
            "disk_free_bytes": shutil.disk_usage(HOME).free}
```

to

```python
    return {"chat": chat, "image": image, "search": [_row(m, "search", None, False) for m in CATALOG["search"]],
            "other": other, "llama_cpp": Path(LLAMA_SERVER).exists(), "disk_free_bytes": shutil.disk_usage(HOME).free}
```

In `delete_model()`, after the line `m = _find_any(id)` add:

```python
    if m in CATALOG["search"] and load_settings()["search"]["enabled"]:
        raise HTTPException(409, "Search is using this model. Turn Search off first.")
```

- [ ] **Step 7: Include the embedder in uninstall**

In `uninstall.sh`, replace

```python
    entries = cat["chat"] + cat["image"]
```

with

```python
    entries = cat["chat"] + cat["image"] + cat.get("search", [])
```

- [ ] **Step 8: Run the checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py`
Expected:
```
   ok settings_defaults
   ok embedder_is_not_a_chat_model
   ok embedder_is_a_known_model
   app checks OK
```

- [ ] **Step 9: Hook the check into the build and verify the existing guards**

In `macos/build.sh`, after the line that runs `check_search.py` (added in Part 1) add:

```zsh
$UV run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python "$HERE/check_app.py"
```

Run: `zsh -n macos/build.sh && zsh -n uninstall.sh && python3 -c "import ast,sys,json; ast.parse(open('app.py').read()); json.load(open('catalog.json')); print('ok')"`
Expected: `ok`

- [ ] **Step 10: Commit**

```bash
git add catalog.json app.py uninstall.sh macos/check_app.py macos/build.sh
git commit -m "app.py: download and track the search embedding model"
```

---

### Task 9: Service lifecycle, `/api/busy` and the `/api/search` routes

**Files:**
- Modify: `app.py` (the LLM proxy section above `chat()`; the end of `chat()`; a new search section before `# ---- versions & updates`)
- Modify: `setup.sh` (new `install_search` function and `--search` case)
- Modify: `start.sh`, `stop.sh`
- Modify: `macos/check_app.py`

**Interfaces:**
- Consumes: Task 8's constants and helpers; the Part 1 service routes `GET /health`, `GET /status`, `POST /search`, `POST /reload`, `GET /errors`, `GET /file/{id}`, `GET /thumb/{id}`.
- Produces:
  - `active_chats: int`, `_counted(gen)` (async generator wrapper), `GET /api/busy -> {"busy": bool}`
  - `search_deps_installed() -> bool`, `_search_running() -> bool`, `_start_search()`, `_stop_search()`
  - `GET /api/search/status` → the settings block plus `installed`, `model_installed`, `model: {name, size_gb}`, `index_on_disk`, `running`, `setup: {status, step, error}`, and, when running, every key of the service's `/status` (`state`, `paused_reason`, `current`, `model_loaded`, `roots`, `no_access`, `error`, `files_total`, `files_done`, `files_error`, `chunks`, `index_bytes`)
  - `POST /api/search` `{query, kind?, folder?, limit?}` → `{results}`
  - `POST /api/search/enable`, `POST /api/search/disable`, `POST /api/search/start`
  - `PUT /api/search/settings` `{folders?, removed?, paused?, on_battery?}` → the updated block
  - `POST /api/search/rebuild`, `DELETE /api/search/index`
  - `GET /api/search/errors`, `GET /api/search/thumb/{file_id}`
  - `POST /api/search/open` `{file_id, path, reveal?}`
  - `POST /api/search/choose-folder` → `{"path": str | null}`
  - `setup.sh --search`; `logs/search.log`, `logs/search-setup.log`, `logs/search.pid`

- [ ] **Step 1: Write the failing checks**

Add to `macos/check_app.py`, above `if __name__ == "__main__":`:

```python
@check
def busy_counter():
    async def gen():
        yield "a"
        await asyncio.sleep(30)
        yield "b"

    async def main():
        g = app._counted(gen())
        assert await g.__anext__() == "a" and app.active_chats == 1 and app.api_busy() == {"busy": True}
        await g.aclose()
        assert app.active_chats == 0 and app.api_busy() == {"busy": False}

        async def consume():
            async for _ in app._counted(gen()):
                pass

        t = asyncio.create_task(consume())                 # the user stops a reply: Starlette cancels the stream
        await asyncio.sleep(0.05)
        assert app.active_chats == 1
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert app.active_chats == 0, "a stopped reply must not leave indexing paused"

    asyncio.run(main())
    app.jobs["x"] = {"status": "running"}
    assert app.api_busy() == {"busy": True}
    app.jobs["x"] = {"status": "done"}
    assert app.api_busy() == {"busy": False}
    app.jobs.clear()


@check
def search_status_when_off():
    assert app.search_deps_installed() is False
    app.SEARCH_DEPS_OK.write_text(hashlib.sha256(app.SEARCH_REQS.read_bytes()).hexdigest() + "\n")
    assert app.search_deps_installed() is True
    app.SEARCH_DEPS_OK.write_text("an older requirements file")
    assert app.search_deps_installed() is False
    app.SEARCH_DEPS_OK.unlink()

    s = app.search_status()
    assert s["enabled"] is False and s["running"] is False and s["installed"] is False and s["index_on_disk"] is False
    assert s["model"] == {"name": "EmbeddingGemma 2", "size_gb": 1.3} and s["setup"] == {"status": "idle", "step": None, "error": None}
    assert app.search_start() == {"status": "off"}
    try:
        app.search_query({"query": "anything"})
        raise AssertionError("expected a 503")
    except HTTPException as e:
        assert e.status_code == 503 and "Settings" in e.detail


@check
def search_settings_route():
    folder = TMP / "My Folder"
    folder.mkdir()
    s = app.search_settings({"folders": [str(folder), str(folder)], "removed": ["~/Music"], "on_battery": True, "paused": 1})
    assert s == {"enabled": False, "folders": [str(folder)], "removed": [str(Path.home() / "Music")], "paused": True, "on_battery": True}
    assert app.load_settings()["search"] == s
    assert app.search_settings({"paused": False})["folders"] == [str(folder)], "keys that are not sent are kept"
    for bad in ("relative/path", str(TMP / "does-not-exist")):
        try:
            app.search_settings({"folders": [bad]})
            raise AssertionError("expected a 400")
        except HTTPException as e:
            assert e.status_code == 400
    app.SETTINGS.unlink()


@check
def open_requires_matching_path():
    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"path": "/r/now-a-different-file.txt", "name": "now-a-different-file.txt", "kind": "document", "mtime": 1.0}

    real, app.httpx.request = app.httpx.request, lambda *a, **k: Resp()
    try:
        app.search_open(file_id=5, path="/r/the-file-this-chat-cited.txt", reveal=False)
        raise AssertionError("expected a 404")
    except HTTPException as e:
        assert e.status_code == 404
    finally:
        app.httpx.request = real
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py busy_counter`
Expected: FAIL with `AttributeError: module 'app' has no attribute '_counted'`.

- [ ] **Step 3: Count open chat streams**

In `app.py`, directly above the line `@app.post("/api/chat")`, add:

```python
active_chats = 0


async def _counted(gen):
    """Count open chat streams for /api/busy. The finally also runs when the user stops a reply."""
    global active_chats
    active_chats += 1
    try:
        async for chunk in gen:
            yield chunk
    finally:
        active_chats -= 1


@app.get("/api/busy")
def api_busy():
    """Read by the search service between batches: it waits while the Mac is doing user-facing work."""
    return {"busy": active_chats > 0 or any(j.get("status") in ("queued", "running") for j in jobs.values())}


```

At the end of `chat()`, replace

```python
    return StreamingResponse(relay(), media_type="text/event-stream")
```

with

```python
    return StreamingResponse(_counted(relay()), media_type="text/event-stream")
```

- [ ] **Step 4: Add the search section**

In `app.py`, insert directly above the banner `# ---------------------------------------------------------------- versions & updates`:

```python
# ---------------------------------------------------------------- search
# search.py is a fourth process (port 8083): it indexes the user's files and answers queries. At
# ~2 GB it stays loaded next to a chat engine, so ensure_engine never touches it. It is off until
# the user turns it on; that is also when its dependencies and model are fetched.

search_job: dict = {"status": "idle"}


def search_deps_installed():
    """setup.sh --search records the requirements file it installed; a changed file means reinstall."""
    try:
        return SEARCH_DEPS_OK.read_text().strip() == hashlib.sha256(SEARCH_REQS.read_bytes()).hexdigest()
    except OSError:
        return False


def _search_running():
    try:
        return httpx.get(SEARCH_URL + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _start_search():
    if _search_running():
        return
    log = open(LOGS / "search.log", "a")
    # Relative file names with cwd=ROOT: the bundled path contains a space, which would defeat
    # stop.sh's pkill patterns.
    proc = subprocess.Popen([UV, "run", "--no-project", "--python", "3.12", "--with-requirements", SEARCH_REQS.name,
                             "python", "search.py", "--port", "8083"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}, start_new_session=True)
    (LOGS / "search.pid").write_text(str(proc.pid))


def _stop_search():
    pid = _read_pid("search")
    if pid and _pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)            # uv and its python child share the session
        except OSError:
            pass
    subprocess.run(["pkill", "-f", r"^[^ ]*uv run .*python search.py --port 8083"])
    subprocess.run(["pkill", "-f", r"^[^ ]*python[^ ]* search.py --port 8083"])
    (LOGS / "search.pid").unlink(missing_ok=True)
    for _ in range(40):                               # wait for the port to free before any restart
        if not _search_running():
            break
        time.sleep(0.25)


def _enable_search():
    """Background job behind the Search switch: install what's missing, then start the service."""
    try:
        if not search_deps_installed():
            search_job.update(step="Installing search components (about 1 GB, one time)")
            with open(LOGS / "search-setup.log", "w") as log:
                rc = subprocess.run(["/bin/zsh", str(ROOT / "setup.sh"), "--search"], stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, "LOCAL_STUDIO_HOME": str(STATE)}).returncode
            if rc != 0 or not search_deps_installed():
                raise RuntimeError("Couldn't install the search components. See logs/search-setup.log.")
        m = search_model()
        if not model_installed(m):
            search_job.update(step=f"Downloading {m['name']} ({m['size_gb']} GB)")
            start_download(m)
            while downloads[m["id"]].get("status") == "running":
                time.sleep(1)
            if not model_installed(m):
                raise RuntimeError("The model download didn't finish. Check your connection and try again.")
        search_job.update(step="Starting Search")
        s = load_settings()
        s["search"]["enabled"] = True
        save_settings(s)
        _start_search()
        for _ in range(180):                          # the first start imports PyTorch and MLX
            if _search_running():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Search didn't start. See logs/search.log.")
        search_job.update(status="done", step=None)
    except Exception as e:
        search_job.update(status="error", error=str(e), step=None)


def _search_proxy(method, path, **kw):
    try:
        r = httpx.request(method, SEARCH_URL + path, timeout=60, **kw)
    except httpx.HTTPError:
        raise HTTPException(503, "Search isn't running. Turn it on in Settings.")
    if r.status_code != 200:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise HTTPException(r.status_code, detail or "Search error")
    return r


@app.get("/api/search/status")
def search_status():
    m = search_model()
    out = {**load_settings()["search"], "installed": search_deps_installed(), "model_installed": model_installed(m),
           "model": {"name": m["name"], "size_gb": m["size_gb"]}, "index_on_disk": SEARCH_DIR.exists(), "running": False,
           "setup": {k: search_job.get(k) for k in ("status", "step", "error")}}
    try:
        r = httpx.get(SEARCH_URL + "/status", timeout=3)
        if r.status_code == 200:
            out.update(r.json(), running=True)
    except httpx.HTTPError:
        pass
    return out


@app.post("/api/search")
def search_query(body: dict = Body(...)):
    return _search_proxy("POST", "/search", json=body).json()


@app.post("/api/search/enable")
def search_enable():
    if search_job.get("status") != "running":
        search_job.clear()
        search_job.update(status="running", step="Starting")
        threading.Thread(target=_enable_search, daemon=True).start()
    return {k: search_job.get(k) for k in ("status", "step", "error")}


@app.post("/api/search/disable")
def search_disable():
    s = load_settings()
    s["search"]["enabled"] = False
    save_settings(s)
    _stop_search()
    return {"ok": True}


@app.post("/api/search/start")
def search_start():
    """Called by start.sh: bring the service up if the user has Search turned on."""
    if load_settings()["search"]["enabled"] and search_deps_installed() and model_installed(search_model()):
        _start_search()
        return {"status": "starting"}
    return {"status": "off"}


@app.put("/api/search/settings")
def search_settings(body: dict = Body(...)):
    s = load_settings()
    for key in ("folders", "removed"):
        if key in body:
            paths = [str(Path(p.strip()).expanduser()) for p in body[key] if isinstance(p, str) and p.strip()]
            if key == "folders":
                bad = next((p for p in paths if not Path(p).is_absolute() or not Path(p).is_dir()), None)
                if bad:
                    raise HTTPException(400, f"Not a folder on this Mac: {bad}")
            s["search"][key] = list(dict.fromkeys(paths))
    for key in ("paused", "on_battery"):
        if key in body:
            s["search"][key] = bool(body[key])
    save_settings(s)
    try:
        httpx.post(SEARCH_URL + "/reload", timeout=3)
    except httpx.HTTPError:
        pass
    return s["search"]


def _reset_index(restart):
    _stop_search()
    shutil.rmtree(SEARCH_DIR, ignore_errors=True)
    if restart:
        _start_search()
    return {"ok": True}


@app.post("/api/search/rebuild")
def search_rebuild():
    if not load_settings()["search"]["enabled"]:
        raise HTTPException(409, "Turn Search on first.")
    return _reset_index(restart=True)


@app.delete("/api/search/index")
def search_delete_index():
    return _reset_index(restart=load_settings()["search"]["enabled"])


@app.get("/api/search/errors")
def search_errors():
    return _search_proxy("GET", "/errors").json()


@app.get("/api/search/thumb/{file_id}")
def search_thumb(file_id: int):
    r = _search_proxy("GET", f"/thumb/{file_id}")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/search/open")
def search_open(file_id: int = Body(...), path: str = Body(...), reveal: bool = Body(False)):
    """Open or reveal an indexed file. The path comes from the index, never from the request: the
    request's path only has to match, so an id reused after a rebuild can't open a different file."""
    try:
        f = _search_proxy("GET", f"/file/{file_id}").json()
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(404, "This file is no longer in the search index.")
        raise
    if f["path"] != path or not Path(f["path"]).exists():
        raise HTTPException(404, "This file has moved or is no longer in the search index.")
    subprocess.run(["open", "-R", f["path"]] if reveal else ["open", f["path"]])
    return {"ok": True}


@app.post("/api/search/choose-folder")
def search_choose_folder():
    """The standard macOS folder chooser. `tell me to activate` brings it in front of the app window."""
    try:
        r = subprocess.run(["osascript", "-e", "tell me to activate", "-e",
                            'POSIX path of (choose folder with prompt "Choose a folder to index")'],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"path": None}
    return {"path": r.stdout.strip().rstrip("/") or None if r.returncode == 0 else None}


```

Add `Response` to the existing import line so it reads:

```python
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
```

- [ ] **Step 5: Run the app checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py`
Expected: seven `ok` lines (`settings_defaults`, `embedder_is_not_a_chat_model`, `embedder_is_a_known_model`, `busy_counter`, `search_status_when_off`, `search_settings_route`, `open_requires_matching_path`) and `app checks OK`.

- [ ] **Step 6: Add `setup.sh --search`**

In `setup.sh`, add `#   setup.sh --search    install the search components (run by the app when Search is turned on)` to the header comment after the `--llama` line.

Add this function after `install_llama`:

```zsh
install_search() {
  # Search's Python dependencies (torchvision and a pinned mlx-vlm). Not part of --install: they
  # are fetched only when the user turns Search on.
  [[ -x "$UV" ]] || { say "@error uv is not installed"; return 1; }
  say "@log Installing search components"
  ( cd "$CODE" && "$UV" run --no-project --python 3.12 --with-requirements search-requirements.txt python -c \
      "import mlx_vlm, torchvision, soundfile, pillow_heif, pypdf, pypdfium2, docx, openpyxl, fastapi, uvicorn; from transformers import EmbeddingGemma2Processor; print('search components OK')" ) 2>&1 | log_lines
  [[ ${pipestatus[1]} -eq 0 ]] || { say "@error Search components failed to install"; return 1; }
  mkdir -p "$STATE/data"
  shasum -a 256 "$CODE/search-requirements.txt" | awk '{print $1}' > "$STATE/data/search-deps.ok"
  say "@done"
}
```

In the `case` at the end, add after the `--llama` line:

```zsh
  --search) install_search; exit $? ;;
```

and change the usage line to `say "usage: setup.sh [--check|--install|--llama|--search]"; exit 64 ;;`.

- [ ] **Step 7: Verify `setup.sh --search` against a throwaway state folder**

Run: `T=$(mktemp -d); LOCAL_STUDIO_HOME="$T" ./setup.sh --search | tail -3; echo "rc=$?"; cat "$T/data/search-deps.ok"; shasum -a 256 search-requirements.txt`
Expected: `@log search components OK`, `@done`, then two identical 64-character hashes. The dependencies are already cached from Part 1, so this takes seconds.

- [ ] **Step 8: Start and stop the service with the app**

In `start.sh`, after the `fi` that closes the UI-start block and before `if [[ "${1:-}" != "--ui" ]]; then`, add:

```zsh
curl -sf -X POST http://127.0.0.1:7860/api/search/start >/dev/null      # does nothing unless Search is turned on
```

In `stop.sh`, change `for name in llm vlm gguf ui; do` to `for name in llm vlm gguf search ui; do`, and after the `llama-server` `pkill` line add:

```zsh
pkill -f "^[^ ]*uv run .*python search.py --port 8083" 2>/dev/null
pkill -f "^[^ ]*python[^ ]* search.py --port 8083" 2>/dev/null
```

- [ ] **Step 9: Syntax-check the scripts**

Run: `zsh -n setup.sh && zsh -n start.sh && zsh -n stop.sh && echo syntax-ok`
Expected: `syntax-ok`

- [ ] **Step 10: Run a development server and turn Search on through the API**

Do **not** use `./start.sh`. In one Terminal tab (it keeps running):

```bash
T=$(mktemp -d) && echo "state: $T" && LOCAL_STUDIO_HOME="$T" LOCAL_STUDIO_SEARCH_ROOTS="$PWD/build/search_samples/files" uv run -q --no-project --python 3.12 --with fastapi --with uvicorn --with httpx --with python-multipart --with pillow --with pypdf --with pypdfium2 --with python-docx --with openpyxl uvicorn app:app --host 127.0.0.1 --port 7861
```

In a second tab:

```bash
curl -s http://127.0.0.1:7861/api/search/status | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['enabled'], s['running'], s['installed'], s['setup']['status'])"
```
Expected: `False False False idle`

```bash
curl -s -X POST http://127.0.0.1:7861/api/search/enable; echo
```
Expected: `{"status":"running","step":"Starting","error":null}` (the step may already have advanced).

```bash
until curl -s http://127.0.0.1:7861/api/search/status | grep -q '"state":"idle"'; do sleep 3; done; curl -s http://127.0.0.1:7861/api/search/status | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['enabled'], s['running'], s['installed'], s['setup'], s['files_done'], s['files_error'], s['index_on_disk'])"
```
Expected (within about a minute): `True True True {'status': 'done', 'step': None, 'error': None} 19 0 True`. If it never reaches idle, read `$T/logs/search-setup.log` and `$T/logs/search.log`.

```bash
curl -s http://127.0.0.1:7861/api/search -H 'Content-Type: application/json' -d '{"query":"how much do I owe the plumber"}' | python3 -c "import json,sys; r=json.load(sys.stdin)['results'][0]; print(r['name'], r['file_id'])"
```
Expected: `invoice_acme.txt` and its numeric id.

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7861/api/search/open -H 'Content-Type: application/json' -d '{"file_id": <that id>, "path": "/not/the/indexed/path"}'
```
Expected: `404`. (Do not send the correct path here: that would open the file in an app.)

```bash
curl -s http://127.0.0.1:7861/api/busy; echo; curl -s http://127.0.0.1:7861/api/models | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['search'][0]['installed'], [o['repo'] for o in d['other'] if 'embeddinggemma-2-8bit' in o['repo']])"
```
Expected: `{"busy":false}` then `True []`.

- [ ] **Step 11: Turn Search off and delete the index through the API**

```bash
curl -s -X POST http://127.0.0.1:7861/api/search/disable; echo; sleep 2; pgrep -fl "search.py --port 8083"; curl -s http://127.0.0.1:7861/api/search/status | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['enabled'], s['running'], s['index_on_disk'])"
```
Expected: `{"ok":true}`, no `pgrep` output, then `False False True`.

```bash
curl -s -X DELETE http://127.0.0.1:7861/api/search/index; echo; curl -s http://127.0.0.1:7861/api/search/status | python3 -c "import json,sys; print(json.load(sys.stdin)['index_on_disk'])"
```
Expected: `{"ok":true}` then `False`. Leave the development server running for Task 10, or stop it with Ctrl-C.

- [ ] **Step 12: Verify the `pkill` patterns on a path with a space**

This uses `pgrep` with the two patterns from `stop.sh`. It never runs `stop.sh`.

```bash
D="$(mktemp -d)/Local Studio.app/Contents/Resources/app"; mkdir -p "$D"; printf 'import time\ntime.sleep(120)\n' > "$D/search.py"; : > "$D/search-requirements.txt"; (cd "$D" && uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --port 8083 &); sleep 5; pgrep -f "^[^ ]*uv run .*python search.py --port 8083" | wc -l; pgrep -f "^[^ ]*python[^ ]* search.py --port 8083" | wc -l; pkill -f "python search.py --port 8083"
```
Expected: `1` and `1` (leading spaces are fine). A `0` means that pattern misses the process when the app is installed in `/Applications`; fix the pattern in both `stop.sh` and `_stop_search` before continuing.

- [ ] **Step 13: Commit**

```bash
git add app.py setup.sh start.sh stop.sh macos/check_app.py
git commit -m "app.py: run the search service and proxy /api/search"
```

---

### Task 10: Search view and Settings section

**Files:**
- Modify: `index.html` (CSS; sidebar; a new `v-search` section; a Search section in `v-manage`; `showView`; `updateHeader`; `loadManage`; a new `// ---------------- search` script block; boot)

**Interfaces:**
- Consumes: the Task 9 routes. Existing helpers in `index.html`: `$`, `esc`, `human`, `icon`, `api`, `jpost`, `toast`, `confirmBox`, `store`, `showView`, `view`.
- Produces: global `sr` (the latest `/api/search/status` response, or `null`), `loadSearch()` (refreshes `sr` and the DOM), `srHome(path)`, `srLoc(locator)`. Task 11 relies on `sr` and adds one line to `loadSearch`.

- [ ] **Step 1: Add the styles**

In the `<style>` block of `index.html`, after the line that starts `.row .sub {`, add:

```css
.srbox { display: flex; gap: 10px; align-items: center; }
.srbox input { flex: 1; min-width: 0; height: 36px; border: 0; outline: 0; background: transparent; color: var(--ink); font: inherit; font-size: 15px; }
.srstatus { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px; color: var(--muted); margin: 12px 4px 8px; min-height: 30px; }
.srrow { display: flex; gap: 14px; align-items: flex-start; padding: 14px 6px; border-bottom: 1px solid var(--line); }
.srrow .th { width: 56px; height: 56px; flex: none; border-radius: 10px; border: 1px solid var(--line); display: grid; place-items: center; overflow: hidden; color: var(--muted); }
.srrow .th img { width: 100%; height: 100%; object-fit: cover; }
.srrow .nm { font-size: 14px; font-weight: 500; word-break: break-word; }
.srrow .pt { font-size: 12px; color: var(--muted); word-break: break-all; }
.srrow .sn { font-size: 13px; color: var(--ink-2); margin-top: 6px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.srrow .acts { display: flex; gap: 6px; flex: none; }
.srpath { width: 220px; height: 30px; border: 1px solid var(--line-2); border-radius: 8px; padding: 0 10px; background: var(--bg); color: var(--ink); font: inherit; font-size: 12.5px; outline: 0; }
```

- [ ] **Step 2: Add the sidebar item and the Search view**

In the sidebar, after the line

```html
      <button class="navitem" data-view="images"><svg class="i"><use href="#i-image"/></svg>Images</button>
```

add

```html
      <button class="navitem" data-view="search"><svg class="i"><use href="#i-search"/></svg>Search</button>
```

Directly above the line `    <section class="view" id="v-library">`, add:

```html
    <section class="view" id="v-search">
      <div class="scroll"><div class="page" style="max-width:880px">
        <h2>Search</h2>
        <p class="lead">Find your files by what they contain. Runs entirely on this Mac.</p>
        <div class="section" id="srOff" style="display:none">
          <div class="sh"><div><h3>Search is off</h3><p id="srOffMsg"></p></div>
            <button class="btn primary" id="srEnable">Turn on Search</button></div>
        </div>
        <div id="srOn" style="display:none">
          <div class="prompt-card" style="padding:10px 14px 10px 18px">
            <div class="srbox"><svg class="i"><use href="#i-search"/></svg>
              <input id="srQuery" placeholder="Describe what you're looking for" autocomplete="off">
              <div class="sel"><select id="srKind" title="Kind"><option value="">All</option><option value="document">Documents</option><option value="image">Images</option><option value="audio">Audio</option></select></div>
            </div>
          </div>
          <div class="srstatus" id="srStatus"></div>
          <div id="srResults"></div>
        </div>
      </div></div>
    </section>

```

- [ ] **Step 3: Add the Settings section**

In `v-manage`, directly above the block that begins

```html
        <div class="section">
          <div class="sh"><div><h3>Storage</h3><p id="diskFree">–</p></div></div>
```

add:

```html
        <div class="section">
          <div class="sh"><div><h3>Search</h3><p id="srSetSum">–</p></div>
            <label class="switch"><input type="checkbox" id="srSw"><span></span></label></div>
          <div id="srSetBody" style="display:none">
            <div id="srFolders"></div>
            <div class="row"><div class="grow"><div class="ttl">Add a folder</div><div class="sub">Choose one, or paste its path</div></div>
              <input class="srpath" id="srPath" placeholder="/Users/you/Projects" autocomplete="off">
              <button class="btn sm" id="srAddPath">Add</button><button class="btn sm" id="srChoose">Choose…</button></div>
            <div class="row"><div class="grow"><div class="ttl">Index on battery</div><div class="sub">When off, indexing waits until the Mac is plugged in</div></div>
              <label class="switch"><input type="checkbox" id="srBattery"><span></span></label></div>
            <div class="row"><div class="grow"><div class="ttl">Index</div><div class="sub" id="srIdxSub">–</div></div>
              <button class="btn sm" id="srErrBtn" style="display:none">Show unreadable files</button><button class="btn sm" id="srRebuild">Rebuild</button></div>
            <div id="srErrList" style="display:none"></div>
          </div>
          <div class="row" id="srDelRow" style="display:none"><div class="grow"><div class="ttl">Index on disk</div><div class="sub">Search is off, but its index is still stored</div></div>
            <button class="btn sm danger" id="srDelete">Delete index</button></div>
        </div>

```

- [ ] **Step 4: Hook the view into navigation**

In `showView`, after the line `if (v === "manage") loadManage();` add:

```js
  if (v === "search") { loadSearch(); setTimeout(() => $("srQuery").focus(), 0); }
```

In `updateHeader`, replace

```js
  else if (view === "library") { $("hdrTitle").textContent = "Model library"; $("hdrSub").textContent = ""; }
```

with

```js
  else if (view === "library") { $("hdrTitle").textContent = "Model library"; $("hdrSub").textContent = ""; }
  else if (view === "search") { $("hdrTitle").textContent = "Search"; $("hdrSub").textContent = "EmbeddingGemma 2"; }
```

Change the start of `loadManage` from `async function loadManage() { loadStorage(); loadVersions(); loadLibrary().then(() => {` to `async function loadManage() { loadStorage(); loadVersions(); loadSearch(); loadLibrary().then(() => {`.

- [ ] **Step 5: Add the search script block**

Directly above the line `// ---------------- boot`, add:

```js
// ---------------- search
let sr = null, srTimer = 0, srSeq = 0;
const srHome = (p) => p.replace(/^\/Users\/[^/]+/, "~");
const srLoc = (l) => !l ? "" : /^p\d+$/.test(l) ? `page ${l.slice(1)}` : /^\d+s$/.test(l) ? `${Math.floor(+l.slice(0, -1) / 60)}:${String(+l.slice(0, -1) % 60).padStart(2, "0")}` : l;
const SR_ICON = { document: "i-file", image: "i-image", audio: "i-spark", name: "i-file" };
const SR_PAUSED = { user: "Paused", busy: "Waiting while chat or image generation is running", battery: "Paused on battery power" };
const SR_OFF = "Turn it on to index your documents, images and audio. Nothing is read or downloaded until you do.";
function srStatusLine(s, buttons = true) {
  const bad = (t) => `<span style="color:var(--bad)">${esc(t)}</span>`;
  if (s.setup?.status === "running") return esc(s.setup.step || "Setting up");
  if (!s.running) return s.setup?.status === "error" ? bad(s.setup.error) : "";
  if (s.error) return bad(s.error);
  if (s.state === "starting") return "Starting";
  const done = (s.files_done + s.files_error).toLocaleString(), total = s.files_total.toLocaleString();
  const btn = (v, label) => buttons ? ` <button class="btn sm" data-srpause="${v}">${label}</button>` : "";
  if (s.state === "paused") return `${SR_PAUSED[s.paused_reason] || "Paused"} · ${done} of ${total} files` + (s.paused_reason === "user" ? btn(0, "Resume") : "");
  if (s.state === "indexing") return `Indexing ${done} of ${total} files${s.current ? " · " + esc(s.current.split("/").pop()) : ""}` + btn(1, "Pause");
  return `${s.files_done.toLocaleString()} files indexed · up to date`;
}
async function loadSearch() {
  try { sr = await api("/api/search/status"); } catch { return; }
  const on = sr.enabled && sr.running, busy = sr.setup?.status === "running";
  $("srOn").style.display = on ? "" : "none"; $("srOff").style.display = on ? "none" : "";
  $("srEnable").disabled = busy; $("srEnable").textContent = busy ? "Setting up" : "Turn on Search";
  $("srOffMsg").innerHTML = srStatusLine(sr) || SR_OFF;
  $("srStatus").innerHTML = srStatusLine(sr);
  $("srSw").checked = sr.enabled || busy; $("srSw").disabled = busy;
  $("srSetSum").innerHTML = (sr.enabled || busy ? srStatusLine(sr, false) : "") || `Find your files by what they contain. Uses about ${Math.ceil(sr.model.size_gb + 1)} GB for the model and search components.`;
  $("srSetBody").style.display = on ? "" : "none";
  $("srDelRow").style.display = !sr.enabled && !busy && sr.index_on_disk ? "" : "none";
  if (!on) return;
  $("srFolders").innerHTML = sr.roots.map((p) => `<div class="row"><div class="grow"><div class="ttl">${esc(srHome(p))}</div>${sr.no_access.includes(p) ? `<div class="sub" style="color:var(--bad)">No access. Allow it in System Settings, Privacy &amp; Security, Files and Folders.</div>` : ""}</div><button class="btn sm" data-srrm="${esc(p)}">Remove</button></div>`).join("");
  $("srBattery").checked = sr.on_battery;
  $("srIdxSub").textContent = `${sr.files_done.toLocaleString()} files · ${human(sr.index_bytes)} on disk${sr.files_error ? ` · ${sr.files_error.toLocaleString()} could not be read` : ""}`;
  $("srErrBtn").style.display = sr.files_error ? "" : "none";
}
async function srSave(patch) { try { await jpost("/api/search/settings", patch, "PUT"); } catch (e) { toast(e.message); } loadSearch(); }
function srAddFolder(path) {
  path = (path || "").trim().replace(/\/+$/, ""); if (!path || !sr) return;
  if (sr.removed.includes(path)) return srSave({ removed: sr.removed.filter((p) => p !== path) });
  if (sr.roots.includes(path)) return toast("That folder is already indexed");
  srSave({ folders: [...sr.folders, path] });
}
function srRemoveFolder(path) {
  if (sr.folders.includes(path)) return srSave({ folders: sr.folders.filter((p) => p !== path) });
  srSave({ removed: [...sr.removed, path] });
}
async function srEnable() { try { await jpost("/api/search/enable"); } catch (e) { toast(e.message); } loadSearch(); }
$("srEnable").onclick = srEnable;
$("srSw").onchange = async () => {
  if ($("srSw").checked) return srEnable();
  try { await jpost("/api/search/disable"); } catch (e) { toast(e.message); }
  loadSearch();
};
$("srFolders").onclick = (e) => { const b = e.target.closest("[data-srrm]"); if (b) srRemoveFolder(b.dataset.srrm); };
$("srAddPath").onclick = () => { srAddFolder($("srPath").value); $("srPath").value = ""; };
$("srPath").addEventListener("keydown", (e) => { if (e.key === "Enter") $("srAddPath").click(); });
$("srChoose").onclick = async () => { try { const r = await jpost("/api/search/choose-folder"); if (r.path) srAddFolder(r.path); } catch (e) { toast(e.message); } };
$("srBattery").onchange = () => srSave({ on_battery: $("srBattery").checked });
$("srRebuild").onclick = async () => {
  if (!await confirmBox("Rebuild the search index?", "Every file is read and indexed again. This can take an hour or more.", "Rebuild")) return;
  try { await jpost("/api/search/rebuild"); toast("Rebuilding the index"); } catch (e) { toast(e.message); }
  loadSearch();
};
$("srDelete").onclick = async () => {
  if (!await confirmBox("Delete the search index?", "Your files are not touched. Turning Search on again rebuilds it.")) return;
  try { await api("/api/search/index", { method: "DELETE" }); } catch (e) { toast(e.message); }
  loadSearch();
};
$("srErrBtn").onclick = async () => {
  const box = $("srErrList"); if (box.style.display === "") { box.style.display = "none"; return; }
  try {
    const { errors } = await api("/api/search/errors");
    box.innerHTML = errors.map((x) => `<div class="row"><div class="grow"><div class="ttl">${esc(srHome(x.path))}</div><div class="sub">${esc(x.error)}</div></div></div>`).join("");
    box.style.display = "";
  } catch (e) { toast(e.message); }
};
document.addEventListener("click", (e) => { const b = e.target.closest("[data-srpause]"); if (b) srSave({ paused: b.dataset.srpause === "1" }); });
function srRow(r) {
  const dir = srHome(r.path.slice(0, r.path.length - r.name.length - 1)), loc = srLoc(r.locator);
  const th = r.kind === "image" ? `<img src="/api/search/thumb/${r.file_id}" alt="" loading="lazy">` : icon(SR_ICON[r.kind] || "i-file");
  const open = `data-open="${r.file_id}" data-path="${esc(r.path)}"`;
  return `<div class="srrow"><div class="th">${th}</div><div style="flex:1; min-width:0"><div class="nm">${esc(r.name)}</div><div class="pt">${esc(dir)}${loc ? " · " + esc(loc) : ""}</div>${r.snippet ? `<div class="sn">${esc(r.snippet)}</div>` : ""}</div>
    <div class="acts"><button class="btn sm" ${open}>Open</button><button class="btn sm" ${open} data-reveal="1">Reveal</button></div></div>`;
}
async function runSearch() {
  const q = $("srQuery").value.trim(), seq = ++srSeq;                 // srSeq: a slow earlier query must not overwrite a newer one
  if (!q) { $("srResults").innerHTML = ""; return; }
  let res;
  try { res = (await jpost("/api/search", { query: q, kind: $("srKind").value })).results; }
  catch (e) { if (seq === srSeq) $("srResults").innerHTML = `<p class="lead" style="margin-top:18px">${esc(e.message)}</p>`; return; }
  if (seq !== srSeq) return;
  $("srResults").innerHTML = res.length ? res.map(srRow).join("") : `<p class="lead" style="margin-top:18px">Nothing found.</p>`;
}
function srOpen(b) { return jpost("/api/search/open", { file_id: +b.dataset.open, path: b.dataset.path, reveal: b.dataset.reveal === "1" }).catch((e) => toast(e.message)); }
$("srQuery").addEventListener("input", () => { clearTimeout(srTimer); srTimer = setTimeout(runSearch, 300); });
$("srQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(srTimer); runSearch(); } });
$("srKind").onchange = runSearch;
$("srResults").onclick = (e) => { const b = e.target.closest("[data-open]"); if (b) srOpen(b); };
setInterval(() => { if (view === "search" || view === "manage" || sr?.setup?.status === "running") loadSearch(); }, 3000);

```

In the boot block, after the line `  await loadLibrary();` add:

```js
  loadSearch();
```

- [ ] **Step 6: Run the UI start-up guard**

Run: `node macos/check_ui.js index.html`
Expected: `   UI script OK`

- [ ] **Step 7: Check the Search view in a browser**

Start the development server from Task 9 Step 10 if it is not running (port 7861, temporary state, sample roots). Open `http://127.0.0.1:7861` in the Browser pane and verify, reading the page text after each action:

1. Click **Search** in the sidebar. The page shows "Search is off" and a **Turn on Search** button. The header reads "Search".
2. Click **Turn on Search**. The message changes to a setup step ("Starting Search" or similar), then within about a minute the search box appears with the status "19 files indexed · up to date".
3. Type `how much do I owe the plumber`. Within a second the first result is `invoice_acme.txt`, with its folder path and the passage starting "Invoice 2291 from Acme Plumbing".
4. Set the kind filter to **Images** and type `desert plant at night`. The only results are images, the first is `IMG_2001.jpg`, and it shows a thumbnail (the `<img>` has loaded: check `document.querySelector(".srrow img").naturalWidth > 0`).
5. Type `zebra astronomy quantum physics` with the filter on **All**. One result is shown, not all 19.
6. Clear the box. The results clear.
7. Open **Settings** (do not touch the Engine section's Stop button: it controls the user's live engine). The Search section shows the switch on, one folder row ending in `build/search_samples/files`, "Index on battery" off, and "19 files · … on disk".
8. Click **Remove** on the folder row. The row disappears and, after the next refresh, the index line reads "0 files". Paste the folder's full path into the box and click **Add**. The row returns and the count goes back to 19.
9. Turn the Search switch off. The folder rows disappear and an "Index on disk" row with **Delete index** appears. Click it and confirm. The row disappears.

Also resize the pane to the mobile preset and confirm the Search view has no horizontal scroll, then return to the desktop preset.

- [ ] **Step 8: Commit**

```bash
git add index.html
git commit -m "index.html: Search view and Search settings"
```

---

### Task 11: "My files" in chat

**Files:**
- Modify: `app.py` (above `@app.post("/api/chat")`; inside `chat()`)
- Modify: `index.html` (composer; `saveChat`; `turnHTML`; the thread click handler; `generate`; the search script block)
- Modify: `macos/check_app.py`

**Interfaces:**
- Consumes: the service's `POST /passages` (`{query, limit, max_chars}` → `{passages: [{file_id, name, path, locator, text}]}`); `_counted`, `_sse`, `SEARCH_URL`; in the UI `sr`, `loadSearch`, `srOpen`.
- Produces: `/api/chat` accepts `use_files: true` and, when passages are found, emits `data: {"x_sources": [{file_id, name, path, locator}]}` as its first event. Chat messages gain an optional `sources` array, saved with the chat.

- [ ] **Step 1: Write the failing check**

Add to `macos/check_app.py`, above `if __name__ == "__main__":`:

```python
@check
def file_passages():
    class Resp:
        status_code = 200

        def __init__(self, passages):
            self._p = passages

        def json(self):
            return {"passages": self._p}

    found = [{"file_id": 3, "name": "lease.pdf", "path": "/r/lease.pdf", "locator": "p2", "text": "Rent is 2,450 dollars per month."},
             {"file_id": 4, "name": "notes.txt", "path": "/r/notes.txt", "locator": "", "text": "Bond is four weeks' rent."}]
    calls = []
    real = app.httpx.post
    try:
        app.httpx.post = lambda url, **kw: calls.append((url, kw["json"])) or Resp(found)
        msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"}, {"role": "user", "content": "what is my rent?"}]
        sources = app._add_file_passages(msgs)
        assert calls == [(app.SEARCH_URL + "/passages", {"query": "what is my rent?", "limit": 6, "max_chars": 12000})]
        assert sources == [{"file_id": 3, "name": "lease.pdf", "path": "/r/lease.pdf", "locator": "p2"},
                           {"file_id": 4, "name": "notes.txt", "path": "/r/notes.txt", "locator": ""}]
        text = msgs[-1]["content"]
        assert text.startswith(app.FILES_PROMPT) and text.endswith("Question: what is my rent?")
        assert "[1] lease.pdf (p2)\nRent is 2,450 dollars per month." in text and "[2] notes.txt\nBond is four weeks' rent." in text
        assert msgs[1]["content"] == "earlier question", "only the latest user message is changed"

        app.httpx.post = lambda url, **kw: Resp([])                         # nothing matched
        msgs = [{"role": "user", "content": "hello"}]
        assert app._add_file_passages(msgs) == [] and msgs[0]["content"] == "hello"

        def down(url, **kw):
            raise app.httpx.ConnectError("refused")

        app.httpx.post = down                                               # Search is not running
        assert app._add_file_passages(msgs) == [] and msgs[0]["content"] == "hello"
        assert app._add_file_passages([{"role": "assistant", "content": "no user turn"}]) == []
    finally:
        app.httpx.post = real
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py file_passages`
Expected: FAIL with `AttributeError: module 'app' has no attribute '_add_file_passages'`.

- [ ] **Step 3: Implement the backend step**

In `app.py`, directly above the line `active_chats = 0` (added in Task 9), add:

```python
FILES_PROMPT = ("Answer using these excerpts from the user's files. Name the files you used. "
                "If they don't contain the answer, say so.")


def _add_file_passages(messages):
    """"My files": put the best-matching passages from the search index in front of the latest
    user message. Returns the sources, or [] (message untouched) when Search is off or nothing matched."""
    last = next((m for m in reversed(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].strip()), None)
    if last is None:
        return []
    try:
        r = httpx.post(SEARCH_URL + "/passages", json={"query": last["content"], "limit": 6, "max_chars": 12000}, timeout=30)
        passages = r.json()["passages"] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, KeyError):
        return []
    if not passages:
        return []
    block = "\n\n".join(f"[{i + 1}] {p['name']}" + (f" ({p['locator']})" if p["locator"] else "") + f"\n{p['text']}"
                        for i, p in enumerate(passages))
    last["content"] = f"{FILES_PROMPT}\n\n{block}\n\nQuestion: {last['content']}"
    return [{k: p[k] for k in ("file_id", "name", "path", "locator")} for p in passages]


```

In `chat()`, replace

```python
    m = chat_model(body.pop("model_id", None))
    messages, needs_vision = build_messages(body.get("messages", []))
```

with

```python
    m = chat_model(body.pop("model_id", None))
    # Before build_messages, so the passages count towards the context-size estimate below.
    sources = await asyncio.to_thread(_add_file_passages, body.get("messages", [])) if body.pop("use_files", False) else []
    messages, needs_vision = build_messages(body.get("messages", []))
```

Inside `relay()`, make its first statement:

```python
        if sources:
            yield _sse({"x_sources": sources})
```

so the function begins:

```python
    async def relay():
        if sources:
            yield _sse({"x_sources": sources})
        if needs_vision and not m.get("vision"):
```

- [ ] **Step 4: Run the app checks to verify they pass**

Run: `uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py`
Expected: eight `ok` lines ending with `ok file_passages`, then `app checks OK`.

- [ ] **Step 5: Add the pill and its styles**

In the `<style>` block of `index.html`, after the `.srpath` rule from Task 10, add:

```css
.sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 2px; }
.sources .chip { cursor: pointer; font: inherit; font-size: 12.5px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.sources .chip:hover { background: var(--hover); }
```

In the composer, after the line that contains `id="effortSel"`, add:

```html
            <button class="pillbtn" id="filesPill"><svg class="i"><use href="#i-file"/></svg>My files</button>
```

- [ ] **Step 6: Wire the pill**

In the search script block from Task 10, change `let sr = null, srTimer = 0, srSeq = 0;` to:

```js
let sr = null, srTimer = 0, srSeq = 0, useFiles = store.get("useFiles", false);
function syncFiles() {
  const can = !!(sr && sr.enabled && sr.running && !sr.error);
  $("filesPill").classList.toggle("on", useFiles && can);
  $("filesPill").disabled = !can;
  $("filesPill").title = can ? "Answer from your indexed files" : "Turn on Search in Settings to use your files";
  store.set("useFiles", useFiles);
}
$("filesPill").onclick = () => { useFiles = !useFiles; syncFiles(); };
```

In `loadSearch`, replace the line `  if (!on) return;` with:

```js
  syncFiles();
  if (!on) return;
```

- [ ] **Step 7: Send the flag, receive the sources, show and save them**

In `generate()`, after the line `if (s.temp) body.temperature = +s.temp;` add:

```js
  if (useFiles && sr?.running && !sr.error) body.use_files = true;
```

In the stream loop of `generate()`, after the line that begins `if (j.x_engine) {`, add:

```js
          if (j.x_sources) { bot.sources = j.x_sources; continue; }
```

Above `function turnHTML(m, i, live = false) {`, add:

```js
function sourcesHTML(m) {
  if (!m.sources?.length) return "";
  const seen = new Set();                                    // one chip per file, however many passages it gave
  return `<div class="sources">${m.sources.filter((s) => !seen.has(s.path) && seen.add(s.path)).map((s) => `<button class="chip" data-open="${s.file_id}" data-path="${esc(s.path)}" title="${esc(srHome(s.path))}">${icon("i-file", "width:14px;height:14px")}${esc(s.name)}</button>`).join("")}</div>`;
}
```

In `turnHTML`, change the final `return` for a bot turn from

```js
  return `<div class="turn bot" data-i="${i}">${thoughtHTML(m, live)}<div class="content">${body}${tail}</div>${m.error ? `<div class="err">${esc(m.error)}</div>` : ""}${cut}${actions}</div>`;
```

to

```js
  return `<div class="turn bot" data-i="${i}">${thoughtHTML(m, live)}<div class="content">${body}${tail}</div>${live ? "" : sourcesHTML(m)}${m.error ? `<div class="err">${esc(m.error)}</div>` : ""}${cut}${actions}</div>`;
```

In the `$("thread").onclick` handler, after the line `if (full) { window.open(full.dataset.full, "_blank"); return; }` add:

```js
  const src = e.target.closest(".sources [data-open]");
  if (src) { srOpen(src); return; }
```

In `saveChat`, replace both field lists so `sources` is kept. The line becomes:

```js
  const msgs = current.messages.map(({ role, content, reasoning, stats, thoughtSecs, error, attachments, finish, sources }) => ({ role, content, reasoning, stats, thoughtSecs, error, attachments, finish, sources }));
```

- [ ] **Step 8: Run the UI start-up guard**

Run: `node macos/check_ui.js index.html`
Expected: `   UI script OK`

- [ ] **Step 9: Check the event stream from the development server**

With the development server running on 7861 and Search on (Task 9 Step 10), run:

```bash
curl -sN http://127.0.0.1:7861/api/chat -H 'Content-Type: application/json' -d '{"model_id":"lfm2.5-1.2b","use_files":true,"messages":[{"role":"user","content":"what is the rent on my lease?"}]}'; echo
```
Expected: two events. First `data: {"x_sources": [{"file_id": …, "name": "lease.txt", "path": "…/build/search_samples/files/lease.txt", "locator": ""}]}`, then `data: {"error": "LFM2.5 1.2B isn't downloaded yet. Get it from the Model library."}`. The `model_id` must be a catalog model that is not downloaded (see Global Constraints), so no engine loads.

```bash
curl -sN http://127.0.0.1:7861/api/chat -H 'Content-Type: application/json' -d '{"model_id":"lfm2.5-1.2b","use_files":false,"messages":[{"role":"user","content":"what is the rent on my lease?"}]}' | grep -c x_sources
```
Expected: `0`.

- [ ] **Step 10: Check the pill in a browser**

Open `http://127.0.0.1:7861` in the Browser pane.

1. With Search on, the **My files** pill in the message box is enabled. Click it: it turns green. Reload the page: it is still green.
2. Turn Search off in Settings and return to the chat. The pill is disabled and its tooltip reads "Turn on Search in Settings to use your files". Turn Search back on.

A full reply with source chips needs a chat engine, and loading one from the development server would stop the engine the user's live app is using. That end-to-end reply is verified in Task 12's manual check.

- [ ] **Step 11: Commit**

```bash
git add app.py index.html macos/check_app.py
git commit -m "My files: chat answers from the search index and cites its sources"
```

---

### Task 12: Bundle, documentation, version 1.3.0 and release verification

**Files:**
- Modify: `macos/build.sh` (file list; `Info.plist`)
- Modify: `app.py` (`APP_VERSION`)
- Modify: `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `build/Local Studio.app` version 1.3.0 containing `search.py` and `search-requirements.txt`.

- [ ] **Step 1: Bundle the new runtime files and add the privacy strings**

In `macos/build.sh`, change

```zsh
for f in app.py index.html catalog.json setup.sh start.sh stop.sh uninstall.sh; do
```

to

```zsh
for f in app.py index.html catalog.json search.py search-requirements.txt setup.sh start.sh stop.sh uninstall.sh; do
```

In the `Info.plist` heredoc, after the `NSAppTransportSecurity` line, add:

```xml
  <key>NSDesktopFolderUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
  <key>NSFileProviderDomainUsageDescription</key><string>Local Studio indexes your files on this Mac so you can search them. Nothing leaves your computer.</string>
```

- [ ] **Step 2: Bump the version**

In `app.py`, change `APP_VERSION = "1.2.2"` to `APP_VERSION = "1.3.0"`.

In `README.md`, run `grep -n "1\.2\.2" README.md` and change every occurrence (the version badge and the DMG file name) to `1.3.0`.

- [ ] **Step 3: Document the feature in `README.md`**

Add this section directly above the `## Models` heading:

```markdown
## Search your files

Turn on **Search** in Settings and Local Studio indexes your Documents, Desktop, Downloads,
Pictures, Movies, Music and iCloud Drive in the background, on this Mac. You can then:

- **Find** a file by describing it: "the invoice from the plumber", "photo of a desert plant at
  night", "voice memo about the dentist". Text, images and spoken audio are all searchable.
- **Ask** in chat with the **My files** switch on. The active chat model answers from your files
  and lists the ones it used.

Search uses [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) on MLX. It is off
until you turn it on; doing so downloads the model (1.3 GB) and its components (about 1 GB) once.
Indexing pauses while you chat or generate images, and on battery power unless you allow it.
Add or remove folders, see what could not be read, and rebuild or delete the index in
Settings → Search. The index lives in `data/search/`.

Not indexed: hidden files, `Library`, apps and other packages (including the Photos library),
developer folders such as `node_modules` and `.git`, private keys, and iCloud files that are not
downloaded. Video files and unrecognised types are found by name only.
```

- [ ] **Step 4: Update `CLAUDE.md`**

Make these four edits:

1. In the file list under "What this is", after the `catalog.json` line add:
   `- `search.py` + `search-requirements.txt`: the file-search service (EmbeddingGemma 2 on MLX) and its dependencies`
2. In the Commands section, add to the standalone checks block:
   ```sh
   uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py
   uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py
   uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples
   ```
   with the sentence: "The first two run in `macos/build.sh`. The third needs the model and a sample folder built by `macos/make_search_samples.py build/search_samples`."
3. After the "One engine at a time" subsection add:
   ```markdown
   ### Search service

   `search.py` is a fourth process on port 8083, separate from the chat engines and exempt from
   the one-at-a-time rule (it holds ~2 GB). `app.py` is its only launcher (`_start_search`), always
   with relative file names and `cwd=ROOT`, because the bundled path contains a space and
   `stop.sh`/`_stop_search` find it by the literal `python search.py --port 8083`. Keep those
   patterns in sync. Its dependencies live in `search-requirements.txt` and are installed by
   `setup.sh --search` when the user turns Search on, never by `--install`. The index is
   `data/search/index.db` under `STATE`. Module level in `search.py` imports only the standard
   library and numpy so `macos/check_search.py` can run without a model. For development set
   `LOCAL_STUDIO_SEARCH_ROOTS` to a sample folder so the real default folders are never crawled.
   ```
4. In "Mac app and setup protocol", change the bundled file list to
   `(`app.py index.html catalog.json search.py search-requirements.txt setup.sh start.sh stop.sh uninstall.sh`)`.

- [ ] **Step 5: Run every automated check**

Run: `node macos/check_ui.js index.html && uv run -q --no-project --python 3.12 --with numpy python macos/check_search.py | tail -1 && uv run -q --no-project --python 3.12 --with fastapi --with httpx --with python-multipart python macos/check_app.py | tail -1 && uv run -q --no-project --python 3.12 --with-requirements search-requirements.txt python search.py --eval build/search_samples | tail -1`
Expected:
```
   UI script OK
   search checks OK
   app checks OK
17/17 queries ranked the expected file first
```

- [ ] **Step 6: Build the app (without installing)**

Run: `macos/build.sh`
Expected: the three check lines above appear under "==> Checking UI script", the build ends with `Done: …/build/Local Studio.app (1.3.0, build N)`, and

```bash
ls "build/Local Studio.app/Contents/Resources/app/" && /usr/libexec/PlistBuddy -c "Print :NSDocumentsFolderUsageDescription" "build/Local Studio.app/Contents/Info.plist"
```

lists `search.py` and `search-requirements.txt` among the files and prints the usage sentence.

- [ ] **Step 7: Commit**

```bash
git add macos/build.sh app.py README.md CLAUDE.md
git commit -m "Local Studio 1.3.0: search your files" -m "- Search view: find documents, images and audio by describing them, on-device with EmbeddingGemma 2 (MLX)
- My files in chat: answers from your files, with sources
- Background indexing of your folders, paused during chat, image generation and on battery
- Settings: folders, unreadable files, rebuild and delete the index"
```

- [ ] **Step 8: Manual check in the installed Mac app, with the user**

This is the only step that reads the user's real folders, replaces their installed app and shows macOS permission prompts. **Stop and ask the user before starting it**, and do it together.

1. The user quits Local Studio. Run `macos/build.sh --install`. The user opens Local Studio.
2. Settings → Search → turn the switch on. The summary shows "Installing search components", then "Downloading EmbeddingGemma 2" if needed, then "Starting Search", then "Indexing N of M files".
3. macOS asks for access to Documents, Desktop, Downloads (and iCloud Drive if used), each naming **Local Studio** and showing the usage sentence. Allow them. Decline one on purpose and confirm that folder shows "No access" in Settings while the others index.
4. Start a chat reply while it is indexing. The Search status reads "Waiting while chat or image generation is running" and returns to "Indexing" when the reply ends. Stop a second reply mid-stream with the Stop button and confirm indexing resumes.
5. Unplug the Mac. The status reads "Paused on battery power". Turn "Index on battery" on and it resumes. Plug back in.
6. Search view: try three queries the user knows the answer to, one each for a document, a photo and a recording. Confirm the expected file is first or near the top. Click **Open** and **Reveal** on a result.
7. Note what a deliberately unrelated query returns. If many weak results appear, or good ones are cut, record the scores shown and adjust `MARGIN` and `FLOOR` in `search.py` in a follow-up commit.
8. Settings → Search: click **Choose…** and confirm the folder chooser opens in front of the window; add a folder. Paste a path and add it. Remove a folder.
9. Click **Show unreadable files** if any are listed, and read a few reasons. Anything systematic (for example every `.heic` failing) is a bug to fix before release.
10. Chat with **My files** on: ask a question the user's documents answer. The reply names the file and shows source chips; clicking a chip opens the file. Ask an unrelated question: the reply is normal and shows no chips.
11. Quit the app. Run `pgrep -fl "search.py --port 8083"` and confirm it prints nothing. Reopen the app and confirm Search starts on its own and shows the index without re-reading everything.
12. Record the total indexing time and the index size for the README if they differ much from "one to two hours" and "a few hundred MB".
