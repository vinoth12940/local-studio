# Performance baseline

The baseline for the refactor (`docs/refactor-progress.json`, P0.bench / P0.perf-baseline). It was taken before any code
moved, on `main` at 1bc4640 plus the P0 safety-net commit, which made no product code changes. P4 re-measures against it.

- **Machine:** Mac16,7 (Apple M4 Pro, 48 GB), macOS 27.0.1, Python 3.12.13
- **Date:** 2026-10-18
- **Dependencies:** fastapi 0.146.0, starlette 1.9.0, uvicorn 0.54.0, httpx 0.28.1, anyio 4.15.1

## Method

Backend numbers come from `macos/bench_app.py`. Run it by hand; it is not part of `build.sh`:

```sh
uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt python macos/bench_app.py [--chats 200] [--json out.json]
```

The bench uses a temporary `LOCAL_STUDIO_HOME` and free ports. It runs uvicorn in-process with a fake OpenAI-style streaming
engine and stubs engine and model state, so it never starts a real engine, search service or CuaDriver daemon. It also
never touches 7860 or 8080–8083. The fixtures are 200 chats of 25 messages each (3.3 MB), or 2,000 chats (32.6 MB), plus a
2,000-file workspace. Hot routes are timed with 200 sequential requests after 10 warm-ups. Two runs agreed within 12% on
every row below.

UI numbers come from the `agent-ui-dev` server (port 7861), read with the Navigation and Resource Timing APIs in the app's
browser pane on a warm load.

## Results

### API overhead per hot route (200 chats)

| Route | p50 | p95 | Note |
|---|---|---|---|
| `GET /api/chats` | 6.5 ms | 7.5 ms | Parses every chat file in full on each call |
| `GET /api/chats` at **2,000 chats** | **64.9 ms** | **66.5 ms** | Linear in total chat bytes; over the 50 ms target |
| `GET /api/chats/{id}` | 0.6 ms | 0.8 ms | |
| `GET /api/workspace/files` (2,000 files, `q=mod07`) | 38.3 ms | 39.8 ms | Walks the whole tree on every keystroke query |
| `GET /api/models` (settings + catalog) | 0.8 ms | 0.9 ms | |
| `GET /api/system` | 8.7 ms | 9.4 ms | Polled every 4 s by the page |

### Chat proxy

| | p50 | p95 |
|---|---|---|
| First token, direct from the fake engine | 1.0 ms | 1.1 ms |
| First token through `POST /api/chat` | 5.3 ms | 7.2 ms |
| **Overhead added by the proxy** | **4.3 ms** | **6.1 ms** |

### Event loop under 10 concurrent streams

There were 10 streams of 300 tokens each at 5 ms per token, and all of them were delivered. Wall time was 1.7 s.

- Loop lag: p50 0.9 ms, p95 1.3–1.5 ms, max 8.0–8.8 ms
- Stalls over 250 ms: **0**

### Threadpool saturation

- **Setup:** anyio's default thread limit is **40**. 60 slow sync requests were sent to `GET /api/search/page/{id}/1`. Each
  request blocks for 1 s, which simulates an OCR page render through the search service.
- **Idle:** a fast sync route (`GET /api/busy`) took p50 0.9 ms.
- **Saturated:** while the 60 requests were in flight, its first request waited **942–948 ms** for a free worker thread.

Long sync work and quick routes share one pool, so a burst of OCR or engine-switch requests can stall the UI's polling
routes for as long as the slow work lasts. This is the clearest P4 lead (§7.3: a separate bounded executor for long work).

### Cold start (engines off)

From `python -m uvicorn app:app` to the first `/api/system` 200, the time was **0.44–0.50 s**. This covers import and
server start only. It excludes `uv run` environment resolution and the Mac app's own launch.

### UI (dev server, warm load)

| Metric | Value |
|---|---|
| `index.html` transferred | 302 KB, served uncompressed |
| marked 12.0.2 (jsDelivr, SRI) | 35 KB decoded, from cache on a warm load |
| Google Fonts CSS | 25 KB decoded |
| API calls in the first second | 15, 25 KB in total: `/api/system`, `/api/models`, `/api/chats`, `/api/agent/status` ×2, `/api/search/status`, `/api/llm/health`, `/api/llm/metrics`, then polling |
| TTFB | 41 ms |
| `domInteractive` / `load` | 99 ms / 100 ms |
| Console errors | none |

A cold first load (empty cache) was not measured. The pane keeps its cache between loads.

## Against the starting targets (§7.2)

| Target | Baseline | Status |
|---|---|---|
| API overhead p95 < 50 ms | ≤ 40 ms at 200 chats. `/api/chats` is 66.5 ms at 2,000 chats | Met at 200 chats. **Missed at 2,000 chats** |
| Search candidate lookup p95 < 300 ms | Not measured. Needs the real search model (`search.py --eval`) | Open |
| 4-page OCR < 5 s | Not measured here. Needs `build/DocumentReader` and real scans | Open |
| Cold start (engines off) < 10 s | 0.5 s for the backend alone | Met |
| No event-loop stall > 250 ms | 0 stalls, max lag 8.8 ms | Met |
| (New) A fast route never waits on slow sync work | 948 ms wait with 60 slow requests | **Missed** |

## Leads for P4 (fix only after re-measuring)

1. **Threadpool.** Give long sync work (search proxy renders, OCR, DOCX/XLSX parsing, large diffs) its own bounded
   executor so polling routes keep a free worker.
2. **`GET /api/chats`.** Avoid re-parsing every chat file on each call, for example with a small title/updated index kept
   next to the files. P5 decides whether SQLite is justified.
3. **`GET /api/workspace/files`.** Cache the walk per workspace and invalidate it on the harness's own writes.
4. **`index.html`.** It is 302 KB uncompressed. Compress only `/` and static files, never the SSE responses.
