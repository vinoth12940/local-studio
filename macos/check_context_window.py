"""Offline checks for the configurable maximum context window. No engine is started; engine processes are faked at the
subprocess boundary so the real ensure_engine/_start_engine decisions and command lines are exercised."""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ls-ctx-", dir="/tmp")).resolve()
os.environ["LOCAL_STUDIO_HOME"] = str(TMP / "state")
os.environ["HF_HUB_CACHE"] = str(TMP / "hub")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from studio import config
from studio.api import engines as api_engines
from studio.main import app
from studio.services import engines, harness, models
from studio.storage.settings import load_settings, save_settings

client = TestClient(app, base_url="http://127.0.0.1:7860")
ORIGINAL = config.CONTEXT


def fake_model(name, config_json, **extra):
    """A cached HF snapshot with just a config.json, so the real native-limit reader runs."""
    snap = models.repo_dir(f"org/{name}") / "snapshots" / "abc"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "config.json").write_text(json.dumps(config_json))
    return {"id": name, "name": name.upper(), "repo": f"org/{name}", **extra}


def restore():
    config.CONTEXT = ORIGINAL
    settings = load_settings(); settings.pop("context_window", None); save_settings(settings)


def test_config_reads_only_valid_values_from_settings():
    path = config.SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text() if path.exists() else None
    d = config.DEFAULT_CONTEXT
    try:
        for value, expected in ((100000, 100000), (150000, 150000), (262144, 262144), (131072, 131072), (99999, d), (65536, d), (32768, d), (4096, d),
                                (262145, d), (0, d), (-5, d), (True, d), (False, d), (131072.0, d), ("131072", d), (None, d), ([131072], d)):
            path.write_text(json.dumps({"context_window": value}))
            assert config._configured_context() == expected, (value, config._configured_context())
        path.write_text("{ not json")
        assert config._configured_context() == config.DEFAULT_CONTEXT
        path.write_text(json.dumps({"chat_model": "x"}))
        assert config._configured_context() == config.DEFAULT_CONTEXT
        path.unlink()
        assert config._configured_context() == config.DEFAULT_CONTEXT
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original)
    assert (config.DEFAULT_CONTEXT, config.MIN_CONTEXT, config.MAX_CONTEXT, config.LEGACY_CONTEXT) == (131072, 100000, 262144, 32768)


def test_api_validates_persists_and_updates_the_live_value():
    saved_model = api_engines.chat_model
    api_engines.chat_model = lambda: {"id": "m", "name": "M", "repo": None}
    try:
        settings = load_settings(); settings["chat_model"] = "keep-me"; save_settings(settings)
        first = client.get("/api/context-window").json()
        assert first["value"] == 131072 and first["default"] == 131072 and first["min"] == 100000 and first["max"] == 262144
        assert first["effective"] == 131072 and first["native_max"] is None
        assert first["presets"] == [100000, 131072, 196608, 262144] and first["restart_required"] is False
        done = client.post("/api/context-window", json={"value": 150000})
        assert done.status_code == 200 and done.json()["value"] == 150000 and config.CONTEXT == 150000
        saved = json.loads(config.SETTINGS.read_text())
        assert saved["context_window"] == 150000 and saved["chat_model"] == "keep-me", "other settings are preserved"
        assert client.get("/api/context-window").json()["value"] == 150000
        assert config._configured_context() == 150000, "a restart of the app reads the saved value"
        for bad in (0, 4095, 65536, 99999, 262145, -1, True, False, 3.5, "128000", [1], {"v": 1}, ""):
            r = client.post("/api/context-window", json={"value": bad})
            assert r.status_code == 400 and "between 100,000 and 262,144" in r.text, (bad, r.status_code, r.text)
            assert config.CONTEXT == 150000, "a rejected value changes nothing"
        for body in ({}, {"val": 1}, {"value": 150000, "extra": 1}):
            assert client.post("/api/context-window", json=body).status_code == 400, body
        assert client.post("/api/context-window", json={"value": 100000}).json()["value"] == 100000
        assert client.post("/api/context-window", json={"value": 262144}).json()["value"] == 262144
        reset = client.post("/api/context-window", json={"value": None}).json()
        assert reset["value"] == 131072 and config.CONTEXT == 131072 and "context_window" not in json.loads(config.SETTINGS.read_text())
        assert client.post("/api/context-window", json={"value": 131072}).status_code == 200
        assert "context_window" not in json.loads(config.SETTINGS.read_text()), "the default is stored as 'unset'"
    finally:
        api_engines.chat_model = saved_model
        restore()


