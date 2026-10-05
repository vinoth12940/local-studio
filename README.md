# Local Studio

<img src="docs/logo.png" width="96" align="right" alt="Local Studio logo">

A private, on-device AI studio for Apple Silicon Macs. Chat with models like **Qwen3.8-27B**
accelerated by **DFlash/DSpark** speculative decoding or any **GGUF** model on **llama.cpp**, ask
about images and documents, and generate images with **Qwen-Image 2.1**, **Z-Image Turbo** or
**FLUX.2 Klein**. Everything runs locally; nothing leaves your Mac.

**Version 1.1.0**

![Chat](docs/chat.jpg)

| Model library | Images |
|---|---|
| ![Model library](docs/library.jpg) | ![Images](docs/images.jpg) |

## Features

- **Chat**: streaming replies, collapsible "thinking", effort control, saved conversations with
  search/rename/delete, copy and regenerate, and per-reply performance details (tok/s, speed-up,
  accept length, time to first token, cached tokens).
- **Attachments (multimodal)**: attach with the paperclip, drag and drop, or paste. Ask about
  images (Qwen3.8 vision), PDFs, Word (.docx), Excel (.xlsx), and text/code files. Scanned PDFs
  are read as page images.
- **Images**: text-to-image with aspect presets, steps, seed, guidance and step caching; live
  progress with ETA and cancel; a library with full-size view, reuse settings, download and delete.
- **Model library**: one-click download, switch and delete for a curated set of chat and image
  models, plus search across Hugging Face for any MLX or GGUF model (pick the quantization).
  Switch the chat model from the header; the right engine starts automatically.
- **Settings**: start/stop the engine, see versions, update the engines in one click, clear
  images, chats, uploads, logs and caches, and uninstall everything.
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

## Mac app

Build a native **Local Studio.app** (Dock icon, own window, Mac menus) and install it to
/Applications:

```sh
macos/build.sh --install
```

- Opening the app starts the engine and interface automatically; there's nothing else to run.
- Quitting (⌘Q) stops everything and frees the ~16 GB the model uses. To keep the servers running
  in the background instead, check **Local Studio → Keep Running After Quit**.
- Closing the window keeps the app in the Dock; click the icon to bring it back.
- Attachments open a native file picker, downloads go to ~/Downloads, and links open in your browser.
- The app is a thin Swift/WebKit shell (`macos/main.swift`) around the same local web UI, so the
  browser at http://127.0.0.1:7860 keeps working too. It is signed with your Apple Development
  certificate if one is installed (ad-hoc otherwise), and it looks for the scripts in the folder it
  was built from.

## Use

```sh
./start.sh          # engine + UI, then open http://127.0.0.1:7860
./start.sh --ui     # UI only; start the engine later from the UI to save memory
./stop.sh           # stop everything and free memory
```

The servers listen on `127.0.0.1` only and are not reachable from other devices.

## Models

Chat models (one-click in the Model library; sizes are download sizes):

| Model | Engine | Size | Notes |
|---|---|---|---|
| Qwen3.8 27B (default) | MLX + DFlash 2 | 18.7 GB | Best all-rounder, vision, ~40 tok/s |
| Qwen3.6 35B-A3B | MLX + DSpark | 23.5 GB | Fast MoE, vision, coding |
| Nemotron 3.5 Lightning 30B-A3B | MLX + DSpark | 19.7 GB | Fast hybrid MoE |
| Gemma 4 12B | MLX + DFlash | 14.2 GB | Multilingual writing, vision |
| Qwen3 8B | MLX + DFlash | 10.8 GB | Light everyday model |
| LFM2.5 1.2B | MLX + DSpark | 2.9 GB | Tiny, ~200 tok/s (measured) |
| Qwen3 Coder 30B-A3B | llama.cpp (GGUF Q4_K_M) | 18.6 GB | Coding |
| Qwen3.6 35B-A3B | llama.cpp (GGUF UD-Q4_K_M) | 23.0 GB | Vision via mmproj |
| Gemma 4 12B | llama.cpp (GGUF Q4_K_M) | 7.3 GB | Vision via mmproj |
| Qwen3.5 9B | llama.cpp (GGUF Q4_K_M) | 6.6 GB | Vision via mmproj |

