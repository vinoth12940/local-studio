#!/usr/bin/env python3
"""Run the agent-harness evaluation tasks.

    run_evals.py --list | --validate
    run_evals.py --scripted                       # no model: proves tasks are solvable and checks discriminate
    run_evals.py --attach --model <id> --repeat 3 # real model already loaded in Local Studio

Real runs never start, stop or switch an engine (LOCAL_STUDIO_EVAL_ATTACH=1). They use a throwaway
LOCAL_STUDIO_HOME, so the app's chats, settings and backups are not touched. Load the model in Local Studio
first and avoid using it while the evaluation runs: there is only one engine and it is shared.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--validate", action="store_true", help="validate tasks, fixtures and scripts and exit")
    parser.add_argument("--task", action="append", help="run only this task id (repeatable)")
    parser.add_argument("--model", help="chat model id of the model already loaded in Local Studio")
    parser.add_argument("--attach", action="store_true", help="use the engine that is already running (required for real runs)")
    parser.add_argument("--scripted", action="store_true", help="replace the model with each task's reference script")
    parser.add_argument("--seed", type=int, help="forwarded to the engine in the chat body; honored only if the engine supports it")
    parser.add_argument("--temperature", type=float, help="sampling temperature forwarded in the chat body (default: the engine's)")
    parser.add_argument("--allow-model-mismatch", action="store_true", help="evaluate even if the engine reports a different model than --model")
    parser.add_argument("--repeat", type=int, default=1, help="runs per task, 1..10")
    parser.add_argument("--out", help="report directory (default build/evals/<timestamp>)")
    parser.add_argument("--baseline", help="a measured report.json to compare against")
    parser.add_argument("--max-drop", type=float, default=0.15, help="largest tolerated pass-rate drop per task and overall")
    args = parser.parse_args(argv)

    import evalkit
    tasks = evalkit.load_tasks(args.task)
    if args.list or args.validate:
        for task in tasks:
            if args.validate:
                evalkit.load_script(task["id"], "reference")
                evalkit.load_script(task["id"], "negative")
            print(f"{task['id']:<34} r{task['revision']} {task['category']:<17} {task['title']}")
        print(f"{len(tasks)} tasks valid · task set {evalkit.task_set_revision(tasks)[:12]}")
        return 0
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    if args.scripted == args.attach:
        parser.error("choose exactly one of --scripted (no model) or --attach (the model already loaded)")
    if args.attach and not args.model:
        parser.error("--attach needs --model <chat model id>")

    os.environ["LOCAL_STUDIO_HOME"] = evalkit.temp_home()
    if args.attach:
        os.environ["LOCAL_STUDIO_EVAL_ATTACH"] = "1"
    from studio.config import ENGINES
    from studio.services import engines
    from studio.services.models import chat_model, engine_mode

    model_id, mode, served, model_check = args.model, "text", None, None
    if args.attach:
        entry = chat_model(model_id)
        if entry["id"] != model_id:
            print(f"Unknown chat model id {model_id!r}; chat_model() would silently substitute {entry['id']!r}.", file=sys.stderr)
            return 2
        mode = engine_mode(entry)
        if not engines._engine_ready(mode):
            print(f"BLOCKED: no {mode} engine answers at {ENGINES[mode]['url']}. Load {entry['name']} in Local Studio, then rerun.", file=sys.stderr)
            return 3
        served = evalkit.served_model(ENGINES[mode]["url"])
        model_check = evalkit.model_match(entry, served)
        if model_check == "mismatch" and not args.allow_model_mismatch:
            print(f"REFUSING: the engine reports {served!r}, which does not match {model_id!r}. Results would be scored under the wrong model. "
                  "Load the right model, or pass --allow-model-mismatch to record the mismatch explicitly.", file=sys.stderr)
            return 4
        if model_check != "verified":
            print(f"WARNING: model check is {model_check}; the report records it.", file=sys.stderr)
        print(f"Attached to the running {mode} engine (it reports {served or 'an unknown model'}). It will not be restarted.")
    else:
        from studio.storage.settings import load_settings
        model_id = load_settings()["chat_model"]

    sampling = {k: v for k, v in (("seed", args.seed), ("temperature", args.temperature)) if v is not None}
    session = evalkit.Session(model_id, sampling)
    records, started = [], time.strftime("%Y-%m-%dT%H:%M:%S%z")
    work = Path(evalkit.temp_home())
    for round_no in range(args.repeat):
        for task in tasks:
            turns = evalkit.load_script(task["id"], "reference") if args.scripted else None
            record = evalkit.run_task(task, session, work, turns)
            records.append(record)
            print(f"[{round_no + 1}/{args.repeat}] {task['id']:<34} {record['status']:<8} {record['latency_seconds']}s {record['reason'] or ''}")
    flags = session_flags(tasks)
    report = evalkit.build_report(tasks, records, evalkit.environment(model_id, mode, flags, args.scripted, served, sampling, model_check), args.repeat,
                                  started, time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    out = Path(args.out or HERE.parent.parent / "build" / "evals" / time.strftime("%Y%m%d-%H%M%S"))
    path = evalkit.write_report(out, report, records)
    print(evalkit.render_markdown(report))
    print(f"Report: {path}")
    if args.baseline:
        problems = evalkit.compare(report, json.loads(Path(args.baseline).read_text()), args.max_drop)
        for problem in problems:
            print("REGRESSION:", problem, file=sys.stderr)
        return 1 if problems else 0
    return 0 if report["summary"]["pass_rate"] in (None, 1.0) or args.attach else 1


def session_flags(tasks):
    merged = {}
    for task in tasks:
        merged.update(task.get("flags", {}))
    return merged


if __name__ == "__main__":
    sys.exit(main())
