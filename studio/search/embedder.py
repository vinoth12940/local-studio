"""Search service: embedder. Moved verbatim from search.py (P6)."""
import threading
import time
import numpy as np

from studio.search.config import DIM, MODEL_REPO
from studio.search.extractors import SAMPLE_RATE

# ---------------------------------------------------------------- embedder
# EmbeddingGemma 2 on MLX. One lock around every model call: a query can arrive mid-batch.

class Embedder:
    def __init__(self, repo=MODEL_REPO):
        self.repo, self.lock = repo, threading.Lock()
        self.model = self.processor = None
        self.last_used = time.time()

    @property
    def loaded(self):
        return self.model is not None

    def unload(self):
        with self.lock:
            self.model = self.processor = None
            import gc
            import mlx.core as mx
            gc.collect()
            mx.clear_cache()

    def _run(self, make_inputs):
        import mlx.core as mx
        with self.lock:
            if self.model is None:
                from mlx_vlm import load
                self.model, self.processor = load(self.repo)
            self.last_used = time.time()
            inputs = make_inputs(self.processor)
            out = self.model(**{k: mx.array(v) for k, v in inputs.items()}).text_embeds
            mx.eval(out)
            return np.array(out.astype(mx.float32))

    def embed_texts(self, texts):
        """texts already carry their task prefix (see doc_text)."""
        return self._run(lambda p: p.tokenizer(list(texts), padding=True, truncation=True, max_length=2048, return_tensors="np"))

    def embed_query(self, query):
        return self.embed_texts([f"task: search result | query: {query}"])[0]

    def embed_image(self, image):
        # One image per call: the processor treats a list of images as a single combined input.
        return self._run(lambda p: p(images=[image], return_tensors="np"))[0]

    def embed_audio(self, samples):
        return self._run(lambda p: p(audio=[samples], sampling_rate=SAMPLE_RATE, return_tensors="np"))[0]

    def selftest(self):
        v = self.embed_query("hello")
        return v.shape == (DIM,) and bool(np.isfinite(v).all()) and abs(float(np.linalg.norm(v)) - 1.0) < 0.01
