"""Speech recognition: Parakeet TDT 0.6B v3 on MLX (parakeet-mlx). Loaded on first use; the first transcription
compiles the graph (about 3 s), so warm() runs one on silence."""
import threading

import numpy as np

from studio.voice import config


class Parakeet:
    def __init__(self):
        self.lock, self._model = threading.Lock(), None

    @property
    def loaded(self):
        return self._model is not None

    def _load(self):
        if self._model is None:
            from parakeet_mlx import from_pretrained
            self._model = from_pretrained(config.STT_MODEL)
        return self._model

    def transcribe(self, pcm, rate=config.MIC_RATE):
        """Text of a mono float32 recording at `rate` (resampled to 16 kHz)."""
        from parakeet_mlx.audio import get_logmel
        import mlx.core as mx
        from studio.voice.audio import resample
        x = resample(pcm, rate, config.MIC_RATE)
        if len(x) < config.MIC_RATE // 10:          # under 0.1 s: nothing to hear
            return ""
        with self.lock:
            model = self._load()
            results = model.generate(get_logmel(mx.array(x), model.preprocessor_config))
        return " ".join(r.text.strip() for r in results if r.text.strip())

    def warm(self):
        self.transcribe(np.zeros(config.MIC_RATE, dtype=np.float32))
