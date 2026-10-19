"""Opt-in secret filtering at subprocess log file descriptors and Python log formatters."""
import codecs
import logging
import os
import threading

from studio.security.redact import StreamRedactor, redact
from studio.storage.flags import flag


class _PipeLog:
    """A file descriptor children can inherit; only filtered bytes reach disk."""
    def __init__(self, path, mode):
        reader, writer = os.pipe()
        self.writer = os.fdopen(writer, "w", encoding="utf-8", buffering=1)
        def copy():
            r = StreamRedactor()
            try:
                with os.fdopen(reader, "rb") as src, open(path, mode, encoding="utf-8") as dst:
                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    while chunk := src.read1(4096):
                        dst.write(r.feed(decoder.decode(chunk))); dst.flush()
                    dst.write(r.feed(decoder.decode(b"", final=True)))
                    dst.write(r.finish()); dst.flush()
            except OSError:
                pass
        self.thread = threading.Thread(target=copy, daemon=True)
        self.thread.start()

    def fileno(self):
        return self.writer.fileno()

    def write(self, text):
        return self.writer.write(text)

    def flush(self):
        self.writer.flush()

    def close(self):
        if not self.writer.closed:
            self.writer.close()
            self.thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        if hasattr(self, "writer") and not self.writer.closed:
            self.writer.close()


def open_log(path, mode="w", **kwargs):
    return _PipeLog(path, mode) if flag("security.redact") else open(path, mode, **kwargs)


class _RedactingFormatter(logging.Formatter):
    def __init__(self, original):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record):
        text = self.original.format(record)
        return redact(text) if flag("security.redact") else text


def install_log_redaction():
    # Uvicorn configures these handlers before importing the ASGI application.
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not isinstance(handler.formatter, _RedactingFormatter):
                handler.setFormatter(_RedactingFormatter(handler.formatter))
