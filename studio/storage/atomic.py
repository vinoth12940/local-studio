"""Atomic file writes (refactor P3): a temp file next to the target, then os.replace, so a crash or a full disk never leaves a
half-written settings.json or chat behind (readers see the old file or the new one)."""
import os
import tempfile
from pathlib import Path


def write_text_atomic(path, text, encoding="utf-8"):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
