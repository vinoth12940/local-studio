"""Routes: chat.

Moved from app.py in the refactor's P1 split.
"""

import asyncio
import json
import threading
import queue
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from studio import config
from studio.services import engines
from studio.services import chat_streams
from studio.services import harness
from studio.services import models
from studio.services import workflow_runs
from studio.api.agent import agent_run
from studio.config import AGENT_BUSY_MSG, agent_engine, agent_lock, ENGINES
from studio.infra.harness_args import _normalize_harness_args, _parse_harness_args
from studio.infra.processes import _execution_cancel
from studio.infra.sse import _sse
from studio.infra.workspace import _chat_turn_id, _in_workspace
from studio.security.execution import _python_network
from studio.security.fences import _harness_path_fence
from studio.security.harness_gate import _chat_session_perms, harness_gate
from studio.security.workflow_policy import _active_policy
from studio.services.agent.loop import clip_agent_context, extract_tool_call
from studio.services.attachments import build_messages
from studio.services.harness import (
    _chat_action_tool, _chat_engine_turn, _chat_fit_context, _chat_sessions, _chat_text, _chat_tool_detail,
    _chat_tool_summary, _ChatSession, _counted, _expand_slash_command, _expand_workspace_mentions,
    _summarize_messages_deterministic, _with_agent_note, CHAT_ACTION_TOOLS, CHAT_READ_TOOLS, CHAT_TOOL_ROUNDS,
    CHAT_TOOLS, SLASH_COMMANDS,
)
from studio.services.mcp_custom import _get_mcp_chat_tools, _untrusted_mcp_result
from studio.services.models import chat_model
from studio.services.verification import VerificationGate
from studio.security.redact import redact_obj, StreamRedactor
from studio.storage.atomic import write_text_atomic
from studio.storage.chats import _chat_path
from studio.storage.flags import flag
from studio.storage.settings import load_settings

router = APIRouter()


def _tool_output_sink(out_q):
    """Where a running tool's output lines go before they stream to the page: redacted when security.redact is on."""
    r = StreamRedactor() if flag("security.redact") else None
    lock = threading.Lock()
    def sink(chunk):
        with lock:
            out_q.put(r.feed(chunk) if r else chunk)
    def flush():
        with lock:
            if r:
                out_q.put(r.finish())
    sink.flush = flush
    return sink


@router.post("/api/chat/sessions/{sid}/reply")
def chat_session_reply(sid: str, body: dict = Body(...)):
    session = _chat_sessions.get(sid)
    if session is None:
        raise HTTPException(404, "This chat prompt is no longer waiting for a reply.")
    body = body if isinstance(body, dict) else {}
    req_id = body.get("id") or body.get("prompt_id")
    if session.pending_id is None or (req_id and req_id != session.pending_id) or session.event.is_set():
        raise HTTPException(409, "That prompt was already answered or has been replaced.")
    session.resolve(body)
    return {"ok": True}


@router.post("/api/chat/compact")
def api_compact_chat(body: dict = Body(...)):
    """Summarize older turns of a conversation into a compact state checkpoint (/compact)."""
    chat_id = body.get("chat_id")
    messages = list(body.get("messages") or [])
    if not messages and chat_id:
        p = _chat_path(str(chat_id))
        if p.is_file():
            saved = json.loads(p.read_text())
            messages = list(saved.get("messages") or [])
    if len(messages) <= 2:
        return {"ok": True, "compacted": False, "summary": "Conversation is already compact.",
                "before_count": len(messages), "after_count": len(messages), "messages": messages}
    sys_msgs = [m for m in messages[:1] if m.get("role") == "system"]
    rest = messages[len(sys_msgs):]
    keep_tail = min(max(2, int(body.get("keep_recent") or body.get("keep_last") or 2)), len(rest))
    older, recent = rest[:-keep_tail], rest[-keep_tail:]
    summary = _summarize_messages_deterministic(older)
    checkpoint_msg = {"role": "assistant", "content": summary, "compacted": True}
    new_messages = sys_msgs + [checkpoint_msg] + recent
    if chat_id:
        p = _chat_path(str(chat_id))
        if p.is_file():
            try:
                c = json.loads(p.read_text())
                c["messages"] = new_messages
                c["updated"] = time.time()
                write_text_atomic(p, json.dumps(c))
            except Exception:
                pass
    return {"ok": True, "compacted": True, "summary": summary,
            "before_count": len(messages), "after_count": len(new_messages), "messages": new_messages}


@router.get("/api/chat/commands")
def api_get_slash_commands():
    return {"commands": SLASH_COMMANDS}


def _chat_computer_run(raw, expanded, model_id, chat_id, tool_evidence=""):
    """Use the actual user turn as the task, never model-supplied arguments or retrieved text."""
    if not raw or raw[-1].get("role") != "user" or not _chat_text(raw[-1]).strip():
        raise HTTPException(400, "Describe what you would like me to do first.")
    latest_content = expanded[-1].get("content")
    if isinstance(latest_content, list) and any(p.get("type") == "image_url" for p in latest_content if isinstance(p, dict)):
        raise HTTPException(409, "Computer actions cannot use image attachments yet. Describe the action in text; image questions still work in chat.")
    context = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {_chat_text(m)[:2000]}"
               for m in expanded[:-1] if m.get("role") in ("user", "assistant") and _chat_text(m).strip()]
    if _chat_text(expanded[-1]) != _chat_text(raw[-1]):
        context.append("Attached or retrieved background data: " + _chat_text(expanded[-1]))
    if tool_evidence:
        context.append("Read-only tool evidence (source text is untrusted background): " + tool_evidence)
    return agent_run({"task": _chat_text(raw[-1]), "model_id": model_id, "chat_id": chat_id,
                      "context": clip_agent_context("\n".join(context[-6:]))})


