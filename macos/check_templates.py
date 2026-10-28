"""Every request the chat route sends an engine must render with each installed model's own chat template.

Offline: no engine is started, stopped or contacted. The real /api/chat runs in-process against the eval kit's scripted
engine, which records each request body; each body is then rendered with the chat template of every installed MLX chat
model (read from the Hugging Face cache, rendered by transformers in the mlx-dspark tool environment). This is the check
that would have caught "could not apply chat template: System message must be at the beginning" before a user did.

Models that are not installed are skipped (named in the output); without the mlx-dspark environment the rendering step
is skipped with a note, and the structural checks still run.
"""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "evals"))
sys.path.insert(0, str(HERE.parent))
TMP = Path(tempfile.mkdtemp(prefix="ls-templates-", dir="/tmp")).resolve()
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")
os.environ.pop("LOCAL_STUDIO_EVAL_ATTACH", None)

import evalkit  # noqa: E402
from studio.services import engines as _engines  # noqa: E402


def _no_engine(*args, **kwargs):
    raise AssertionError("the template check reached a real engine process")


_engines._start_engine = _engines._stop_engine = _no_engine
RENDER_PY = Path.home() / ".local/share/uv/tools/mlx-dspark/bin/python3"

# What the renderer runs (in the mlx-dspark environment, which has transformers): argv = bodies.json snapshot-dir...
RENDERER = r'''
import json, sys
from transformers import AutoTokenizer
bodies = json.load(open(sys.argv[1]))
# As the engine's OpenAI-compatible server does before templating: tool-call arguments arrive as JSON text.
for b in bodies:
    for m in b["messages"]:
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {})
            if isinstance(fn.get("arguments"), str):
                try: fn["arguments"] = json.loads(fn["arguments"] or "{}")
                except ValueError: pass
out = {}
for snap in sys.argv[2:]:
    tok = AutoTokenizer.from_pretrained(snap)
    results = []
    for b in bodies:
        try:
            tok.apply_chat_template(b["messages"], tools=b.get("tools"), add_generation_prompt=True, tokenize=False)
            results.append(None)
        except Exception as e:
            results.append(f"{type(e).__name__}: {str(e)[:200]}")
    out[snap] = results
print(json.dumps(out))
'''


def capture(body, turns):
    """The request bodies /api/chat sends for one exchange, with the scripted model answering `turns`."""
    from fastapi.testclient import TestClient
    from studio.main import app
    from studio.storage.settings import load_settings
    client = TestClient(app, base_url="http://127.0.0.1:7860")
    context = evalkit.Scripted(turns)
    with context:
        r = client.post("/api/chat", json={"model_id": load_settings()["chat_model"], "chat_id": uuid.uuid4().hex[:16], **body})
    assert r.status_code == 200, r.text[:300]
    events = evalkit.parse_stream(r.text)
    errors = [e["error"] for e in events if isinstance(e, dict) and e.get("error")]
    assert not errors, errors
    return context.seen


