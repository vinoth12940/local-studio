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


## P4 measurement before fixes (2026-10-18)

Both runs used temporary STATE, fake streaming engines and the pinned Python runtime; no live app/engine state.
The 200-chat run and 2,000-chat/50,000-message run reproduce the two earlier leads: ~950 ms polling starvation,
and 73.56 ms p95 chat listing at scale. Workspace lookup p95 44.84 ms and loop lag remain within the starting targets,
so no workspace caching, thread virtualization or rendering rewrite is justified by these results.

### 200 chats

```json
{
 "date": "2026-10-18 22:18",
 "mac": "arm64 27.0.1",
 "python": "3.12.13",
 "fixture": {
  "chats": 200,
  "chats_mb": 3.3,
  "workspace_files": 2000
 },
 "hot_routes": {
  "GET /api/chats": {
   "p50_ms": 7.94,
   "p95_ms": 8.72,
   "max_ms": 9.74,
   "n": 200
  },
  "GET /api/chats/{id}": {
   "p50_ms": 0.74,
   "p95_ms": 0.97,
   "max_ms": 1.16,
   "n": 200
  },
  "GET /api/workspace/files": {
   "p50_ms": 42.78,
   "p95_ms": 44.83,
   "max_ms": 53.76,
   "n": 200
  },
  "GET /api/models (settings)": {
   "p50_ms": 0.86,
   "p95_ms": 1.07,
   "max_ms": 1.21,
   "n": 200
  },
  "GET /api/system": {
   "p50_ms": 10.92,
   "p95_ms": 14.2,
   "max_ms": 19.54,
   "n": 200
  }
 },
 "proxy": {
  "direct_first_token": {
   "p50_ms": 1.29,
   "p95_ms": 1.53,
   "max_ms": 1.72,
   "n": 30
  },
  "via_api_chat_first_token": {
   "p50_ms": 6.33,
   "p95_ms": 8.33,
   "max_ms": 8.53,
   "n": 30
  },
  "overhead_p50_ms": 5.04,
  "overhead_p95_ms": 6.8
 },
 "concurrent_streams": {
  "streams": 10,
  "content_events_each": [
   300
  ],
  "wall_s": 1.72,
  "loop_lag": {
   "p50_ms": 0.93,
   "p95_ms": 1.43,
   "max_ms": 21.33,
   "n": 157
  },
  "stalls_over_250ms": 0
 },
 "threadpool": {
  "anyio_thread_limit": 40,
  "blocking_requests": 60,
  "block_s": 1.0,
  "fast_sync_route_idle": {
   "p50_ms": 0.95,
   "p95_ms": 1.72,
   "max_ms": 2.3,
   "n": 20
  },
  "fast_sync_route_while_saturated": {
   "p50_ms": 1.84,
   "p95_ms": 951.42,
   "max_ms": 951.42,
   "n": 5
  }
 },
 "cold_start": {
  "import_to_first_api_system_s": [
   0.26,
   0.26,
   0.26
  ]
 }
}
```

### 2000 chats / 50000 messages

```json
{
 "date": "2026-10-18 22:22",
 "mac": "arm64 27.0.1",
 "python": "3.12.13",
 "fixture": {
  "chats": 2000,
  "chats_mb": 32.6,
  "workspace_files": 2000
 },
 "hot_routes": {
  "GET /api/chats": {
   "p50_ms": 66.75,
   "p95_ms": 73.56,
   "max_ms": 88.45,
   "n": 200
  },
  "GET /api/chats/{id}": {
   "p50_ms": 0.69,
   "p95_ms": 0.82,
   "max_ms": 1.19,
   "n": 200
  },
  "GET /api/workspace/files": {
   "p50_ms": 41.61,
   "p95_ms": 44.84,
   "max_ms": 47.5,
   "n": 200
  },
  "GET /api/models (settings)": {
   "p50_ms": 0.81,
   "p95_ms": 1.01,
   "max_ms": 1.12,
   "n": 200
  },
  "GET /api/system": {
   "p50_ms": 9.61,
   "p95_ms": 11.92,
   "max_ms": 18.59,
   "n": 200
  }
 },
 "proxy": {
  "direct_first_token": {
   "p50_ms": 1.0,
   "p95_ms": 1.3,
   "max_ms": 1.3,
   "n": 30
  },
  "via_api_chat_first_token": {
   "p50_ms": 5.33,
   "p95_ms": 6.54,
   "max_ms": 7.38,
   "n": 30
  },
  "overhead_p50_ms": 4.33,
  "overhead_p95_ms": 5.24
 },
 "concurrent_streams": {
  "streams": 10,
  "content_events_each": [
   300
  ],
  "wall_s": 1.73,
  "loop_lag": {
   "p50_ms": 0.55,
   "p95_ms": 1.35,
   "max_ms": 28.51,
   "n": 160
  },
  "stalls_over_250ms": 0
 },
 "threadpool": {
  "anyio_thread_limit": 40,
  "blocking_requests": 60,
  "block_s": 1.0,
  "fast_sync_route_idle": {
   "p50_ms": 0.84,
   "p95_ms": 1.76,
   "max_ms": 1.77,
   "n": 20
  },
  "fast_sync_route_while_saturated": {
   "p50_ms": 1.95,
   "p95_ms": 946.12,
   "max_ms": 946.12,
   "n": 5
  }
 },
 "cold_start": {
  "import_to_first_api_system_s": [
   0.32,
   0.26,
   0.25
  ]
 }
}
```