def test_restart_required_follows_the_window_the_running_engine_started_with():
    saved, saved_model = engines.engine_state, api_engines.chat_model
    api_engines.chat_model = lambda: {"id": "m", "name": "M", "repo": None}
    try:
        config.CONTEXT = 150000
        engines.engine_state = lambda: {"mode": "text", "model_id": "m", "context_window": 131072}
        r = client.get("/api/context-window").json()
        assert r["restart_required"] is True and r["engine"]["started_with"] == 131072 and "automatically" in r["applies"], r
        engines.engine_state = lambda: {"mode": "text", "model_id": "m", "context_window": 150000}
        assert client.get("/api/context-window").json()["restart_required"] is False
        engines.engine_state = lambda: {"mode": "text", "model_id": "m"}            # started before the window was recorded: it was 32768
        r = client.get("/api/context-window").json()
        assert r["restart_required"] is True and r["engine"]["started_with"] == config.LEGACY_CONTEXT == 32768
        engines.engine_state = lambda: {"mode": None, "model_id": "m"}
        r = client.get("/api/context-window").json()
        assert r["restart_required"] is False and r["engine"]["started_with"] is None
    finally:
        engines.engine_state, api_engines.chat_model = saved, saved_model; restore()


def test_memory_note_is_advisory_and_only_for_models_that_may_not_fit():
    big = fake_model("big", {"model_type": "llama", "num_hidden_layers": 40, "num_key_value_heads": 8, "num_attention_heads": 32,
                             "hidden_size": 4096, "max_position_embeddings": 262144})
    saved = (api_engines._ram_gb, api_engines.chat_model, models._repo_bytes)
    api_engines.chat_model = lambda: big
    models._repo_bytes = lambda repo: 30 * 2**30
    try:
        config.CONTEXT = 131072
        api_engines._ram_gb = lambda: 48
        r = client.get("/api/context-window").json()
        assert r["memory_estimate_gb"] and r["memory_estimate_gb"] > 36, r["memory_estimate_gb"]
        assert any("may need up to about" in n and "48 GB" in n for n in r["notes"]), r["notes"]
        api_engines._ram_gb = lambda: 128
        assert not any("may need up to" in n for n in client.get("/api/context-window").json()["notes"])
        api_engines._ram_gb = lambda: None
        assert not any("may need up to" in n for n in client.get("/api/context-window").json()["notes"])
        api_engines.chat_model = lambda: {"id": "x", "name": "X", "repo": None}      # unknown architecture: no figure, no note
        api_engines._ram_gb = lambda: 16
        r = client.get("/api/context-window").json()
        assert r["memory_estimate_gb"] is None and not any("may need up to" in n for n in r["notes"]) and any("vision engine" in n for n in r["notes"])
        hybrid = fake_model("hybrid", {"text_config": {"num_hidden_layers": 40, "num_key_value_heads": 2, "num_attention_heads": 16, "hidden_size": 2048,
                                                      "head_dim": 256, "layer_types": ["linear_attention"] * 30 + ["full_attention"] * 10}})
        full = fake_model("allfull", {"num_hidden_layers": 40, "num_key_value_heads": 2, "num_attention_heads": 16, "hidden_size": 2048, "head_dim": 256})
        models._repo_bytes = lambda repo: 0
        assert api_engines._memory_estimate(hybrid, 131072) < api_engines._memory_estimate(full, 131072) / 3, "only full-attention layers keep a KV cache"
    finally:
        api_engines._ram_gb, api_engines.chat_model, models._repo_bytes = saved; restore()


