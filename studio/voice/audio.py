"""Pure audio helpers (numpy only, no models or AVFoundation), so the check suite can run them anywhere."""
import base64
import io
import queue
import threading
import wave

import numpy as np

from studio.voice import config


def resample(x, src, dst):
    """Linear resampling of a mono float32 signal; enough for speech in and out."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if src == dst or not len(x):
        return x
    n = max(1, int(round(len(x) * dst / src)))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def to_wav(pcm, rate):
    """Mono 16-bit WAV bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(rate))
        w.writeframes((np.clip(np.asarray(pcm, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def from_wav(data):
    """(mono float32, rate) from 16-bit PCM WAV bytes; anything else is a ValueError."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                raise ValueError("only 16-bit PCM WAV is accepted")
            ch, rate, frames = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a WAV file: {e}") from None
    if not 8000 <= rate <= 96000 or ch not in (1, 2):
        raise ValueError("unsupported WAV format")
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
    if ch == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1)
    return pcm.astype(np.float32), rate


class Chunker:
    """Cuts a stream into fixed-size chunks, carrying the remainder to the next call."""
    def __init__(self, size=config.CHUNK):
        self.size, self.carry = size, np.zeros(0, dtype=np.float32)

    def __call__(self, x):
        data = np.concatenate([self.carry, np.asarray(x, dtype=np.float32)])
        n = len(data) // self.size
        out = [data[i * self.size:(i + 1) * self.size] for i in range(n)]
        self.carry = data[n * self.size:]
        return out


def mic_event(chunk):
    """The /events payload for one microphone chunk: level plus base64 little-endian int16 PCM."""
    rms = float(np.sqrt(np.mean(np.asarray(chunk, dtype=np.float64) ** 2))) if len(chunk) else 0.0
    pcm = base64.b64encode((np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes()).decode()
    return {"t": "mic", "rms": round(rms, 5), "pcm": pcm}


class Fanout:
    """Delivers events to every open /events stream. A stream that falls behind loses audio instead of growing memory."""
    def __init__(self, size=config.EVENT_QUEUE):
        self.size, self.lock, self.queues = size, threading.Lock(), set()

    def subscribe(self):
        q = queue.Queue(maxsize=self.size)
        with self.lock:
            self.queues.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.queues.discard(q)

    def publish(self, event):
        with self.lock:
            targets = list(self.queues)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def count(self):
        with self.lock:
            return len(self.queues)
