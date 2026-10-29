# Local Studio guide

The detail behind the [README](../README.md): what each part of the app does, the limits, and where things live.

- [Chat and the agent](#chat-and-the-agent)
- [Code view](#code-view)
- [Search your files](#search-your-files)
- [Voice (preview)](#voice-preview)
- [Computer use (experimental)](#computer-use-experimental)
- [Attachments](#attachments)
- [Images](#images)
- [Models and engines](#models-and-engines)
- [Performance](#performance)
- [Where things are stored](#where-things-are-stored)
- [Updating and uninstalling](#updating-and-uninstalling)

## Chat and the agent

Every chat is also an agent. A local model can take up to 12 tool-calling rounds per turn. Each round shows, in order:
- its own **Thought for Xs** block;
- any intermediate note;
- expandable tool steps;
- the final answer.

You never switch modes: just ask.

| Tools | What they do | Approval |
|---|---|---|
| `get_current_datetime` | The real date and time | None |
| `web_search`, `read_webpage` | Search the web (DuckDuckGo) and read public pages | None |
| `search_files`, `read_file` | Search and read your indexed documents, PDFs and scans (on-device OCR) | None |
| `list_directory`, `find_files`, `grep_files`, `code_outline`, `git_status`, `git_diff` | Explore the workspace | None |
| `todo_write`, `memory_store`, `memory_recall`, `delegate_task` | A live checklist, memory across chats, read-only sub-agents | None |
| `write_file`, `edit_file`, `multi_edit_file` | Change files, with a diff preview, an automatic backup and one-click **Undo** | Asks first |
| `run_python`, `run_shell` | Run code in the workspace (20 s and 30 s limits; background processes are managed) | Asks first |
| `ask_user` | A clarifying question with option chips | Answer it |
| Custom MCP servers | Your own tools (Settings → MCP) | Asks first |
| `use_computer` | A browser or Mac app task ([below](#computer-use-experimental)) | Asks for risky steps |

- **Approvals.** Answer with **1** (Yes), **2** (Yes, and don't ask again in this chat) or **3** (No, and tell the
  agent what to do instead). **Esc** declines, and Esc also stops a running reply.
- **Accept edits.** **Shift+Tab** switches to *Accept edits*, for files inside the workspace only. It lasts until you
  quit; commands still ask.
- **Read-only commands.** Commands such as `git status`, `git diff`, `ls` and test runners are pre-approved when
  *Auto-approve read-only* is on.
- **Safety fences.** Credentials (`~/.ssh`, `~/.aws`, `~/.gnupg`, Keychains, `.env` files, private keys, the app's own
  tokens) and system folders are blocked. So are destructive commands (`sudo`, `rm -rf /`, `curl | sh`, `dd`, `mkfs`,
  `launchctl`, keychain dumps). Each turn can be reverted with **Revert turn**.
- **Personal documents** (passports, visas, I-94s, tax forms, receipts) are always looked up in your files, never on the
  web. Scanned PDFs and images are read with on-device Vision OCR, and filled PDF form values are kept.
- **Web results** are leads to check against the source pages; an unreadable page is reported as an error, never
  guessed at.
- **Context.** A meter under the message box shows how much of the context window is used and free. The window is 128k
  tokens by default (100k to 256k in Settings → Context window, capped by what the model supports).
- **Compacting.** `/compact`, or a click on the meter when it fills, summarizes older turns.
- **Background replies.** A reply keeps running while you open other chats or views.

## Code view

A workbench for one workspace folder:
- **Explorer:** Files, Changes and Outline tabs.
- **Editor:** Code and Diff modes, with a syntax check on Save.
- **Terminal:** a real terminal (your login shell, in the workspace).
- **Agent:** beside them, with `/review`, `/test`, `/commit`, `/simplify`, `/init` and `/compact`.
- **Mentions:** type `@` in the message box to mention a file or folder.
- **Project instructions:** `AGENTS.md`, `CLAUDE.md`, `.localstudio.md` and `.cursorrules` are loaded automatically.

When the agent changes files, the editor refreshes, but never over unsaved edits.

## Search your files

Turn on **Search** in Settings. Local Studio then indexes these folders in the background, on this Mac:
- Documents, Desktop and Downloads;
- Pictures, Movies and Music;
- iCloud Drive.

Then you can:
- **Find** a file by describing it: "the invoice from the plumber", "photo of a desert plant at night", "voice memo about
  the dentist". Text, images, spoken audio and video are all searchable. Video is matched by what is on screen and what
  is said, for the first 10 minutes.
- **Preview** a result where it matched:
  - a PDF page with the passage highlighted;
  - the passage in context for Word, Excel, notes and code;
  - the picture;
  - the recording or video, from the matching moment.
- **Ask** in chat with **My files** on. The model answers from your files and lists the ones it used.

How it runs:
- Search uses [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) on MLX.
- Turning it on downloads the model (1.3 GB) and its components (about 1 GB) once.
- Indexing pauses while you chat or generate images, and on battery unless you allow it.
- Manage folders, see what could not be read, and rebuild or delete the index in Settings → Search.
- The search service answers only Local Studio itself: it requires a private token on every request.

Not indexed:
- hidden files, `Library`, and apps and other packages (including the Photos library);
- developer folders such as `node_modules` and `.git`, and Python environments;
- private keys.

Unrecognised file types, and iCloud files not downloaded to this Mac, are found by name only.

## Voice (preview)

A hands-free conversation with the same agent:
- you can interrupt it at any time;
- the microphone is echo-cancelled, so it doesn't hear itself;
- there are 13 voices to choose from.

Speech is recognized with Parakeet TDT 0.6B v3 and spoken with Phonon, both on this Mac; audio is never written to disk.
The microphone opens only while Voice is on. Voice speaks English.

Voice is off by default while it is in preview. Turn it on in **Settings → Voice and web answers**; no restart is needed.
The same section has two more switches:
- **Live-facts check for voice:** before a spoken reply, Liquid's d1-2B decides in about 0.1 s whether the question needs
  a search, and that search starts at once (LFM Open License v1.0; a 1.35 GB download the first time Voice starts with it).
- **Faster web answers:** a web search also reads the top two pages, and news comes from dated headlines.

**Voice** then appears in the sidebar. The first time you open it, it downloads its speech models once, and macOS asks
for microphone access.

Known limits:
- Uncommon names can be misheard.
- If the Mac is short on memory, a turn can be slower while the model reloads.

## Computer use (experimental)

Ask in chat: "Open Calculator", "Look up the Apple Store hours", "Fill in this form and stop before submitting".

When a request needs a browser or a Mac app, the model starts a task (`use_computer`). It runs in the same chat, with
progress, approvals and the result:
- **Access** is off by default. The first request offers **Enable computer access and continue**, or turn it on in
  Settings → Computer access.
- **Risky actions** pause for Allow once, Deny or Stop.
- **Password, card and one-time-code fields** are always blocked.
- **Take over** brings the browser or app forward for you; **Resume** continues the task.

You need:
- [CuaDriver](https://github.com/trycua/cua) 0.28.2 or newer. Local Studio runs its own copy and never changes
  CuaDriver's settings without your click.
- macOS **Accessibility** and **Screen Recording** for CuaDriver (Settings → Computer access shows the status and a
  Grant button).
- Google Chrome, for the agent's isolated browser profile.

Which apps it may touch (editable in Settings → Computer access):

| Tier | What it can do | Default apps |
|---|---|---|
| Read-only | Look, never act | Banking and finance apps, Keychain Access, System Settings, Terminal and other shells, password managers, Local Studio |
| Ask first | Every changing click or keystroke asks | Mail, Messages, chat apps, Calendar, Finder, your own browsers |
| Allowed | Acts freely; risky wording such as send or delete still asks | Every other app |

Privacy:
- Pages and app contents go only to your local model.
- **CuaDriver's own telemetry is on by default.** Settings → Computer access shows it and has a **Turn off** button.
- Runs and their screenshots are saved in `data/agent/runs/` and can be deleted.

Limits:
- **One task at a time.** Chat and image generation wait while it runs.
- **Step and time limits:** 40 steps and 15 minutes by default. A long research task may end with a **Partial answer**.
- **Downloads** stop at Chrome's save dialog; use Take over there.
- **Pages served by this Mac** (such as Local Studio itself) are never opened.
- **Controls that share a name** are reported as "position uncertain" rather than clicked blindly.
- **Mac apps** must expose accessibility. With several windows of one app, CuaDriver may refuse background typing: Take
  over, then Resume.
- **Small models misread pages sometimes.** Check answers that matter.

## Attachments

Attach with the paperclip, by drag and drop, or by pasting:

| What | How it is used |
|---|---|
| Images | Downscaled to 1536 px. Read by vision models, which switch to the vision engine automatically. |
| PDFs | Converted to text with pypdf. Scanned PDFs (no text layer) send their first 4 pages as images. |
| Word (.docx), Excel (.xlsx), text and code | Converted to text when attached |

Each document is capped at about 20k tokens, so the answer has room; longer files are marked "truncated". Files stay in
`uploads/` on this Mac.

## Images

Text-to-image with three models: Qwen-Image 2.1 (default, 9.6 GB), Z-Image Turbo (5.9 GB, about 9 steps) and FLUX.2
Klein 4B (4.6 GB, 4 steps).

You can set aspect presets, steps, a seed, guidance and step caching. While an image is made there is live progress
with an ETA and Cancel. The library has a full-size view, Reuse settings, download and delete.

Jobs run one at a time with mflux `--low-ram`. Before a job starts, Local Studio checks that there is room in memory
next to the chat model. Chat and images share the GPU, so running both at once slows each down.

Image editing needs the full `Qwen/Qwen-Image-2.1` checkpoint (33.1 GB); the 4-bit weights installed by default are
text-to-image only. Start the backend with `EDIT_MODEL=Qwen/Qwen-Image-2.1` to enable `mode=edit`.

## Models and engines

Chat models in the Model library (sizes are downloads):

| Model | Engine | Size | Good for |
|---|---|---|---|
| Qwen3.8 27B (default) | MLX + DFlash 2 | 18.7 GB | Best all-rounder, reads images |
| Qwen3.6 35B-A3B | MLX + DSpark | 23.5 GB | Fast mixture-of-experts, coding, reads images |
| Nemotron 3.5 Lightning 30B-A3B | MLX + DSpark | 19.7 GB | Long documents, tool use |
| Gemma 4 12B | MLX + DFlash | 14.2 GB | Multilingual writing, reads images |
| Qwen3 8B | MLX + DFlash | 10.8 GB | Light everyday model |
| LFM2.5 24B-A2B | MLX | 13.6 GB | Agents and tool use, ~85 tok/s |
| LFM2.5 1.2B | MLX + DSpark | 2.9 GB | Tiny and very fast, ~200 tok/s |
| LFM3 1.7B | MLX (mlx-vlm) | 1.9 GB | Tiny agent model, tool use, 256K context |
| LFM3-VL 2B | MLX (mlx-vlm) | 2.2 GB | Small vision model, reads images |
| Qwen3 Coder 30B-A3B | llama.cpp (GGUF) | 18.6 GB | Coding |
| Qwen3.6 35B-A3B | llama.cpp (GGUF) | 23.0 GB | Reads images (mmproj) |
| Gemma 4 12B | llama.cpp (GGUF) | 7.3 GB | Reads images (mmproj) |
| Qwen3.5 9B | llama.cpp (GGUF) | 6.6 GB | Reads images (mmproj) |
| LFM3 1.7B | llama.cpp (GGUF) | 1.1 GB | QAD Q4_0: accurate at 4 bits |
| LFM3-VL 2B | llama.cpp (GGUF) | 1.4 GB | QAD Q4_0, reads images (mmproj) |

Anything else: Model library → **Search Hugging Face**.
- MLX repositories run on the accelerated engine, without a drafter unless mlx-dspark knows one.
- GGUF repositories run on llama.cpp; ticking **Vision** also downloads the repository's mmproj file.
- The curated list lives in `catalog.json`.

LFM3 needs mlx-vlm 0.7.11 or newer (MLX) or llama.cpp b11960 (0.8.0) or newer (GGUF): on older engines, Settings →
**Update engines** first.

Only one chat engine runs at a time, so memory stays free for the model:

| Engine | Runs | Port |
|---|---|---|
| mlx-dspark | MLX chat models with DFlash/DSpark speculative decoding | 8080 |
| mlx-vlm | The same MLX model, when a chat contains an image; LFM3 always (mlx-dspark can't load it) | 8081 |
| llama.cpp (`llama-server`) | GGUF models, with images when an mmproj is present | 8082 |

Switching takes about 5–30 s, depending on the model. The engines also serve OpenAI- and Anthropic-compatible APIs (for
example `http://127.0.0.1:8080/v1`) for other local apps, on this Mac only.

## Performance

Measured on an M4 Pro with 48 GB:

| | Result |
|---|---|
| Qwen3.8 27B 4-bit, plain decoding | 15.0 tok/s |
| The same with DFlash 2 | **41.7 tok/s (2.78×)**; chat 2.0×, code 3.1×, math 3.25× |
| Chat engine memory | ~16 GB resident, ~19 GB peak |
| One 1024 px image | ~10.5 s per step in low-RAM mode, ~10 GB peak |
| First spoken word in Voice (Qwen3.6 35B-A3B) | 0.5–1.3 s after you stop talking |

The "vs plain" figure under each reply compares against the engine's theoretical plain-decoding ceiling, so it reads
a little lower than the benchmark. To re-run the benchmark:

```sh
mlx-dspark benchmark --model mlx-community/Qwen3.8-27B-4bit --modes dflash --caps auto --trials 3
```

## Where things are stored

| What | Location |
|---|---|
| Models (MLX and GGUF) | `~/.cache/huggingface/hub` (shared with other tools) |
| Conversations | `<data>/data/chats/` |
| Settings, chosen and added models | `<data>/data/settings.json` |
| Attachments | `<data>/uploads/` |
| Images | `<data>/gallery/` |
| File search index | `<data>/data/search/` |
| Agent memory, file backups (Undo), computer-use runs | `<data>/data/agent/` |
| Logs | `<data>/logs/` |
| llama.cpp (when installed without Homebrew) | `~/Library/Application Support/Local Studio/llama.cpp` |
| Engine calibration | `~/.cache/mlx_dspark` |

`<data>` is `~/Library/Application Support/Local Studio` for the Mac app (**Local Studio → Show Data Folder**), or the
checkout folder when running from source.

## Updating and uninstalling

**Updating:**
- **The app:** download the new DMG from [Releases](https://github.com/vinoth12940/local-studio/releases) and replace the
  app in Applications. Your data stays where it is.
- **The engines:** Settings → **Update engines**, then restart the engine.

**Uninstalling:**
- **From the app:** Settings → Storage → **Uninstall Local Studio**.
- **From a checkout:** `./uninstall.sh`, or `./uninstall.sh --remove-app` to also delete the folder with your images and
  chats.

Uninstalling stops the servers and removes:
- the engines and every catalog or added model;
- `~/.cache/mlx_dspark`;
- `/Applications/Local Studio.app`.

uv itself is left installed.