def test_a_model_is_clamped_to_its_own_native_window():
    plain = fake_model("plain", {"max_position_embeddings": 65536})
    nested = fake_model("nested", {"model_type": "vlm", "text_config": {"max_position_embeddings": 262144}, "max_position_embeddings": 4096})
    roomy = fake_model("roomy", {"max_position_embeddings": 1048576})
    broken = fake_model("broken", {})
    (models.repo_dir("org/broken") / "snapshots" / "abc" / "config.json").write_text("{ not json")
    try:
        config.CONTEXT = 131072
        assert models.native_context(plain) == 65536 and models.effective_context(plain) == 65536, "the setting is a ceiling, not a promise"
        assert models.native_context(nested) == 262144, "a vision model's text_config wins over a top-level value"
        assert models.effective_context(roomy) == 131072, "a roomy model uses the setting"
        assert models.native_context({"id": "g", "repo": "org/not-downloaded", "backend": "gguf"}) is None
        assert models.effective_context({"id": "g", "repo": "org/not-downloaded"}) == 131072, "unknown native limit: no clamp"
        assert models.native_context(broken) is None and models.effective_context(broken) == 131072
        assert models.effective_context({**roomy, "context": 98304}) == 98304, "a catalog 'context' overrides config.json"
        assert models.native_context({**roomy, "context": 0}) == 1048576 and models.native_context({**roomy, "context": True}) == 1048576
        config.CONTEXT = 100000
        assert models.effective_context(plain) == 65536 and models.effective_context(roomy) == 100000
        saved = api_engines.chat_model
        api_engines.chat_model = lambda: plain
        config.CONTEXT = 131072
        r = client.get("/api/context-window").json()
        api_engines.chat_model = saved
        assert r["effective"] == 65536 and r["native_max"] == 65536 and any("supports at most 65,536" in n for n in r["notes"]), r
    finally:
        restore()


class FakeEngines:
    """Replaces only process control; ensure_engine's own decision logic runs. It tracks which engines are 'running'."""
    def __init__(self, state, ready=True):
        self.state, self.ready, self.stopped, self.started = state, ready, [], []
        self.running = {state.get("mode")} - {None}

    def __enter__(self):
        self.saved = (engines.engine_state, engines._engine_ready, engines._proc_running, engines._stop_engine, engines._start_engine)
        mode_of = {entry["pattern"]: mode for mode, entry in config.ENGINES.items()}
        engines.engine_state = lambda: dict(self.state)
        engines._engine_ready = lambda mode: self.ready and mode in self.running
        engines._proc_running = lambda pattern: mode_of[pattern] in self.running
        engines._stop_engine = lambda mode: (self.stopped.append(mode), self.running.discard(mode))
        engines._start_engine = lambda mode, m: (self.started.append((mode, models.effective_context(m))), self.running.add(mode))
        return self

    def __exit__(self, *exc):
        (engines.engine_state, engines._engine_ready, engines._proc_running, engines._stop_engine, engines._start_engine) = self.saved


def test_ensure_engine_restarts_only_when_the_window_changed():
    model = {"id": "m", "name": "M", "repo": "r"}
    try:
        config.CONTEXT = 131072
        with FakeEngines({"mode": "text", "model_id": "m", "context_window": 131072}) as fake:
            assert engines.ensure_engine("text", model) is False and not fake.stopped and not fake.started
        config.CONTEXT = 150000
        with FakeEngines({"mode": "text", "model_id": "m", "context_window": 131072}) as fake:
            assert engines.ensure_engine("text", model) is True
            assert fake.stopped == ["text"] and fake.started == [("text", 150000)], (fake.stopped, fake.started)
        with FakeEngines({"mode": "text", "model_id": "m", "context_window": 150000}) as fake:
            assert engines.ensure_engine("text", model) is False and not fake.started, "same window: reuse the engine"
        with FakeEngines({"mode": "text", "model_id": "m"}) as fake:                      # recorded nowhere: it really runs the old 32768
            assert engines.ensure_engine("text", model) is True and fake.started == [("text", 150000)]
        config.CONTEXT = 131072
        with FakeEngines({"mode": "text", "model_id": "m"}) as fake:
            assert engines.ensure_engine("text", model) is True, "an old 32k engine is replaced now that the default is 128k"
        limited = {"id": "m", "name": "M", "repo": "r", "context": 65536}
        with FakeEngines({"mode": "text", "model_id": "m", "context_window": 65536}) as fake:
            assert engines.ensure_engine("text", limited) is False, "a model's own limit is the window it runs at, so no restart loop"
        with FakeEngines({"mode": "text", "model_id": "m", "context_window": 131072}) as fake:
            assert engines.ensure_engine("text", limited) is True and fake.started == [("text", 65536)]
    finally:
        restore()


