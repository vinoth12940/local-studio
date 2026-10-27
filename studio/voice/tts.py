"""Speech: Phonon 31M through Gradium's ptts runtime (Rust, no PyTorch). Loaded on first use."""
import threading

import numpy as np

from studio.voice import config


class Phonon:
    def __init__(self):
        self.lock, self._tts, self._voices = threading.Lock(), None, None

    def _load(self):
        if self._tts is None:
            import ptts
            self._tts = ptts.TTS(lang=config.TTS_LANG)
            self._voices = [{"name": v["name"], "description": v.get("description", "")}
                            for v in ptts.catalog_voices() if v.get("language") == config.TTS_LANG]
        return self._tts

    @property
    def loaded(self):
        return self._tts is not None

    def voices(self):
        with self.lock:
            self._load()
            return list(self._voices)

    def synth(self, text, voice, speed=0.0):
        """(float32 PCM, rate). Only catalog voices; speed is -0.5 (faster) to 0.5 (slower)."""
        text = str(text or "").strip()
        if not text or len(text) > config.MAX_TEXT:
            raise ValueError(f"text must be 1-{config.MAX_TEXT} characters")
        with self.lock:
            tts = self._load()
            if voice not in {v["name"] for v in self._voices}:
                raise ValueError("unknown voice")
            delta = max(-0.5, min(0.5, float(speed or 0)))
            pcm = tts.synth(text, voice=voice, conditions={"duration_delta": delta}) if delta else tts.synth(text, voice=voice)
            return np.asarray(pcm, dtype=np.float32), tts.sample_rate
