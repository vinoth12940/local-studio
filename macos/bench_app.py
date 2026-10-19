"""Offline benchmark of the backend (refactor P0/P4, docs/perf-baseline.md). Run by hand, never by build.sh:

    uv run -q --no-project --python 3.12 --with-requirements requirements-app.txt \
        python macos/bench_app.py [--chats 200] [--json out.json]

Everything runs against a temporary LOCAL_STUDIO_HOME and free ports. The chat engine is a fake OpenAI-style streaming
server, and engine state, model install state and the search service are stubbed in-process, so no real engine, search
service or CuaDriver daemon is started and the ports of the app the user is running (7860, 8080-8083) are never touched.

Measures: p50/p95 overhead per hot route, first-token overhead added by the /api/chat proxy, event-loop lag during 10
concurrent streams, threadpool saturation by slow sync handlers, and cold start (import + first /api/system answer).
"""
import argparse
import asyncio
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(dir="/tmp", prefix="lsbench"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")
os.environ["LOCAL_STUDIO_SEARCH_PORT"] = str(free_port())      # nothing listens there; never the app's 8083
os.environ["LOCAL_STUDIO_SEARCH_ROOTS"] = str(TMP / "no-search-roots")
(TMP / "state").mkdir()
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import app  # noqa: E402
import studio.services.engines  # noqa: E402
import studio.services.models  # noqa: E402
import studio.services.search  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

LAG, PROBE = [], {"on": False}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))] if xs else None


def summary_ms(xs):
    return {"p50_ms": round(pct(xs, 50) * 1000, 2), "p95_ms": round(pct(xs, 95) * 1000, 2), "max_ms": round(max(xs) * 1000, 2), "n": len(xs)}


# ---------------------------------------------------------------- fake engine
fake = FastAPI()
FAKE = {"chunks": 5, "delay": 0.0}


@fake.get("/health")
def fake_health():
    return {"status": "ok"}