def test_started_engines_get_the_window_on_their_command_line_and_record_it():
    class Proc:
        pid = 4242
    seen = []
    saved = (engines.subprocess.Popen, engines.snapshot_file)
    engines.subprocess.Popen = lambda cmd, **kw: (seen.append(list(map(str, cmd))), Proc())[1]
    engines.snapshot_file = lambda repo, name: TMP / "model.gguf"
    try:
        for value in (100000, 131072):
            config.CONTEXT = value
            engines._start_engine("text", {"id": "t", "name": "T", "repo": "org/t"})
            cmd = seen[-1]
            assert cmd[cmd.index("--context-window") + 1] == str(value), cmd
            assert json.loads(engines.ENGINE_STATE.read_text()) == {"mode": "text", "model_id": "t", "context_window": value}
            engines._start_engine("gguf", {"id": "g", "name": "G", "repo": "org/g", "file": "g.gguf", "backend": "gguf"})
            cmd = seen[-1]
            assert cmd[cmd.index("-c") + 1] == str(value), cmd
            assert json.loads(engines.ENGINE_STATE.read_text())["context_window"] == value
        config.CONTEXT = 131072
        engines._start_engine("text", {"id": "t", "name": "T", "repo": "org/t", "context": 65536})
        cmd = seen[-1]
        assert cmd[cmd.index("--context-window") + 1] == "65536" and json.loads(engines.ENGINE_STATE.read_text())["context_window"] == 65536
        engines._start_engine("vision", {"id": "qwen3.8-27b", "name": "V", "repo": "org/v", "drafter": "org/d"})
        assert "--context-window" not in seen[-1] and "-c" not in seen[-1], "mlx-vlm has no window flag; its own default applies"
    finally:
        engines.subprocess.Popen, engines.snapshot_file = saved
        restore()


def test_budgets_follow_the_configured_window():
    def fitted(window, tokens):
        config.CONTEXT = window
        turns = []
        for index in range(tokens // 1000):
            turns += [{"role": "user", "content": f"note {index}: " + "word " * 620}, {"role": "assistant", "content": "ok"}]
        body = {"messages": [{"role": "system", "content": "s"}] + turns + [{"role": "user", "content": "latest"}], "tools": [], "max_tokens": 16384}
        harness._chat_fit_context(body)
        return body, len(turns)
    try:
        big, total = fitted(131072, 60000)
        assert len(big["messages"]) == total + 2, "a 60k-token chat fits a 128k window untouched"
        assert big["max_tokens"] == 16384
        small, total = fitted(16384, 60000)
        assert len(small["messages"]) < total // 2 and small["messages"][-1]["content"] == "latest"
        assert "### Conversation Checkpoint Summary" in small["messages"][0]["content"]
        assert small["max_tokens"] >= 3000, small["max_tokens"]
        tiny, _ = fitted(4096, 20000)
        assert tiny["max_tokens"] >= 512 and tiny["messages"][-1]["content"] == "latest"
    finally:
        restore()


TESTS = (test_config_reads_only_valid_values_from_settings, test_api_validates_persists_and_updates_the_live_value,
         test_restart_required_follows_the_window_the_running_engine_started_with, test_memory_note_is_advisory_and_only_for_models_that_may_not_fit, test_a_model_is_clamped_to_its_own_native_window,
         test_ensure_engine_restarts_only_when_the_window_changed, test_started_engines_get_the_window_on_their_command_line_and_record_it,
         test_budgets_follow_the_configured_window)

if __name__ == "__main__":
    for function in TESTS:
        function()
        print("   ok " + function.__name__)
    print("   context window checks OK")
