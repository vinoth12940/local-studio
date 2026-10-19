"""Search service: indexer. Moved verbatim from search.py (P6)."""
import json
import subprocess
import sys
import threading
import time
import urllib.request

from studio.search.config import APP_URL, STATE, load_config, _under
from studio.search.crawler import walk
from studio.search import extractors as extractors_module
from studio.search.ranking import doc_text

# ---------------------------------------------------------------- indexer

TEXT_BATCH = 32
RESCAN_SECONDS = 300
IDLE_UNLOAD_SECONDS = 600
POLL_SECONDS = 1.0
DEBOUNCE_SECONDS = 0.5


class EmbedFailure(Exception):
    """The embedder itself raised, as opposed to one file being unreadable."""


class Indexer:
    def __init__(self, store, embedder, config=load_config, app_url=APP_URL):
        self.store, self.embedder, self.config, self.app_url = store, embedder, config, app_url
        # "starting" until the first pass begins: an empty index must not read as "up to date"
        self.state, self.paused_reason, self.current, self.no_access, self.error = "starting", None, None, [], None
        self.problem = None                               # why the last pass stopped early; cleared by the next one
        self.wake, self.stop = threading.Event(), threading.Event()
        self._ac = (0.0, True)
        self.full_indexing = False
        self._snapshot = None
        self._changes = {}

    def on_ac(self):
        if time.time() - self._ac[0] > 30:                # pmset is a subprocess: ask at most twice a minute
            try:
                out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
                self._ac = (time.time(), "AC Power" in out)
            except Exception:
                self._ac = (time.time(), True)
        return self._ac[1]

    def app_busy(self):
        """True while the app is streaming a chat reply or generating an image."""
        if not self.app_url:
            return False
        try:
            req = urllib.request.Request(self.app_url + "/api/busy")
            try:
                req.add_header("Authorization", "Bearer " + (STATE / "data/.api-token").read_text().strip())
            except OSError:
                pass
            with urllib.request.urlopen(req, timeout=1) as r:
                return bool(json.load(r).get("busy"))
        except Exception:
            return False                                  # app not answering: keep indexing

    def hold_reason(self, cfg):
        if cfg["paused"]:
            return "user"
        if not cfg["on_battery"] and not self.on_ac():
            return "battery"
        return "busy" if self.app_busy() else None

    def wait_clear(self):
        """Block while indexing should hold off. False means the service is stopping."""
        while not self.stop.is_set():
            reason = self.hold_reason(self.config())
            if reason is None:
                self.state, self.paused_reason = "indexing", None
                return True
            self.state, self.paused_reason = "paused", reason
            self.stop.wait(1)
        return False

    def _embed(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            raise EmbedFailure(f"{type(e).__name__}: {e}"[:300]) from e

    def index_file(self, f):
        """Index one file. Returns "ok", "stopped", "error" (the file is unreadable; recorded) or
        ("embed_failed", message): the embedder raised and the file is left pending."""
        self.current = f["path"]
        self.store.set_meta("in_flight", str(f["id"]))     # if this file kills the process, skip it next time
        try:
            items = extractors_module.extract(f["path"], f["kind"]) if f["kind"] != "name" else []
            rows, texts = [], [i for i in items if i["kind"] == "text"]
            for i in range(0, len(texts), TEXT_BATCH):
                if not self.wait_clear():
                    return "stopped"
                batch = texts[i:i + TEXT_BATCH]
                vecs = self._embed(self.embedder.embed_texts, [doc_text(f["name"], t["text"]) for t in batch])
                rows += [("text", t["text"], t["locator"], v) for t, v in zip(batch, vecs)]
            for item in items:                            # images and audio: one per call
                if item["kind"] == "text":
                    continue
                if not self.wait_clear():
                    return "stopped"
                vec = (self._embed(self.embedder.embed_image, item["image"]) if item["kind"] == "image"
                       else self._embed(self.embedder.embed_audio, item["audio"]))
                rows.append((item["kind"], None, item["locator"], vec))
            self.store.replace_chunks(f["id"], rows)
            return "ok"
        except EmbedFailure as e:
            return "embed_failed", str(e)
        except Exception as e:                            # one unreadable file must not stop the crawl
            self.store.mark_error(f["id"], f"{type(e).__name__}: {e}"[:300])
            return "error"
        finally:
            self.current = None
            self.store.set_meta("in_flight", "")

    def run_once(self):
        crashed = self.store.get_meta("in_flight")
        if crashed:                                       # the process died with this file in flight
            self.store.mark_error(int(crashed), "Indexing this file stopped the service, so it was skipped.")
            self.store.set_meta("in_flight", "")
        cfg, no_access, failed = self.config(), [], []
        found = list(walk(cfg["roots"], cfg["removed"], no_access, failed))
        self.no_access = no_access
        self.store.sync(found, keep_prefixes=failed)
        if cfg.get("incremental"):
            self._snapshot = {f[0]: f for f in found}
            self._changes.clear()
        for f in self.store.pending():
            if self.wake.is_set():                        # folders or settings changed: start over with them
                return
            if not self.wait_clear():
                return
            result = self.index_file(f)
            if isinstance(result, tuple):
                try:                                      # is it this file, or the embedder?
                    self.embedder.embed_query("probe")
                except Exception:
                    self.problem = f"The embedding model is failing ({result[1]}). Indexing will try again in a few minutes."
                    return
                self.store.mark_error(f["id"], result[1])

    def run_pass(self):
        """One crawl-and-index pass. A failure is kept for /status instead of passing for "up to date"."""
        self.problem = None
        self.full_indexing = bool(self.config().get("incremental"))
        try:
            self.run_once()
        except Exception as e:
            self.problem = f"Indexing failed: {type(e).__name__}: {e}"[:300]
            print(self.problem, file=sys.stderr, flush=True)
        finally:
            self.full_indexing = False

    def poll_changes(self, now=None):
        """Poll metadata using the normal crawler policy; debounce and re-embed only stable changed paths."""
        cfg = self.config()
        if not cfg.get('incremental') or self._snapshot is None:
            return []
        now = time.monotonic() if now is None else now
        failed, no_access = [], []
        found = {f[0]: f for f in walk(cfg['roots'], cfg['removed'], no_access, failed)}
        self.no_access = no_access
        for path, previous in self._snapshot.items():
            if path not in found and any(_under(path, prefix) for prefix in failed):
                found[path] = previous  # unreadable/unmounted folders are never interpreted as deletion
        for path in self._snapshot.keys() | found.keys():
            if self._snapshot.get(path) != found.get(path):
                self._changes[path] = (found.get(path), now)
        self._snapshot = found
        if self.hold_reason(cfg):
            return []
        ready = {path: item for path, (item, stamp) in self._changes.items() if now - stamp >= DEBOUNCE_SECONDS}
        if not ready:
            return []
        for path in ready:
            self._changes.pop(path, None)
        self.store.sync([item for item in ready.values() if item is not None], only_paths=ready)
        indexed = []
        for f in self.store.pending():
            if f['path'] not in ready or self.stop.is_set() or self.wake.is_set():
                continue
            result = self.index_file(f)
            if isinstance(result, tuple):
                self.problem = 'Incremental embedding failed: ' + result[1]
                self._changes[f['path']] = (found.get(f['path']), now)
            elif result == 'ok':
                indexed.append(f['path'])
        self.state, self.paused_reason = 'idle', None
        return indexed

    def run(self):
        while not self.stop.is_set():
            self.wake.clear()
            if self.error is None:
                self.run_pass()
            if self.wake.is_set():                        # changed during the pass: go again now
                continue
            self.state, self.paused_reason = "idle", None
            deadline = time.time() + RESCAN_SECONDS
            while not self.stop.is_set() and not self.wake.is_set() and time.time() < deadline:
                if self.embedder.loaded and time.time() - self.embedder.last_used > IDLE_UNLOAD_SECONDS:
                    self.embedder.unload()                # give the memory back until the next query or change
                if self.config().get('incremental') and self.error is None:
                    try:
                        self.poll_changes()
                    except Exception as exc:
                        self.problem = f'Incremental indexing failed: {type(exc).__name__}: {exc}'[:300]
                    self.wake.wait(POLL_SECONDS)
                else:
                    self.wake.wait(5)

    def status(self):
        return {"state": self.state, "paused_reason": self.paused_reason, "current": self.current,
                "model_loaded": self.embedder.loaded, "roots": self.config()["roots"], "no_access": self.no_access,
                "error": self.error, "problem": self.problem, **self.store.counts()}


def shutdown(indexer):
    """A normal stop (the app quitting) with a file in flight is not a crash: don't leave its marker behind."""
    indexer.stop.set()
    indexer.store.set_meta("in_flight", "")

