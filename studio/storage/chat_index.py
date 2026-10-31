"""Bounded in-memory metadata cache; JSON remains authoritative and external edits invalidate entries."""
import json
import threading

from studio.storage.chats import chat_summary

_LOCK = threading.Lock()
_CACHE = {}


def chat_metadata(directory):
    out, live = [], set()
    with _LOCK:
        for path in directory.glob('*.json'):
            live.add(path)
            try:
                stat = path.stat()
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                cached = _CACHE.get(path)
                if cached is None or cached[0] != signature:
                    meta = chat_summary(json.loads(path.read_text()))
                    cached = (signature, meta)
                    _CACHE[path] = cached
                out.append(dict(cached[1]))
            except (OSError, ValueError, KeyError, TypeError):
                _CACHE.pop(path, None)
        for path in set(_CACHE) - live:
            _CACHE.pop(path, None)
    return sorted(out, key=lambda chat: chat['updated'], reverse=True)