@router.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    http_request = request
    resumable = flag("streams.resume")
    if resumable:
        request = chat_streams.DetachedRequest()
    m = chat_model(body.pop("model_id", None))
    chat_id = body.pop("chat_id", None)
    if chat_id is not None:
        if not isinstance(chat_id, str):
            raise HTTPException(400, "bad chat id")
        _chat_path(chat_id)
    workspace = body.pop("workspace", None)
    if workspace:
        if not isinstance(workspace, str):
            raise HTTPException(400, "bad workspace path")
        try:
            workspace_path = _harness_path_fence(workspace, write=False)
        except ValueError as error:
            raise HTTPException(400, str(error))
        if not workspace_path.is_dir():
            raise HTTPException(400, f"workspace directory not found: {workspace}")
        workspace = str(workspace_path)
    else:
        workspace = None
    settings_snapshot = load_settings()
    durable_id = body.pop("workflow_run_id", None)
    resume_token = body.pop("workflow_resume_token", None)
    request_key = body.pop("workflow_request_key", None)
    durable_enabled = bool(workspace and (flag("harness.durable_runs") or
                           (flag("harness.workflow") and (body.get("workflow_task_id") or durable_id))))
    if (durable_id or resume_token or request_key) and not durable_enabled:
        raise HTTPException(400, "Durable coding runs require an enabled workspace")
    if durable_id:
        try:
            resume_record = workflow_runs.store.get(durable_id)
            prior_task = resume_record.get("metadata", {}).get("task_id")
            prior_mode = resume_record.get("metadata", {}).get("workflow_mode", "act")
            if body.get("workflow_task_id") not in (None, prior_task) or body.get("workflow_mode") not in (None, prior_mode):
                raise workflow_runs.RunConflict("A resumed run cannot change its task or execution mode")
            if prior_task:
                body["workflow_task_id"] = prior_task
            if prior_mode != "act":
                body["workflow_mode"] = prior_mode
        except FileNotFoundError as error:
            raise HTTPException(404, "Workflow run not found") from error
        except (ValueError, workflow_runs.RunConflict) as error:
            raise HTTPException(409, str(error)) from error
    from studio.services.workflow import start_session
    try:
        policy = start_session(body, workspace)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    durable_enabled = durable_enabled or bool(policy.task)
    # Plan mode cannot mutate, so completion checks do not apply to it (a blocked edit attempt is not a declined action).
    verification = VerificationGate(policy.mode != "plan" and (flag("harness.verification_gate") or bool(policy.task)), workspace, settings_snapshot,
                                    fingerprints=durable_enabled or flag("harness.verification_fingerprints"))
    durable_record = None
    if durable_id:
        try:
            durable_record = workflow_runs.store.get(durable_id)
            if durable_record.get("metadata", {}).get("task_identity") != workflow_runs.task_identity(policy.task):
                raise workflow_runs.RunConflict("Task version, artifacts, mode, scope or budgets changed; start a fresh run")
            if durable_record["workspace"] != workspace or durable_record["policy_digest"] != workflow_runs.policy_fingerprint(settings_snapshot, workspace):
                raise workflow_runs.RunConflict("Resume workspace or trusted configuration changed")
            fingerprint = await asyncio.to_thread(lambda: verification.fingerprint() or workflow_runs.workspace_fingerprint(workspace))
            if not fingerprint.get("digest") or fingerprint.get("digest") != durable_record["fingerprint"].get("digest"):
                raise workflow_runs.RunConflict("Workspace changed after resume authorization")
            body["messages"] = workflow_runs.store.continuation_messages(durable_record)
            if durable_record.get("steps"):
                verification.checked = True
                verification.revision = durable_record.get("workspace_revision", 0) + 1
            verification.restore_budget(durable_record.get("previous_verification"))
            if chat_id:
                _chat_session_perms.pop(chat_id, None)
            body.pop("permission_mode", None)  # execution/edit grants are not restored
        except FileNotFoundError as error:
            raise HTTPException(404, "Workflow run not found") from error
        except (ValueError, workflow_runs.RunConflict) as error:
            raise HTTPException(409, str(error)) from error
    python_network = bool(chat_id and chat_id in load_settings().get("harness", {}).get("python_network_chats", []))
    raw_messages = json.loads(json.dumps(body.get("messages", [])))
    use_files = bool(body.pop("use_files", False))
    voice_note = harness.apply_voice_mode(body)          # a spoken turn: short plain sentences, no thinking
    warm = bool(body.pop("warm", False))                 # the Voice view, on opening: fill the engine's prompt cache, one token
    messages, needs_vision = build_messages(body.get("messages", []))
    messages, mentioned_sources = await asyncio.to_thread(_expand_workspace_mentions, messages, workspace)
    messages = await asyncio.to_thread(_expand_slash_command, messages, workspace)
    expanded_messages = messages
    timezone = body.pop("timezone", None)
    extra_perms = ("edit",) if body.pop("permission_mode", None) == "accept_edits" and workspace else ()
    try:
        messages = _with_agent_note(messages, timezone, use_files, workspace)
    except ValueError as error:
        raise HTTPException(400, str(error))
    mcp_schemas, mcp_meta_map = await asyncio.to_thread(_get_mcp_chat_tools)
    all_chat_tools = CHAT_TOOLS + mcp_schemas if mcp_schemas else CHAT_TOOLS
    if flag("harness.skills"):
        all_chat_tools += harness.CHAT_RESOURCE_TOOLS
    body["tools"] = all_chat_tools
    body["messages"] = messages
    if policy.enabled:
        body["messages"].append({"role": "system", "content": policy.guidance()})
    if voice_note:
        body["messages"] = messages = harness.with_voice_note(body["messages"], voice_note)
    # Keep prompt + reply inside the context window: long documents leave less room to answer.
    prompt_chars, images = 0, 0
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, list):
            prompt_chars += sum(len(p.get("text", "")) for p in c if p.get("type") == "text")
            images += sum(1 for p in c if p.get("type") == "image_url")
        else:
            prompt_chars += len(c or "")
    window = models.effective_context(m)
    room = window - int(prompt_chars / 3.2) - images * 1200 - 512
    body["max_tokens"] = 1 if warm else max(512, min(int(body.get("max_tokens") or 16384), room))
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    gguf = m.get("backend") == "gguf"
    for key, value in (m.get("sampling") or {}).items():       # the model card's sampling, unless the request chose its own
        body.setdefault("repeat_penalty" if gguf and key == "repetition_penalty" else key, value)
    mode = models.engine_mode(m, needs_vision)
    durable_attempt = 0
    if durable_enabled:
        try:
            fingerprint = await asyncio.to_thread(lambda: verification.fingerprint() or workflow_runs.workspace_fingerprint(workspace))
            if not fingerprint.get("digest"):
                raise workflow_runs.RunConflict(fingerprint.get("error") or "Workspace fingerprint unavailable")
            if not durable_record:
                durable_record, created = workflow_runs.store.create(
                    workspace, workflow_runs.policy_fingerprint(settings_snapshot, workspace), fingerprint, body["messages"],
                    request_key=request_key, metadata={"chat_id": chat_id, "model_id": m["id"], "task_id": (policy.task or {}).get("id"),
                                                       "workflow_mode": policy.mode, "task_identity": workflow_runs.task_identity(policy.task)})
                if not created:
                    raise workflow_runs.RunConflict(f"This request already has workflow run {durable_record['id']}; replay it instead of dispatching again")
                durable_id = durable_record["id"]
            policy.audit_callback = lambda kind, payload: workflow_runs.store.append(durable_id, kind, payload)
        except (ValueError, workflow_runs.RunConflict) as error:
            raise HTTPException(409, str(error)) from error

    async def relay():
        # The agent holds the machine: only its own engine may answer (requests run one at a time).
        if agent_lock.locked() and (mode, m["id"]) != (agent_engine["mode"], agent_engine["model_id"]):
            yield _sse({"error": AGENT_BUSY_MSG})
            return
        if needs_vision and not m.get("vision"):
            yield _sse({"error": f"{m['name']} can't read images. Switch to a model marked Vision "
                                 f"(for example Qwen3.8 27B) from the model menu."})
            return
        if not models.model_installed(m):
            yield _sse({"error": f"{m['name']} isn't downloaded yet. Get it from the Model library."})
            return
        st = await asyncio.to_thread(engines.engine_state)
        if (st.get("mode") != mode or st.get("model_id") != m["id"] or st.get("context_window", config.LEGACY_CONTEXT) != window
                or not await asyncio.to_thread(engines._engine_ready, mode)):
            yield _sse({"x_engine": {"status": "switching", "mode": mode, "model": m["name"]}})
            try:
                await asyncio.to_thread(engines.ensure_engine, mode, m)
            except Exception as e:
                yield _sse({"error": str(e)})
                return
            yield _sse({"x_engine": {"status": "ready", "mode": mode, "model": m["name"]}})
        if mentioned_sources:
            yield _sse({"x_sources": mentioned_sources})
        url = ENGINES[mode]["url"]
        if mode == "vision":
            body["model"] = m["repo"]
        if mode == "gguf":
            body["model"] = m["id"]
            body["chat_template_kwargs"] = {"enable_thinking": bool(body.get("enable_thinking", True))}
        t0 = time.time()
        state = {}
        read_names = {t["function"]["name"] for t in CHAT_READ_TOOLS}
        if flag("harness.skills"):
            read_names.update(t["function"]["name"] for t in harness.CHAT_RESOURCE_TOOLS)
        action_names = {t["function"]["name"] for t in CHAT_ACTION_TOOLS} | set(mcp_meta_map.keys())
        unread_web_results, file_candidates = False, {}
        call_counts = {}
        turn_id = uuid.uuid4().hex[:16]
        turn_backups: list[dict] = []
        session = _ChatSession(chat_id=chat_id)
        execution_cancel = threading.Event()
        policy.cancel = execution_cancel
        timer = threading.Timer(verification.remaining_seconds(), execution_cancel.set) if verification.enabled else None
        if timer:
            timer.daemon = True
            timer.start()
        instruction_fingerprints = set()
        pending_instruction_fingerprints = set()
        active_future = None
        observed_steps = set()
        _chat_sessions[session.id] = session
        try:
            for round_no in range(CHAT_TOOL_ROUNDS):
                if await request.is_disconnected(): return
                instruction_fingerprints.update(pending_instruction_fingerprints)
                pending_instruction_fingerprints.clear()
                if durable_id and await asyncio.to_thread(lambda: workflow_runs.store.get(durable_id, recover=False).get("cancel_requested")):
                    execution_cancel.set()
                    policy.cancel.set()
                    yield _sse({"error": "Workflow run cancelled."})
                    return
                if verification.exhausted():
                    _, _, explanation = await asyncio.to_thread(verification.completion)
                    yield _sse({"x_verification": (await asyncio.to_thread(verification.status))})
                    yield _sse({"choices": [{"delta": {"content": explanation}, "finish_reason": "stop"}]})
                    return
                try:
                    # The model still chooses the source. Temporarily remove another search until
                    # it has tried reading the URLs it already found, avoiding endless reworded queries.
                    body["tools"] = [t for t in all_chat_tools if not unread_web_results or t["function"]["name"] != "web_search"]
                    _chat_fit_context(body, window)
                    async for event in _chat_engine_turn(url, body, state):
                        if (verification.enabled or policy.enabled) and event.get("choices"):
                            # Content can claim completion before native calls are known.
                            # Keep it private until both final paths have passed the gate;
                            # A reply tool's arguments can also contain a premature
                            # answer. Native calls still assemble in state; reasoning,
                            # x_tool progress and usage can stream live.
                            event = {**event, "choices": [
                                {**choice, "delta": {k: v for k, v in (choice.get("delta") or {}).items() if k not in ("content", "tool_calls")},
                                 "finish_reason": None} for choice in event["choices"]]}
                        yield _sse(event)
                except (httpx.HTTPError, RuntimeError, ValueError) as error:
                    yield _sse({"error": str(error)})
                    return
                if not state["calls"] and not state["content"].strip() and state.get("finish") == "length":
                    note = ("The reply was cut off before it produced any text: the conversation nearly fills the model's context window "
                            "and the model used its remaining budget thinking. Use /compact or start a new chat.")
                    state["content"] = note
                    yield _sse({"choices": [{"delta": {"content": note}, "finish_reason": None}]})
                calls = list(state["calls"].values())
                for index, call in enumerate(calls):
                    call["id"] = call["id"] or f"chat-{round_no}-{index}"
                    if durable_id:
                        call["id"] = f"attempt-{durable_attempt}-round-{round_no}-call-{index}"
                selected = (extract_tool_call({"tool_calls": calls, "content": ""}, read_names | action_names | {"use_computer", "reply"})
                            if len(calls) == 1 else None)
                final_reply = bool(selected and selected[0] == "reply" and isinstance(selected[1], dict)
                                   and isinstance(selected[1].get("text"), str))
                if (verification.enabled or policy.enabled) and (not calls or final_reply or (selected and selected[0] == "use_computer")):
                    allowed, retry, explanation = await asyncio.to_thread(verification.completion)
                    if not allowed:
                        if retry and not (selected and selected[0] == "use_computer"):
                            if calls:
                                body["messages"].append({"role": "assistant", "content": state["content"], "tool_calls": calls})
                                body["messages"].append({"role": "tool", "tool_call_id": calls[0]["id"], "name": "reply",
                                                         "content": json.dumps({"error": explanation, "verified": False})})
                            else:
                                body["messages"].append({"role": "assistant", "content": state["content"]})
                            body["messages"].append({"role": "system", "content": verification.guidance(explanation)})
                            continue
                        yield _sse({"x_verification": (await asyncio.to_thread(verification.status))})
                        yield _sse({"choices": [{"delta": {"content": explanation}, "finish_reason": "stop"}]})
                        yield "data: [DONE]\n\n"
                        return
                    if not calls or final_reply:
                        yield _sse({"x_verification": (await asyncio.to_thread(verification.status))})
                        answer = selected[1]["text"] if final_reply else state["content"]
                        yield _sse({"choices": [{"delta": {"content": answer}, "finish_reason": "stop"}]})
                        break
                if not calls: break
                if len(calls) == 1:
                    if selected and selected[0] == "reply" and isinstance(selected[1], dict) and isinstance(selected[1].get("text"), str):
                        yield _sse({"choices": [{"delta": {"content": selected[1]["text"]}, "finish_reason": "stop"}]})
                        break
                    if selected and selected[0] == "use_computer":
                        if not await request.is_disconnected():
                            try:
                                policy.before_tool("use_computer", selected[1])
                                if durable_id:
                                    workflow_runs.store.checkpoint(durable_id, body["messages"] + [{"role": "assistant", "content": state["content"], "tool_calls": calls}])
                                    workflow_runs.store.dispatch(durable_id, f"{durable_attempt}-{round_no}-computer", "use_computer", selected[1], call_id=calls[0]["id"])
                                evidence = "\n".join(m.get("content", "") for m in body["messages"] if m.get("role") == "tool")
                                result = await asyncio.to_thread(_chat_computer_run, raw_messages, expanded_messages, m["id"], chat_id, evidence)
                                try:
                                    policy.after_tool("use_computer", selected[1], result)
                                except ValueError as error:
                                    result = {**result, "error": str(error), "post_hook_failed": True, "operation_executed": True}
                                if durable_id:
                                    workflow_runs.store.result(durable_id, f"{durable_attempt}-{round_no}-computer", result)
                                if result.get("post_hook_failed"):
                                    yield _sse({"error": result["error"]})
                                else:
                                    yield _sse({"x_agent": result})
                            except HTTPException as error:
                                if str(error.detail).startswith("Computer access is off") or "CuaDriver" in str(error.detail):
                                    yield _sse({"x_computer_setup": {"message": error.detail, "enable": not load_settings()["agent"]["enabled"]}})
                                else: yield _sse({"error": error.detail})
                        yield "data: [DONE]\n\n"
                        return
                # Feed each real tool result back to the SAME model before it answers.
                # The native tool channel is the only executable channel; prose/code stays prose.
                body["messages"] = body["messages"] + [{"role": "assistant", "content": state["content"],
                                                         "reasoning_content": harness._replay_reasoning(state["reasoning"]), "tool_calls": calls}]
                if durable_id:
                    await asyncio.to_thread(lambda: workflow_runs.store.checkpoint(durable_id, body["messages"], verification.snapshot()))
                for index, call in enumerate(calls):
                    if await request.is_disconnected(): return
                    name = call["function"]["name"]
                    if name == "run_subagent":
                        name = "delegate_task"
                    args = _normalize_harness_args(name, _parse_harness_args(call["function"]["arguments"]))
                    if name == "read_file" and isinstance(args, dict) and args.get("file_id") is None and args.get("path"):
                        raw_p = str(args["path"]).strip()
                        cand_name = Path(raw_p).name.lower()
                        matched_fid = next((fid for fid, item in file_candidates.items()
                                            if item.get("path") == raw_p or (item.get("name") or "").lower() == cand_name), None)
                        if matched_fid is not None:
                            try:
                                local_ok = await asyncio.to_thread(_in_workspace, workspace, lambda: _harness_path_fence(raw_p, write=False).is_file())
                            except ValueError:
                                local_ok = False
                            if not local_ok:
                                args = {k: v for k, v in args.items() if k != "path"}
                                args["file_id"] = matched_fid
                    call_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False) if isinstance(args, dict) else str(args))
                    call_counts[call_key] = call_counts.get(call_key, 0) + 1
                    known = name in (read_names | action_names) and isinstance(args, dict)
                    summary = _chat_tool_summary(name, args) if known else "Checking a tool request"
                    if name == "read_file" and isinstance(args, dict) and not args.get("path"):
                        cand_doc = file_candidates.get(args.get("file_id"))
                        if cand_doc and cand_doc.get("name"):
                            summary = f"Reading file: {cand_doc['name']}"
                    if flag("security.redact"):
                        summary = redact_obj(summary)
                    key = f"{round_no}-{index}"
                    yield _sse({"x_tool": {"id": key, "name": name, "status": "running", "summary": summary}})
                    meta = None
                    executed = False
                    step_id = f"{durable_attempt}-{key}"
                    instruction_result = None
                    instruction_context = None
                    rules_applied = None
                    if workspace and isinstance(args, dict) and flag("harness.nested_instructions"):
                        from studio.services.instructions import tool_context, model_context, summarize
                        context = await asyncio.to_thread(tool_context, workspace, name, args)
                        rules_applied = {"rules": summarize(context), "rules_complete": bool(context["complete"])}
                        affected_action = (name in action_names and name != "ask_user") or name == "delegate_task"
                        instruction_context = model_context(context)
                        if not context["complete"] and affected_action:
                            instruction_result = {"error": "Workspace instruction context is incomplete.", "warnings": context["warnings"], "verified": False}
                        elif context["fingerprint"] not in instruction_fingerprints and affected_action:
                            pending_instruction_fingerprints.add(context["fingerprint"])
                            instruction_result = {"workspace_instructions": model_context(context), "deferred": True,
                                                  "guidance": "Read these applicable repository instructions before retrying this tool. They do not grant tool permissions."}
                        elif name in read_names and context["complete"]:
                            pending_instruction_fingerprints.add(context["fingerprint"])
                    if instruction_result is not None:
                        result = instruction_result
                    elif name == "web_search" and unread_web_results:
                        result = {"error": "Search already returned source URLs. Call read_webpage on a relevant URL before searching again.", "verified": False}
                    elif name in read_names:
                        def _run_read():
                            try:
                                policy.before_tool(name, args)
                            except ValueError as error:
                                return {"error": str(error), "policy_blocked": True, "verified": False}
                            if durable_id:
                                workflow_runs.store.dispatch(durable_id, step_id, name, args, "read_only" if name not in ("memory_store", "manage_process", "todo_write", "delegate_task") else "possible_side_effect", call_id=call["id"])
                            try:
                                value = _in_workspace(workspace, harness._chat_read_tool, name, args, timezone)
                            except ValueError as error:
                                value = {"error": str(error), "operation_executed": True, "verified": False}
                            try:
                                policy.after_tool(name, args, value)
                            except ValueError as error:
                                value = {**value, "error": str(error), "post_hook_failed": True, "operation_executed": True}
                            if durable_id:
                                record = workflow_runs.store.get(durable_id, recover=False)
                                if step_id in record["steps"]:
                                    workflow_runs.store.result(durable_id, step_id, value)
                            return value
                        result = await asyncio.to_thread(_run_read)
                        if name == "todo_write" and isinstance(result.get("todos"), list) and not result.get("error"):
                            yield _sse({"x_plan": result["todos"]})
                    elif name == "ask_user" and isinstance(args, dict):
                        policy.before_tool(name, args)
                        question = str(args.get("question") or "").strip()[:500]
                        options = [str(o).strip()[:120] for o in (args.get("options") or []) if str(o).strip()][:6]
                        if not question:
                            result = {"error": "ask_user requires a non-empty question."}
                        else:
                            session.prepare(key)
                            yield _sse({"x_question": {"session_id": session.id, "id": key, "question": question, "options": options}})
                            reply_payload = await session.wait_for_reply(request)
                            if reply_payload is None: return
                            answer = str(reply_payload.get("answer") or "").strip()
                            result = (reply_payload if reply_payload.get("timed_out") else
                                      {"question": question, "answer": answer} if answer else {"error": "The user skipped the question."})
                        try:
                            policy.after_tool(name, args, result)
                        except ValueError as error:
                            result = {**result, "error": str(error), "post_hook_failed": True, "operation_executed": True}
                    elif name in action_names and isinstance(args, dict):
                        decision, reason, meta = await asyncio.to_thread(_in_workspace, workspace, harness_gate, name, args, chat_id, extra_perms)
                        if decision == "blocked":
                            result = {"error": f"Blocked: {reason}", "verified": False}
                        elif decision == "risky":
                            session.prepare(key)
                            yield _sse({"x_approval": {
                                "session_id": session.id, "id": key, "name": name,
                                "title": meta["title"], "summary": summary, "reason": reason,
                                "preview": meta.get("preview", ""), "kind": meta.get("kind", "code"),
                                "path": meta.get("path"), "perm_scope": meta.get("perm_scope")
                            }})
                            reply_payload = await session.wait_for_reply(request)
                            if reply_payload is None: return
                            user_choice = str(reply_payload.get("decision") or "deny").strip().lower()
                            if user_choice in ("allow", "allow_session"):
                                executed = True
                                if user_choice == "allow_session" and chat_id and meta.get("perm_scope"):
                                    _chat_session_perms.setdefault(chat_id, set()).add(meta["perm_scope"])
                                out_q: queue.Queue = queue.Queue()
                                sink = _tool_output_sink(out_q)
                                def _run_act():
                                    network_token = _python_network.set(python_network)
                                    cancel_token = _execution_cancel.set(execution_cancel)
                                    policy_token = _active_policy.set(policy)
                                    run_token = workflow_runs.current_run.set(durable_id)
                                    try:
                                        _chat_turn_id.set(turn_id)
                                        verification.before_execution(name, args)
                                        if durable_id:
                                            workflow_runs.store.dispatch(durable_id, step_id, name, args, "verification" if verification._requested_ids(name, args) else "possible_side_effect", call_id=call["id"])
                                        value = _in_workspace(workspace, _chat_action_tool, name, args, sink)
                                        if verification.enabled and verification.remaining_seconds() <= 0 and value.get("error"):
                                            value = {**value, "error": "Execution timed out: verification elapsed budget exhausted."}
                                        verification.observe(name, args, value, executed=True)
                                        observed_steps.add(key)
                                        if durable_id:
                                            workflow_runs.store.result(durable_id, step_id, value)
                                        return value
                                    finally:
                                        workflow_runs.current_run.reset(run_token)
                                        _active_policy.reset(policy_token)
                                        _execution_cancel.reset(cancel_token)
                                        _python_network.reset(network_token)
                                fut = asyncio.get_running_loop().run_in_executor(None, _run_act)
                                active_future = fut
                                while not fut.done():
                                    if await request.is_disconnected(): return
                                    if durable_id and workflow_runs.store.is_cancelled(durable_id):
                                        execution_cancel.set()
                                    while not out_q.empty():
                                        yield _sse({"x_tool_output": {"id": key, "chunk": out_q.get_nowait()}})
                                    await asyncio.sleep(0.04)
                                sink.flush()
                                while not out_q.empty():
                                    yield _sse({"x_tool_output": {"id": key, "chunk": out_q.get_nowait()}})
                                result = await fut
                            elif reply_payload.get("timed_out"):
                                result = {**reply_payload, "verified": False}
                            else:
                                feedback = str(reply_payload.get("feedback") or "").strip()[:2000]
                                result = {"error": "The user declined this action. Do not retry it or work around it with another tool or command; briefly say what you could not do, then stop."
                                                   + (f" Do this instead: {feedback}" if feedback else ""),
                                          "declined": True, **({"user_feedback": feedback} if feedback else {})}
                        else:
                            executed = True
                            out_q = queue.Queue()
                            sink = _tool_output_sink(out_q)
                            def _run_act_safe():
                                network_token = _python_network.set(python_network)
                                cancel_token = _execution_cancel.set(execution_cancel)
                                policy_token = _active_policy.set(policy)
                                run_token = workflow_runs.current_run.set(durable_id)
                                try:
                                    _chat_turn_id.set(turn_id)
                                    verification.before_execution(name, args)
                                    if durable_id:
                                        workflow_runs.store.dispatch(durable_id, step_id, name, args, "verification" if verification._requested_ids(name, args) else "possible_side_effect", call_id=call["id"])
                                    value = _in_workspace(workspace, _chat_action_tool, name, args, sink)
                                    if verification.enabled and verification.remaining_seconds() <= 0 and value.get("error"):
                                        value = {**value, "error": "Execution timed out: verification elapsed budget exhausted."}
                                    verification.observe(name, args, value, executed=True)
                                    observed_steps.add(key)
                                    if durable_id:
                                        workflow_runs.store.result(durable_id, step_id, value)
                                    return value
                                finally:
                                    workflow_runs.current_run.reset(run_token)
                                    _active_policy.reset(policy_token)
                                    _execution_cancel.reset(cancel_token)
                                    _python_network.reset(network_token)
                            fut = asyncio.get_running_loop().run_in_executor(None, _run_act_safe)
                            active_future = fut
                            while not fut.done():
                                if await request.is_disconnected(): return
                                if durable_id and workflow_runs.store.is_cancelled(durable_id):
                                    execution_cancel.set()
                                while not out_q.empty():
                                    yield _sse({"x_tool_output": {"id": key, "chunk": out_q.get_nowait()}})
                                await asyncio.sleep(0.04)
                            sink.flush()
                            while not out_q.empty():
                                yield _sse({"x_tool_output": {"id": key, "chunk": out_q.get_nowait()}})
                            result = await fut
                    else:
                        result = {"error": "Use a known read-only or workspace tool, or call reply/use_computer by itself after reading the results."}
                    if instruction_context and instruction_result is None:
                        result = {**result, "applicable_instructions": instruction_context}
                    # observe and the post-step fingerprint hash the workspace: keep that off the event loop that streams the reply
                    if key not in observed_steps:
                        await asyncio.to_thread(lambda: verification.observe(name, args, result, executed=executed))
                    if durable_id:
                        await asyncio.to_thread(lambda: workflow_runs.store.checkpoint(durable_id, body["messages"], verification.snapshot(), verification.fingerprint() or fingerprint))
                    if name == "web_search" and result.get("results"): unread_web_results = True
                    if name == "read_webpage": unread_web_results = False
                    if await request.is_disconnected(): return
                    if flag("security.redact"):
                        result = redact_obj(result)
                    detail = _chat_tool_detail(name, args, result, meta)
                    if flag("security.redact"):
                        detail = redact_obj(detail)
                    yield _sse({"x_tool": {"id": key, "name": name, "status": "error" if result.get("error") else "done", "summary": summary,
                                          **({"detail": detail} if detail else {}),
                                          **(rules_applied if rules_applied and any(c["files"] for c in rules_applied["rules"]) else {}),
                                          **({"backup_id": result["backup_id"]} if result.get("backup_id") else {}),
                                          **({"syntax_check": result["syntax_check"], "syntax_ok": result.get("syntax_ok", True)} if result.get("syntax_check") else {}),
                                          **({"error": result["error"]} if result.get("error") else {})}})
                    if verification.enabled and verification.declined and (await asyncio.to_thread(verification.status))["status"] == "unverified":
                        # Do not give the model another chance to repeat an action
                        # whose approval was refused, blocked or left unanswered.
                        _, _, explanation = await asyncio.to_thread(verification.completion)
                        yield _sse({"x_verification": (await asyncio.to_thread(verification.status))})
                        yield _sse({"choices": [{"delta": {"content": explanation}, "finish_reason": "stop"}]})
                        yield "data: [DONE]\n\n"
                        return
                    if result.get("timed_out"):
                        # An unanswered prompt ends the turn. Do not ask the model
                        # to retry the same unapproved action in another round.
                        yield _sse({"error": result["error"]})
                        yield "data: [DONE]\n\n"
                        return
                    if result.get("backup_id"):
                        turn_backups.append({"backup_id": result["backup_id"], "name": result.get("name") or "file", "path": result.get("path")})
                        yield _sse({"x_checkpoint": {
                            "turn_id": turn_id,
                            "backup_ids": [b["backup_id"] for b in turn_backups],
                            "files": list(dict.fromkeys(b["name"] for b in turn_backups)),
                            "count": len(turn_backups)
                        }})
                    web_sources = result.get("results", []) if name == "web_search" else [result] if name == "read_webpage" and result.get("url") else []
                    if web_sources:
                        yield _sse({"x_web_sources": [{"url": item["url"], "title": item.get("title") or item["url"]} for item in web_sources if item.get("url")]})
                    if name == "search_files" and result.get("results"):
                        file_candidates.update({item["file_id"]: item for item in result["results"] if item.get("file_id")})
                    if name == "read_file" and not result.get("error"):
                        fid = result.get("file_id") or next((cid for cid, item in file_candidates.items() if item.get("path") == result.get("path")), None)
                        if fid:
                            source = {**file_candidates.get(fid, {}), **result, "file_id": fid}
                            if source.get("name") and source.get("path"):
                                yield _sse({"x_sources": [{k: source.get(k) for k in ("file_id", "name", "path", "locator")}]})
                    model_result = result
                    if name == "search_files" and result.get("results") and result.get("scope") != "workspace":
                        # Search offers candidates, not full document evidence. Avoid repeatedly
                        # prefilling long paths and passages before the model reads the actual files.
                        model_result = {**result, "results": [{**{k: v for k, v in item.items() if k not in ("path", "snippet")},
                            "folder": Path(item.get("path") or "").parent.name,
                            "snippet": (item.get("snippet") or "")[:300], "snippet_is_partial": True} for item in result["results"]]}
                    if call_counts[call_key] >= 2 and name != "get_current_datetime" and isinstance(model_result, dict):
                        model_result = {**model_result, "loop_warning": "You have called this exact tool with identical arguments multiple times. Synthesize the results so far, try a different step, or answer the user."}
                    body["messages"].append({"role": "tool", "tool_call_id": call["id"], "name": name,
                                             "content": _untrusted_mcp_result(model_result) if name.startswith("mcp__") else json.dumps(model_result, ensure_ascii=False)})
            else:
                if verification.enabled and (await asyncio.to_thread(verification.status))["status"] == "unverified":
                    _, _, explanation = await asyncio.to_thread(verification.completion)
                    yield _sse({"x_verification": (await asyncio.to_thread(verification.status))})
                    yield _sse({"choices": [{"delta": {"content": explanation + " The tool-call limit was reached."}, "finish_reason": "stop"}]})
                yield _sse({"error": "Stopped after the tool-call limit. I could not finish verifying this request; no unsupported answer was substituted."})
                yield "data: [DONE]\n\n"
                return
        finally:
            execution_cancel.set()
            if timer:
                timer.cancel()
            if active_future and not active_future.done():
                # Give process-group cancellation time to record its actual result.
                # A hard crash still leaves the persisted dispatch explicitly unknown.
                try:
                    await asyncio.wait_for(asyncio.shield(active_future), 2)
                except (TimeoutError, ValueError, OSError):
                    pass
            _chat_sessions.pop(session.id, None)
        first, n_out, last_timings, usage = state["first"], state["n_out"], state["timings"], state["usage"]
        if mode == "text":
            yield "data: [DONE]\n\n"
            return
        end = time.time()
        t = last_timings or {}
        tps = t.get("predicted_per_second")
        out = (usage or {}).get("completion_tokens") or t.get("predicted_n") or n_out
        stats = {"mode": mode, "engine": "llama.cpp" if gguf else "mlx-vlm", "model": m["name"],
                 "decode_tokens_per_sec": round(tps or (out / max(end - (first or end), 1e-6)), 1),
                 "tokens_per_sec": round(out / max(end - t0, 1e-6), 1), "completion_tokens": out,
                 "prompt_tokens": (usage or {}).get("prompt_tokens") or t.get("prompt_n"),
                 "ttft_seconds": round((first or end) - t0, 3), "decode_seconds": round(end - (first or end), 3)}
        if t.get("prompt_per_second"):
            stats["prefill_tokens_per_sec"] = round(t["prompt_per_second"], 1)
        if t.get("cache_n") is not None:
            stats["cached_tokens"] = t["cache_n"]
        if t.get("draft_n"):
            stats["accept_len"] = round(t.get("draft_n_accepted", 0) / max(t["draft_n"], 1), 2)
        yield _sse({"x_mlx_dspark": stats})
        yield "data: [DONE]\n\n"

    async def managed_relay():
        """Persist replay evidence before publication and finalize policy before prose."""
        nonlocal durable_attempt, durable_record
        from studio.services.workflow import claim_session, release_session
        policy_token = _active_policy.set(policy)
        run_token = workflow_runs.current_run.set(durable_id)
        outcome, finalized, claimed, durable_finished, run_claimed = "interrupted", False, False, False, False
        source = relay()
        try:
            claim_session(policy)
            claimed = True
            if durable_id:
                durable_record = workflow_runs.store.claim(durable_id, resume_token)
                run_claimed = True
                durable_attempt = durable_record["attempt"]
            timeout = verification.remaining_seconds() if verification.enabled else (policy.max_seconds if policy.enabled else None)
            async with asyncio.timeout(timeout):
                async for chunk in source:
                    event = None
                    status_override = None
                    if chunk.startswith("data: {"):
                        event = json.loads(chunk[6:])
                    if event and event.get("error"):
                        outcome = "needs_user" if "cancel" not in str(event["error"]).lower() else "cancelled"
                    choice = ((event or {}).get("choices") or [{}])[0]
                    if choice.get("finish_reason") in ("stop", "length") and not finalized:
                        status = await asyncio.to_thread(verification.status)
                        if status["status"] == "unverified":
                            outcome = "needs_user" if verification.declined else "incomplete"
                            _, _, explanation = await asyncio.to_thread(verification.completion)
                            event = {"choices": [{"delta": {"content": explanation}, "finish_reason": "stop"}]}
                            chunk = _sse(event)
                            status_override = status
                        else:
                            try:
                                outcome = policy.finalize(await asyncio.to_thread(verification.snapshot))
                            except ValueError as error:
                                outcome = "needs_user"
                                event = {"choices": [{"delta": {"content": "Incomplete: " + str(error)}, "finish_reason": "stop"}]}
                                chunk = _sse(event)
                        finalized = True
                    if chunk == "data: [DONE]\n\n" and not finalized and outcome == "interrupted":
                        try:
                            outcome = policy.finalize(await asyncio.to_thread(verification.snapshot))
                        except ValueError:
                            outcome = "needs_user"
                        finalized = True
                    if durable_id:
                        if event and (event.get("x_approval") or event.get("x_question")):
                            workflow_runs.store.approval(durable_id, event.get("x_approval") or event["x_question"])
                        if finalized and not durable_finished:
                            snapshot = await asyncio.to_thread(verification.snapshot)
                            if outcome == "verified_complete" and snapshot["status"] != "verified":
                                outcome = "incomplete"
                                event = {"choices": [{"delta": {"content": "Incomplete: verification became stale before completion was recorded."}, "finish_reason": "stop"}]}
                                chunk = _sse(event)
                                status_override = await asyncio.to_thread(verification.status)
                            workflow_runs.store.checkpoint(durable_id, body["messages"], snapshot, snapshot.get("fingerprint") or fingerprint)
                            workflow_runs.store.finish(durable_id, outcome, snapshot)
                            durable_finished = True
                        if status_override:
                            workflow_runs.store.append(durable_id, "sse", {"data": {"x_verification": status_override}})
                        workflow_runs.store.append(durable_id, "sse", {"data": event} if event else {"frame": chunk})
                    if status_override:
                        status_event = {"x_verification": status_override}
                        yield _sse(status_event)
                    yield chunk
        except TimeoutError:
            policy.cancel.set()
            outcome = "incomplete"
            event = {"error": "Workflow elapsed execution budget exhausted; foreground execution was cancelled."}
            if durable_id:
                workflow_runs.store.append(durable_id, "sse", {"data": event})
            yield _sse(event)
            yield "data: [DONE]\n\n"
        except ValueError as error:
            # A cancel that lands while the model's reply is in flight refuses the next dispatch: nothing ran, so the run is
            # cancelled, the same as a cancel seen at the start of a round.
            outcome = "cancelled" if isinstance(error, workflow_runs.RunCancelled) else "needs_user"
            yield _sse({"error": str(error)})
            yield "data: [DONE]\n\n"
        finally:
            try:
                try:
                    await source.aclose()
                finally:
                    if durable_id and run_claimed and not durable_finished:
                        status = None
                        try:
                            status = await asyncio.to_thread(verification.snapshot)
                            await asyncio.to_thread(lambda: workflow_runs.store.checkpoint(durable_id, body["messages"], status, verification.fingerprint() or fingerprint))
                        finally:
                            workflow_runs.store.finish(durable_id, outcome, status)
            finally:
                if claimed:
                    release_session(policy, outcome)
                workflow_runs.current_run.reset(run_token)
                _active_policy.reset(policy_token)

    response_headers = {"X-Workflow-Run-ID": durable_id} if durable_id else {}
    if resumable:
        if await http_request.is_disconnected():
            raise HTTPException(499, "Chat request disconnected before streaming began")
        stream = chat_streams.new_stream(_counted(managed_relay()))
        return StreamingResponse(stream.subscribe(), media_type="text/event-stream",
                                 headers={**response_headers, "X-Chat-Stream-ID": stream.id, "Cache-Control": "no-store"})
    return StreamingResponse(_counted(managed_relay()), media_type="text/event-stream", headers=response_headers)


@router.get("/api/chat/streams/{stream_id}")
async def resume_chat_stream(stream_id: str, after: int = 0):
    stream = chat_streams.get_stream(stream_id)
    stream.validate(after)
    return StreamingResponse(stream.subscribe(after), media_type="text/event-stream",
                             headers={"X-Chat-Stream-ID": stream.id, "Cache-Control": "no-store"})


@router.delete("/api/chat/streams/{stream_id}")
async def stop_chat_stream(stream_id: str):
    stream = chat_streams.get_stream(stream_id)
    stream.stop()
    return {"ok": True}