## P4 after measured fixes (2026-10-18)

Enable `performance.isolated_io` and `performance.chat_index` in settings (both default false).
The isolated pool gives search image/page/preview/media work eight workers and at most 32 submitted jobs per event loop;
waiting requests do not hold FastAPI's shared quick-route workers. The metadata cache validates inode/mtime/size on every
list request, reparsing only changed JSON; JSON is authoritative, and external/atomic edits and deletion invalidate entries.

At 2,000 chats / 50,000 messages, list p95 fell 73.56 -> 20.87 ms (72% lower).
Fast polling under 60 slow renders fell 946.12 -> 2.07 ms p95. Cold-load chat p95 remained below 1 ms.
There were no >250 ms event-loop stalls. These are synthetic measurements, not OCR throughput promises.
The bounded render pool deliberately queues excess rendering rather than growing concurrency without limit.

```json
{
 "optimized": true,
 "date": "2026-10-18 22:24",
 "mac": "arm64 27.0.1",
 "python": "3.12.13",
 "fixture": {
  "chats": 2000,
  "chats_mb": 32.6,
  "workspace_files": 2000
 },
 "hot_routes": {
  "GET /api/chats": {
   "p50_ms": 13.4,
   "p95_ms": 20.87,
   "max_ms": 22.3,
   "n": 200
  },
  "GET /api/chats/{id}": {
   "p50_ms": 0.63,
   "p95_ms": 0.71,
   "max_ms": 0.74,
   "n": 200
  },
  "GET /api/workspace/files": {
   "p50_ms": 39.31,
   "p95_ms": 41.29,
   "max_ms": 47.32,
   "n": 200
  },
  "GET /api/models (settings)": {
   "p50_ms": 0.83,
   "p95_ms": 0.91,
   "max_ms": 1.02,
   "n": 200
  },
  "GET /api/system": {
   "p50_ms": 8.65,
   "p95_ms": 9.84,
   "max_ms": 11.51,
   "n": 200
  }
 },
 "proxy": {
  "direct_first_token": {
   "p50_ms": 1.01,
   "p95_ms": 1.15,
   "max_ms": 1.16,
   "n": 30
  },
  "via_api_chat_first_token": {
   "p50_ms": 5.57,
   "p95_ms": 7.21,
   "max_ms": 7.36,
   "n": 30
  },
  "overhead_p50_ms": 4.56,
  "overhead_p95_ms": 6.07
 },
 "concurrent_streams": {
  "streams": 10,
  "content_events_each": [
   300
  ],
  "wall_s": 1.72,
  "loop_lag": {
   "p50_ms": 0.82,
   "p95_ms": 1.47,
   "max_ms": 17.97,
   "n": 159
  },
  "stalls_over_250ms": 0
 },
 "threadpool": {
  "anyio_thread_limit": 40,
  "blocking_requests": 60,
  "block_s": 1.0,
  "fast_sync_route_idle": {
   "p50_ms": 0.96,
   "p95_ms": 1.62,
   "max_ms": 2.24,
   "n": 20
  },
  "fast_sync_route_while_saturated": {
   "p50_ms": 1.48,
   "p95_ms": 2.07,
   "max_ms": 2.07,
   "n": 5
  }
 },
 "cold_start": {
  "import_to_first_api_system_s": [
   0.25,
   0.26,
   0.19
  ]
 }
}
```


## P5 storage decision (2026-10-18)

The required corpus is 2,000 chats, 25 messages each (50,000 messages, 32.6 MB JSON).
Without optimization, list p95 is 73.56 ms; a single chat loads in 0.82 ms p95.
With P4's metadata cache, list p95 is 20.87 ms and load p95 is 0.71 ms.
The cache detects external writes, atomic replacements, corrupt JSON and deletion in regression tests.

**Decision: retain JSON.** Both measured operations meet the 50 ms target with the simpler opt-in cache.
SQLite's migrations, dual stores and recovery paths are not justified by these numbers for one user on one Mac.
P5.sqlite's conditional prerequisite is false, so no database, migration, deletion, storage.sqlite flag or SQL query plan
is introduced. Existing JSON data remains authoritative; switching the cache off uses the original list path.
Revisit only if future realistic measurements exceed the target after the cache is enabled.