@fake.post("/v1/chat/completions")
async def fake_completions(request: Request):
    await request.json()
    chunks, delay = FAKE["chunks"], FAKE["delay"]

    async def gen():
        for _ in range(chunks):
            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "tok "}}]}) + "\n\n"
            if delay:
                await asyncio.sleep(delay)
        yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": chunks}}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def serve(asgi, probe=False):
    """uvicorn in its own thread and event loop; with probe=True a coroutine on that loop records its scheduling lag."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    loop = asyncio.new_event_loop()

    async def lag_probe():
        while not server.should_exit:
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            if PROBE["on"]:
                LAG.append(time.perf_counter() - t - 0.01)

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(asyncio.gather(server.serve(), *([lag_probe()] if probe else [])))

    threading.Thread(target=run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    return server, loop, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------- fixtures
def seed_chats(n, messages=25, chars=600):
    rnd = random.Random(7)
    app.CHATS.mkdir(parents=True, exist_ok=True)
    words = "the model reads files and answers questions about local documents on this mac quickly".split()
    ids = []
    for i in range(n):
        cid = f"{i:016x}"
        msgs = [{"role": "user" if j % 2 == 0 else "assistant", "content": " ".join(rnd.choice(words) for _ in range(chars // 6))}
                for j in range(messages)]
        (app.CHATS / f"{cid}.json").write_text(json.dumps({"id": cid, "title": f"Chat {i}", "updated": time.time() - i, "messages": msgs}))
        ids.append(cid)
    return ids


def seed_workspace(dirs=40, files=50):
    ws = TMP / "workspace"
    for d in range(dirs):
        (ws / f"pkg{d:02d}").mkdir(parents=True, exist_ok=True)
        for f in range(files):
            (ws / f"pkg{d:02d}" / f"module_{f:02d}.py").write_text(f"# file {d}/{f}\n")
    return ws


# ---------------------------------------------------------------- measurements
def hot_routes(base, chat_id, ws, n=200):
    routes = {
        "GET /api/chats": ("GET", "/api/chats", None),
        "GET /api/chats/{id}": ("GET", f"/api/chats/{chat_id}", None),
        "GET /api/workspace/files": ("GET", "/api/workspace/files", {"workspace": str(ws), "q": "mod07", "limit": 25}),
        "GET /api/models (settings)": ("GET", "/api/models", None),
        "GET /api/system": ("GET", "/api/system", None),
    }
    out = {}
    with httpx.Client(base_url=base, timeout=30) as c:
        for label, (method, path, params) in routes.items():
            for _ in range(10):
                c.request(method, path, params=params).raise_for_status()
            times = []
            for _ in range(n):
                t = time.perf_counter()
                c.request(method, path, params=params).raise_for_status()
                times.append(time.perf_counter() - t)
            out[label] = summary_ms(times)
    return out


async def first_token(url, body):
    async with httpx.AsyncClient(timeout=60) as c:
        t = time.perf_counter()
        async with c.stream("POST", url, json=body) as r:
            lines = r.aiter_lines()
            async for line in lines:
                if '"content"' in line:
                    first = time.perf_counter() - t
                    async for _ in lines:                      # drain, so the next run starts on a finished turn
                        pass
                    return first
    raise RuntimeError("no content event in the stream")


def proxy_overhead(base, engine, runs=30):
    FAKE.update(chunks=5, delay=0.0)
    chat_body = {"messages": [{"role": "user", "content": "hello"}]}
    direct_body = {"model": "x", "stream": True, "messages": chat_body["messages"]}
    direct, proxied = [], []
    for _ in range(3):
        asyncio.run(first_token(base + "/api/chat", chat_body))
    for _ in range(runs):
        direct.append(asyncio.run(first_token(engine + "/v1/chat/completions", direct_body)))
        proxied.append(asyncio.run(first_token(base + "/api/chat", chat_body)))
    return {"direct_first_token": summary_ms(direct), "via_api_chat_first_token": summary_ms(proxied),
            "overhead_p50_ms": round((pct(proxied, 50) - pct(direct, 50)) * 1000, 2),
            "overhead_p95_ms": round((pct(proxied, 95) - pct(direct, 95)) * 1000, 2)}


def concurrent_streams(base, streams=10):
    FAKE.update(chunks=300, delay=0.005)                         # ~1.5 s of tokens per stream

    async def one(c):
        n = 0
        async with c.stream("POST", base + "/api/chat", json={"messages": [{"role": "user", "content": "hello"}]}) as r:
            async for line in r.aiter_lines():
                n += '"content"' in line
        return n

    async def run():
        async with httpx.AsyncClient(timeout=120) as c:
            return await asyncio.gather(*(one(c) for _ in range(streams)))
    LAG.clear()
    PROBE["on"] = True
    t = time.perf_counter()
    counts = asyncio.run(run())
    wall = time.perf_counter() - t
    PROBE["on"] = False
    return {"streams": streams, "content_events_each": sorted(set(counts)), "wall_s": round(wall, 2),
            "loop_lag": summary_ms(LAG), "stalls_over_250ms": sum(1 for x in LAG if x > 0.25)}


def threadpool_saturation(base, loop, blockers=60, block_s=1.0):
    """Slow sync handlers (an OCR page render through the search service) hold anyio's shared worker threads; a fast sync
    route then queues behind them."""
    async def tokens():
        import anyio.to_thread
        return anyio.to_thread.current_default_thread_limiter().total_tokens
    limit = asyncio.run_coroutine_threadsafe(tokens(), loop).result(5)

    class Slow:
        content, headers, status_code = b"\xff\xd8", {"x-pages": "1"}, 200
    real = studio.services.search._search_proxy
    studio.services.search._search_proxy = lambda *a, **k: (time.sleep(block_s), Slow())[1]

    async def run():
        async with httpx.AsyncClient(timeout=60) as c:
            idle = []
            for _ in range(20):
                t = time.perf_counter(); await c.get(base + "/api/busy"); idle.append(time.perf_counter() - t)
            slow = [asyncio.create_task(c.get(base + f"/api/search/page/{i}/1")) for i in range(blockers)]
            await asyncio.sleep(0.1)
            busy = []
            for _ in range(5):
                t = time.perf_counter(); await c.get(base + "/api/busy"); busy.append(time.perf_counter() - t)
            await asyncio.gather(*slow)
            return idle, busy
    try:
        idle, busy = asyncio.run(run())
    finally:
        studio.services.search._search_proxy = real
    return {"anyio_thread_limit": limit, "blocking_requests": blockers, "block_s": block_s,
            "fast_sync_route_idle": summary_ms(idle), "fast_sync_route_while_saturated": summary_ms(busy)}


def cold_start(runs=3):
    out = []
    for i in range(runs):
        port, home = free_port(), TMP / f"cold{i}"
        home.mkdir()
        env = {**os.environ, "LOCAL_STUDIO_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        t = time.perf_counter()
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port),
                              "--log-level", "warning"], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while time.perf_counter() - t < 30:
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/api/system", timeout=0.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.05)
            out.append(time.perf_counter() - t)
        finally:
            p.terminate(); p.wait(10)
    return {"import_to_first_api_system_s": [round(x, 2) for x in out]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chats", type=int, default=200)
    ap.add_argument("--json")
    ap.add_argument("--optimized", action="store_true", help="enable the measured P4 pool and chat metadata fixes")
    args = ap.parse_args()

    if args.optimized:
        app.save_settings({**app.load_settings(), "performance": {"isolated_io": True, "chat_index": True}})
    _, _, engine = serve(fake)
    m = app.chat_model()
    app.ENGINES["text"]["url"] = engine                           # in this process only: the fake engine, never port 8080
    studio.services.models.model_installed = lambda model: True       # patched where the code reads them (studio/)
    studio.services.engines.engine_state = lambda: {"mode": "text", "model_id": m["id"]}
    studio.services.engines._engine_ready = lambda mode: True
    studio.services.engines.ensure_engine = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bench never starts an engine"))
    server, loop, base = serve(app.app, probe=True)

    ids = seed_chats(args.chats)
    ws = seed_workspace()
    chats_bytes = sum(p.stat().st_size for p in app.CHATS.glob("*.json"))
    result = {
        "optimized": args.optimized,
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "mac": platform.machine() + " " + platform.mac_ver()[0],
        "python": platform.python_version(),
        "fixture": {"chats": args.chats, "chats_mb": round(chats_bytes / 1e6, 1), "workspace_files": 2000},
        "hot_routes": hot_routes(base, ids[len(ids) // 2], ws),
        "proxy": proxy_overhead(base, engine),
        "concurrent_streams": concurrent_streams(base),
        "threadpool": threadpool_saturation(base, loop),
        "cold_start": cold_start(),
    }
    server.should_exit = True
    text = json.dumps(result, indent=1)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n")


if __name__ == "__main__":
    main()
