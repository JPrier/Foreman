"""Global CLI: no mandatory repository, directory or task type."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import initialize_config, load_config
from .engine import Engine
from .storage import load, new_task, recent_tasks, save

COMMANDS = {"run", "continue", "status", "history", "show", "init", "install-director-agent"}


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart-coder", description="General-purpose premium-director/cheap-worker harness")
    p.add_argument("--config", type=Path, help="Optional config file (defaults to ~/.smart-harness/config.json)")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true", help="Offline demonstration; no real work")
    modes.add_argument("--live", action="store_true", help="Use configured live providers (credentials required)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create user configuration; never overwrites existing configuration")
    sub.add_parser("install-director-agent", help="Opt-in: install no-tools OpenCode agent for subscription director")
    run = sub.add_parser("run", help="Start a task from any directory")
    run.add_argument("prompt", nargs="+", help="Natural-language task")
    cont = sub.add_parser("continue", help="Continue the last or named task with optional follow-up")
    cont.add_argument("prompt", nargs="*", help="Optional follow-up or answer to a clarifying question")
    cont.add_argument("--task", help="Task ID (default: latest)")
    status = sub.add_parser("status", help="Show latest task status from any directory")
    status.add_argument("--task")
    sub.add_parser("history", help="List recent tasks")
    show = sub.add_parser("show", help="Print full session state as JSON")
    show.add_argument("--task")
    return p


def _shorthand(args: list[str]) -> list[str]:
    if args and not args[0].startswith("-") and args[0] not in COMMANDS:
        return ["run", *args]
    return args


def _print_status(state: dict) -> None:
    print(f"Task: {state['id']} | status: {state['status']} | rounds: {state['round']} | workers: {state['worker_count']}")
    print(f"Director calls: {state['director_calls']} | premium reported: ${state['premium_spent_usd']:.4f}"
          f" | conservatively reserved: ${state['premium_reserved_usd']:.4f}")
    detail = "complete" if state["worker_cost_complete"] else "PARTIAL (not a reliable total)"
    print(f"Worker cost from CLI events: ${state['worker_cost_reported_usd']:.4f} ({detail})")
    if state["mode"] == "mock":
        print("MOCK MODE: zero real model calls or task execution.")
    if state.get("answer"):
        print("\n" + state["answer"])


def main(argv: list[str] | None = None) -> int:
    argv = _shorthand(list(sys.argv[1:] if argv is None else argv))
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        path = initialize_config()
        print(f"Configuration: {path}\nMode defaults to mock. Edit config.json and set mode=live after authentication.")
        return 0
    if args.command == "install-director-agent":
        from .agents import install_director_agent
        try:
            path = install_director_agent()
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"OpenCode no-tools director installed: {path}")
        return 0
    try:
        if args.command == "history":
            for s in recent_tasks():
                print(f"{s['id']}  {s['status']:18}  {s['objective'][:75]}")
            return 0
        if args.command in ("status", "show"):
            state = load(args.task)
            if args.command == "show":
                print(json.dumps(state, indent=2, ensure_ascii=False))
            else:
                _print_status(state)
            return 0
        cfg = load_config(args.config)
        if args.mock:
            cfg["mode"] = "mock"
        elif args.live:
            cfg["mode"] = "live"
        if args.command == "run":
            objective = " ".join(args.prompt).strip()
            if not objective:
                parser.error("Task cannot be blank")
            state = new_task(objective, cfg["mode"])
        else:
            state = load(args.task)
            if cfg["mode"] != state["mode"]:
                raise ValueError("Task mode differs from configuration; use --mock or --live to match its original mode")
            if state["status"] == "done":
                raise ValueError("Task is complete; start a new task")
            if args.prompt:
                state["followups"].append(" ".join(args.prompt))
                state["events"].append({"kind": "note", "message": "User provided follow-up context"})
                state["status"] = "running"
                state["answer"] = None
                save(state)
            elif state["status"] == "needs_input":
                raise ValueError("Task is awaiting user input; provide text after 'continue'")
        outcome = Engine(cfg).run(state)
        _print_status(outcome)
        return 0 if outcome["status"] in {"done", "needs_input"} else 1
    except (ValueError, FileNotFoundError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