def scenarios():
    from studio.services import harness
    hist = [{"role": "user", "content": "What is the capital of France?"}, {"role": "assistant", "content": "Paris."}]
    out = {}
    out["typed"] = capture({"messages": [{"role": "user", "content": "Hi"}]}, [{"text": "Hello."}])
    out["typed with the user's own system prompt"] = capture(
        {"messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}]}, [{"text": "Hello."}])
    out["spoken"] = capture({"messages": hist + [{"role": "user", "content": "And Spain?"}], "voice": True,
                             "voice_style": "a calm British butler"}, [{"text": "Madrid."}])
    out["tool round"] = capture({"messages": [{"role": "user", "content": "What time is it?"}]},
                                [{"tool": "get_current_datetime", "args": {}}, {"text": "It is noon."}])
    out["spoken tool round"] = capture({"messages": hist + [{"role": "user", "content": "What time is it?"}], "voice": True,
                                        "voice_style": "a calm British butler"},
                                       [{"tool": "get_current_datetime", "args": {}}, {"text": "It is noon."}])
    out["voice warm-up"] = capture({"messages": [{"role": "user", "content": "Hello."}], "warm": True, "voice": True,
                                    "voice_style": "a calm British butler"}, [{"text": "Hi"}])
    # Notes the loop adds mid-conversation (workflow policy, the verification gate, a resumed run) must reach the engine
    # folded into the leading system message: built the way the route builds them, then through the engine boundary.
    late = [{"role": "system", "content": "agent note"}, {"role": "user", "content": "Fix it."},
            {"role": "assistant", "content": "Done."}, {"role": "system", "content": "verification failed: rerun the tests"},
            {"role": "user", "content": "Go on."}]
    out["late system notes"] = [{"messages": harness.fold_system_messages(late)}]
    out["_unfolded (negative control)"] = [{"messages": late}]
    return out


def structure(all_bodies):
    for name, bodies in all_bodies.items():
        if name.startswith("_"):
            continue
        for b in bodies:
            roles = [m["role"] for m in b["messages"]]
            assert roles.count("system") == 1 and roles[0] == "system", (name, roles)
            assert "voice" not in b and "voice_style" not in b, name
        print(f"   ok one leading system message: {name} ({len(bodies)} request{'s' * (len(bodies) != 1)})")
    spoken = all_bodies["spoken"][0]["messages"]
    from studio.services import harness
    assert harness.VOICE_NOTE in spoken[0]["content"] and spoken[-1]["content"].endswith(harness.VOICE_TURN_REMINDER)
    print("   ok spoken turns carry the voice note and the reminder")
    # The clock changes every minute, so it is the system text's last line: everything before it stays in the engine's cache.
    for name in ("typed", "spoken", "voice warm-up"):
        system = all_bodies[name][0]["messages"][0]["content"]
        assert system.rstrip().splitlines()[-1].startswith(harness.CLOCK_LINE), (name, system[-200:])
    warm, turn = all_bodies["voice warm-up"][0], all_bodies["spoken"][0]
    assert warm["max_tokens"] == 1, warm["max_tokens"]
    cut = lambda b: b["messages"][0]["content"].rsplit("\n\n" + harness.CLOCK_LINE, 1)[0]
    assert cut(warm) == cut(turn) and warm["tools"] == turn["tools"], "the warm-up must prime exactly what a spoken turn sends"
    print("   ok the clock is the last line; the voice warm-up is one token and primes a spoken turn's tools and instructions")


def installed_templates():
    from studio.services import models
    snaps, skipped = {}, []
    for m in models.chat_models():
        if m.get("backend") != "mlx":
            continue
        d = models.repo_dir(m["repo"]) / "snapshots"
        found = sorted(d.glob("*")) if d.is_dir() else []
        snap = next((s for s in found if (s / "tokenizer_config.json").exists()), None)
        if snap and models.model_installed(m):
            snaps[m["id"]] = snap
        else:
            skipped.append(m["id"])
    return snaps, skipped


def render(all_bodies):
    snaps, skipped = installed_templates()
    if skipped:
        print(f"   skip not installed: {', '.join(skipped)}")
    if not snaps:
        print("   skip rendering: no installed MLX chat model")
        return
    if not RENDER_PY.exists():
        print(f"   skip rendering: {RENDER_PY} not found (install mlx-dspark with setup.sh --install)")
        return
    names = list(all_bodies)
    flat = [b for n in names for b in all_bodies[n]]
    owner = [n for n in names for _ in all_bodies[n]]
    path = TMP / "bodies.json"
    path.write_text(json.dumps(flat))
    proc = subprocess.run([str(RENDER_PY), "-I", "-c", RENDERER, str(path), *map(str, snaps.values())],
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-800:]
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    for mid, snap in snaps.items():
        res = results[str(snap)]
        failed = {owner[i]: err for i, err in enumerate(res) if err and not owner[i].startswith("_")}
        assert not failed, (mid, failed)
        control = [err for i, err in enumerate(res) if owner[i].startswith("_")]
        note = "rejects" if control and control[0] else "accepts"
        print(f"   ok {mid}: all {len(flat) - len(control)} requests render (it {note} a late system message)")


if __name__ == "__main__":
    bodies = scenarios()
    structure(bodies)
    render(bodies)
    print("   template checks OK")
