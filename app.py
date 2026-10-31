"""Local Studio: a local web app for on-device chat and image generation on Apple Silicon.

Chat runs on one of three engines (only one at a time, to keep memory free):
  text   — mlx-dspark: MLX models with DFlash/DSpark speculative decoding (text only)
  vision — mlx-vlm server: the same MLX weights when a chat contains images
  gguf   — llama.cpp's llama-server: GGUF models (vision when an mmproj file is present)
Image jobs run the mflux CLI one at a time. Also manages models, storage, updates and uninstall.

The backend lives in studio/. This file is the entrypoint that `uvicorn app:app` loads (start.sh,
and the process patterns in stop.sh) and the module the check suites import.
"""
# macos/build.sh and release.sh read the version from this line with sed; studio/config.py parses it from here too.
APP_VERSION = "1.7.7"

from studio.main import app, owner_module as _owner_module  # noqa: E402,F401


def __getattr__(name):
    """The check suites read names as app.X: answer live from the studio module that defines X, so a patch there is seen."""
    mod = _owner_module(name)
    if mod is None:
        raise AttributeError(f"module 'app' has no attribute {name!r}")
    return vars(mod)[name]