Image models: Qwen-Image 2.1 (default, 9.6 GB), Z-Image Turbo (5.9 GB, ~9 steps) and
FLUX.2 Klein 4B (4.6 GB, 4 steps; a 1024 px image took 27 s on an M4 Pro).

Anything else: Model library → Search Hugging Face. MLX repos run on the accelerated engine
(without a drafter unless it's in mlx-dspark's registry); GGUF repos run on llama.cpp, and
ticking "Vision" also downloads the repo's mmproj file. The catalog lives in `catalog.json`.

## How the engines work

Only one engine runs at a time, so memory stays free for the model:

| Engine | Runs | Port |
|---|---|---|
| mlx-dspark | MLX chat models with DFlash/DSpark speculative decoding (text) | 8080 |
| mlx-vlm | The same MLX model when a chat contains an image | 8081 |
| llama.cpp (`llama-server`) | GGUF models, including images when an mmproj is present | 8082 |

Switching models or engines takes about 5–30 s depending on model size; the chat shows a
"Loading …" message meanwhile.

## How attachments work

Qwen3.8-27B is multimodal, but the fast DFlash engine (mlx-dspark) is text-only. Local Studio
therefore runs one of two engines on the same weights, never both (each needs ~16 GB):

| Engine | Used for | Speed (measured) |
|---|---|---|
| Text: mlx-dspark + DFlash 2 | Chats without images, including document attachments | ~40 tok/s |
| Vision: mlx-vlm server | Chats that contain an image or scanned PDF | ~20 tok/s, ~10 s to read an image |

The app switches automatically when a chat needs the other engine. GGUF models with an mmproj
read images directly on llama.cpp, with no switch. Models without vision show a clear message.

Documents are converted to text when you attach them (PDF via pypdf, Word via python-docx, Excel via
openpyxl, text and code as-is) and added to your message. Each document is capped at about 20k
tokens so the 32k context keeps room for the answer; longer files are marked "truncated". Images are
downscaled to 1536 px. Scanned PDFs (no text layer) send their first 4 pages to the vision engine.
Files stay in `uploads/` on this Mac.

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

## Updating

Settings → **Update engines** runs `uv tool upgrade mlx-dspark mflux` and `brew upgrade llama.cpp`
and shows what changed. Restart the engine afterwards. Pull new app versions with `git pull` and
rebuild the Mac app with `macos/build.sh --install`.

## Configuration

Environment variables read by `app.py`:

| Variable | Default |
|---|---|
| `EDIT_MODEL` | unset (image editing disabled) |
| `HF_HUB_CACHE` | `~/.cache/huggingface/hub` |

The active chat and image models and any models added from Hugging Face are stored in
`data/settings.json`.

## Where things are stored

| What | Location |
|---|---|
| Models (MLX and GGUF) | `~/.cache/huggingface/hub` |
| Active models, custom models | `data/settings.json` |
| Engine calibration | `~/.cache/mlx_dspark` |
| Images | `gallery/` |
| Conversations | `data/chats/` |
| Attachments | `uploads/` |
| Logs | `logs/` |

None of these folders are committed to git.

## Uninstall

From the UI: **Models & storage → Uninstall**. Or from the terminal:

```sh
./uninstall.sh               # remove tools and models, keep this folder
./uninstall.sh --remove-app  # also delete this folder (images and chats)
```

This stops the servers, uninstalls llama.cpp (Homebrew), runs `uv tool uninstall` for mlx-dspark and mflux, deletes every model in the catalog
or added from Hugging Face, removes `~/.cache/mlx_dspark`, and deletes `/Applications/Local Studio.app` if present. uv itself is left installed.

## Project layout

```
app.py          FastAPI backend: engines, chat proxy, attachments, image jobs, models, updates
catalog.json    Curated chat and image models
index.html      Single-page UI
install.sh      Installs tools and models
start.sh        Starts engine and UI
stop.sh         Stops everything
uninstall.sh    Removes tools, models and (optionally) this folder
macos/          Native Mac app: main.swift, logos.py (icon), build.sh
```

## Licenses

This repository contains only code. Model weights are downloaded from Hugging Face under their
own licenses; check each model card before commercial use (the Qwen-Image 2.1 weights are
described as research/non-commercial by several sources).
