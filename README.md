# Local Studio

A private, on-device AI studio for Apple Silicon Macs. Chat with **Qwen3.8-27B** accelerated by
**DFlash 2** speculative decoding, and generate images with **Qwen-Image 2.1**. Everything runs
locally on MLX; nothing leaves your Mac.

![Chat](docs/chat.jpg)

| Images | Models & storage |
|---|---|
| ![Images](docs/images.jpg) | ![Models](docs/models.jpg) |

## Features

- **Chat**: streaming replies, collapsible "thinking", effort control, saved conversations with
  search/rename/delete, copy and regenerate, and per-reply performance details (tok/s, speed-up,
  accept length, time to first token, cached tokens).
- **Images**: text-to-image with aspect presets, steps, seed, guidance and step caching; live
  progress with ETA and cancel; a library with full-size view, reuse settings, download and delete.
- **Models & storage**: start/stop the engine, download or delete each model, clear images,
  chats, uploads, logs and caches, and uninstall everything from the UI.
- **API**: the engine serves OpenAI- and Anthropic-compatible endpoints at
  `http://127.0.0.1:8080/v1`, usable from Claude Code, scripts or other local apps.

## Requirements

- Mac with Apple Silicon (M1 or newer), macOS 15+
- 32 GB RAM minimum; 48 GB recommended (all numbers below are from a 48 GB M4 Pro)
- ~30 GB free disk space for the models
- Internet only for the one-time install and model downloads

## Install

```sh
git clone https://github.com/vinoth12940/local-studio.git ~/local-studio
cd ~/local-studio
./install.sh            # tools + ~27 GB of models
# or: ./install.sh --no-models   (then download models from Models & storage)
```

`install.sh` installs [uv](https://docs.astral.sh/uv/) if missing, then installs
[mlx-dspark](https://github.com/ARahim3/mlx-dspark) and [mflux](https://github.com/filipstrand/mflux)
as isolated uv tools on Python 3.12, and downloads:

| Model | Role | Size |
|---|---|---|
| `mlx-community/Qwen3.8-27B-4bit` | Chat model | 15.0 GB |
| `incoai/Qwen3.8-27B-DFlash2` | DFlash 2 drafter | 3.6 GB |
| `mlx-community/Qwen-Image-2.1-mflux-q4` | Image model | 8.9 GB |

## Use

```sh
./start.sh          # engine + UI, then open http://127.0.0.1:7860
./start.sh --ui     # UI only; start the engine later from the UI to save memory
./stop.sh           # stop everything and free memory
```

The servers listen on `127.0.0.1` only and are not reachable from other devices.

## Performance (M4 Pro, 48 GB)

| | Result |
|---|---|
| Qwen3.8-27B 4-bit, plain decoding | 15.0 tok/s |
| Same model with DFlash 2 | **41.7 tok/s (2.78×)**; chat 2.0×, code 3.1×, math 3.25× |
| Chat engine memory | ~16 GB resident, ~19 GB peak |
| Image, 1024px | ~10.5 s per step in low-RAM mode, ~10 GB peak |

The "vs plain" figure under each reply compares against the engine's theoretical plain-decoding
ceiling, so it reads slightly lower than the benchmark speed-up.

Image jobs run with mflux `--low-ram`. Without it, generation peaked at ~38 GB and pushed macOS
into critical memory pressure while the chat engine was loaded. Chat and image generation share
the GPU, so running both at once slows each down.

Re-run the benchmark yourself:

```sh
mlx-dspark benchmark --model mlx-community/Qwen3.8-27B-4bit --modes dflash --caps auto --trials 3
```

## Image editing

Editing (`mflux-generate-qwen-2.1-edit`) needs the full `Qwen/Qwen-Image-2.1` checkpoint
(33.1 GB); the 4-bit export used here is text-to-image only, so the UI offers generation only.
The backend accepts `mode=edit` on `POST /api/image` when started with
`EDIT_MODEL=Qwen/Qwen-Image-2.1` (after downloading that checkpoint). Stop the chat engine while
editing, as the full checkpoint needs a lot of memory.

## Configuration

Environment variables read by `app.py`:

| Variable | Default |
|---|---|
| `LLM_MODEL` | `mlx-community/Qwen3.8-27B-4bit` |
| `LLM_DRAFTER` | `incoai/Qwen3.8-27B-DFlash2` |
| `IMAGE_MODEL` | `mlx-community/Qwen-Image-2.1-mflux-q4` |
| `EDIT_MODEL` | unset (edit disabled) |
| `LLM_URL` | `http://127.0.0.1:8080` |

## Where things are stored

| What | Location |
|---|---|
| Models | `~/.cache/huggingface/hub` |
| Engine calibration | `~/.cache/mlx_dspark` |
| Images | `gallery/` |
| Conversations | `data/chats/` |
| Uploads | `uploads/` |
| Logs | `logs/` |

None of these folders are committed to git.

## Uninstall

From the UI: **Models & storage → Uninstall**. Or from the terminal:

```sh
./uninstall.sh               # remove tools and models, keep this folder
./uninstall.sh --remove-app  # also delete this folder (images and chats)
```

This stops the servers, runs `uv tool uninstall` for mlx-dspark and mflux, deletes the Qwen
models from the Hugging Face cache, and removes `~/.cache/mlx_dspark`. uv itself is left installed.

## Project layout

```
app.py          FastAPI backend: chat proxy, image jobs, models, storage, uninstall
index.html      Single-page UI
install.sh      Installs tools and models
start.sh        Starts engine and UI
stop.sh         Stops everything
uninstall.sh    Removes tools, models and (optionally) this folder
```

## Licenses

This repository contains only code. Model weights are downloaded from Hugging Face under their
own licenses; check each model card before commercial use (the Qwen-Image 2.1 weights are
described as research/non-commercial by several sources).
