"""Global CLI: no mandatory repository, directory or task type."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import initialize_config, load_config
from .engine import Engine
from .storage import load, new_task, recent_tasks, save, execution_lock

COMMANDS = {"run", "continue", "status", "history", "show", "init", "install-director-agent", "fingerprint", "metrics", "bench", "observe", "costs"}


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart-coder", description="General-purpose premium-director/cheap-worker harness")
    p.add_argument("--config", type=Path, help="Optional config file (defaults to ~/.smart-harness/config.jsonc; legacy config.json supported)")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true", help="Offline demonstration; no real work")
    modes.add_argument("--live", action="store_true", help="Use configured live providers (credentials required)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create user configuration; never overwrites existing configuration")
    sub.add_parser("install-director-agent", help="Opt-in: install no-tools OpenCode agent for subscription director")
    fingerprint = sub.add_parser("fingerprint", help="Capture commit and working-tree evidence for a Git checkout")
    fingerprint.add_argument("path", type=Path)
    run = sub.add_parser("run", help="Start a task from any directory")
    run.add_argument("prompt", nargs="+", help="Natural-language task")
    run.add_argument("--metrics-json", action="store_true")
    cont = sub.add_parser("continue", help="Continue the last or named task with optional follow-up")
    cont.add_argument("prompt", nargs="*", help="Optional follow-up or answer to a clarifying question")
    cont.add_argument("--task", help="Task ID (default: latest)")
    status = sub.add_parser("status", help="Show latest task status from any directory")
    status.add_argument("--task")
    sub.add_parser("history", help="List recent tasks")
    show = sub.add_parser("show", help="Print full session state as JSON")
    show.add_argument("--task")
    metrics = sub.add_parser('metrics', help='Deterministic usage and cost provenance')
    metrics.add_argument('--task')
    metrics.add_argument('--run')
    metrics.add_argument('--format', choices=('json', 'markdown'), default='json')
    observe = sub.add_parser('observe').add_subparsers(dest='operation', required=True)
    for name in ('tree', 'task', 'timeline', 'serve'):
        command = observe.add_parser(name)
        command.add_argument('--run')
        command.add_argument('--task')
        command.add_argument('--format', choices=('json', 'markdown'), default='json')
        if name == 'serve':
            command.add_argument('--host', default='127.0.0.1')
            command.add_argument('--port', type=int, default=8765)
    costs = sub.add_parser('costs').add_subparsers(dest='operation', required=True)
    for name in ('reconcile', 'audit'):
        command = costs.add_parser(name)
        command.add_argument('--run', required=True)
        command.add_argument('--provider', choices=('openrouter',), default='openrouter')
        command.add_argument('--scope', default='default', help='Non-secret account/key scope label')
        command.add_argument('--opencode-url', help='Loopback session API for recursive recovery')
        command.add_argument('--key-check', action='store_true', help='Coarse key totals, never task attribution')
        command.add_argument('--format', choices=('json', 'markdown'), default='json')
    bench = sub.add_parser('bench').add_subparsers(dest='operation', required=True)
    command = bench.add_parser('run')
    command.add_argument('--suite', type=Path, required=True)
    command.add_argument('--variant', action='append')
    command.add_argument('--repeats', type=int, default=3)
    command.add_argument('--seed', type=int, default=0)
    command.add_argument('--reconcile', action='store_true', help='Reconcile known generations in a background worker after trials')
    command.add_argument('--checks', type=Path, required=True, help='Trusted registry JSON mapping names to argv arrays')
    command.add_argument('--output', choices=('json', 'markdown'), default='json')
    command = bench.add_parser('compare')
    command.add_argument('--suite', required=True, help='Suite ID or suite JSON path')
    command.add_argument('--baseline', required=True)
    command.add_argument('--candidate', required=True)
    command.add_argument('--min-trials', type=int, default=3)
    command.add_argument('--min-coverage', type=float, default=1.0)
    command.add_argument('--format', choices=('json', 'markdown'), default='json')
    command = bench.add_parser('import')
    command.add_argument('--task', required=True)
    command.add_argument('--from-existing-state', action='store_true', required=True)
    return p


def _shorthand(args: list[str]) -> list[str]:
    if args and not args[0].startswith("-") and args[0] not in COMMANDS:
        return ["run", *args]
    return args


def _print_status(state: dict) -> None:
    if 'budget_cost_complete' in state:
        print(f"Budget costs: reported ${state.get('budget_reported_usd', 0):.4f} | estimated ${state.get('budget_estimated_usd', 0):.4f}" + ("" if state['budget_cost_complete'] else " | incomplete"))
    print(f"Task: {state['id']} | status: {state['status']} | rounds: {state['round']} | workers: {state['worker_count']}")
    director_events = [e for e in state.get("events", []) if e.get("kind") == "director"]
    unknown_cost = sum(e.get("cost_usd") is None for e in director_events)
    print(f"Director calls: {state['director_calls']} | reported API cost lower bound: ${state['premium_spent_usd']:.4f}"
          f" | reserve: ${state['premium_reserved_usd']:.4f} | calls without reported cost: {unknown_cost}")
    estimates = [e["api_equivalent_estimate_usd"] for e in director_events if e.get("api_equivalent_estimate_usd") is not None]
    if estimates:
        print(f"Subscription API-equivalent estimate: ${sum(estimates):.4f} for {len(estimates)} call(s); not a charge or quota balance")
    detail = "complete" if state["worker_cost_complete"] else "PARTIAL (not a reliable total)"
    print(f"Worker cost from CLI events: ${state['worker_cost_reported_usd']:.4f} ({detail})")
    ledger = state.get("ledger", {})
    print(f"Ledger: {sum(e.get('status') in {'satisfied', 'resolved'} for e in ledger.values())}/{len(ledger)} entries marked resolved")
    for assignment_id, assignment in state.get("assignments", {}).items():
        if assignment.get("status") in {"running", "queued", "interrupted", "failed", "blocked"}:
            elapsed = ""
            if assignment.get("status") == "running" and assignment.get("started_at"):
                seconds = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(assignment["started_at"])).total_seconds())
                elapsed = f" | {seconds:.0f}s elapsed" + (" | LONG-RUNNING" if seconds >= assignment.get("long_assignment_seconds", 900) else "")
            print(f"  {assignment_id}: {assignment['status']}{elapsed} — {assignment.get('note', assignment.get('prompt', ''))}")
    notes = [e.get("message") for e in state.get("events", []) if e.get("kind") == "note" and e.get("message")]
    if notes:
        print(f"Recent activity: {notes[-1]}")
    if state["mode"] == "mock":
        print("MOCK MODE: zero real model calls or task execution.")
    if state.get("answer"):
        print("\n" + state["answer"])


def main(argv: list[str] | None = None) -> int:
    argv = _shorthand(list(sys.argv[1:] if argv is None else argv))
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "fingerprint":
        from .ledger import git_snapshot
        path = str(args.path.expanduser().resolve())
        snapshot = git_snapshot(path)
        if snapshot is None:
            print("ERROR: Could not fingerprint the Git checkout", file=sys.stderr)
            return 2
        print(json.dumps({"path": path, **snapshot}))
        return 0
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
        if args.command in ('metrics', 'observe', 'costs', 'bench'):
            return _measurement_command(args)
        if args.command == "history":
            for s in recent_tasks():
                print(f"{s['id']}  {s['status']:18}  {s['objective'][:75]}")
            return 0
        if args.command in ("status", "show"):
            state = load(args.task)
            from .recovery import reconcile
            try:
                with execution_lock(state["id"]):
                    state = load(state["id"])
                    reconcile(state)
            except RuntimeError:
                state = load(state["id"])
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
            objective = " ".join(args.prompt)
            if not objective.strip():
                parser.error("Task cannot be blank")
            state = new_task(objective, cfg["mode"])
            outcome = Engine(cfg, log=(lambda *_: None) if args.metrics_json else print).run(state)
        else:
            state = load(args.task)
            with execution_lock(state["id"]):
                state = load(state["id"])
                if cfg["mode"] != state["mode"]:
                    raise ValueError("Task mode differs from configuration; use --mock or --live to match its original mode")
                if state["status"] == "done" and not args.prompt:
                    raise ValueError("Task is complete; provide a follow-up to continue it")
                if args.prompt:
                    state["followups"].append(" ".join(args.prompt))
                    state["events"].append({"kind": "followup", "message": " ".join(args.prompt)})
                    if state["status"] not in {"workers_running", "recovery_blocked"}:
                        state["status"] = "running"
                    state["answer"] = None
                    save(state)
                elif state["status"] == "needs_input":
                    raise ValueError("Task is awaiting user input; provide text after 'continue'")
                outcome = Engine(cfg).run(state, already_locked=True)
        if getattr(args, 'metrics_json', False):
            from .telemetry import Telemetry
            import sqlite3
            try:
                metrics = Telemetry().metrics(outcome['telemetry_run_id'])
            except (OSError, sqlite3.Error, ValueError):
                metrics = {'schema_version': 1, 'run_id': outcome['telemetry_run_id'],
                    'status': outcome['status'], 'measurement_incomplete': True,
                    'cost': {'selected_total_usd': None, 'provider_coverage': 'incomplete'},
                    'usage': {'coverage': 'incomplete'}}
            print(json.dumps(metrics, indent=2))
        else:
            _print_status(outcome)
        return 0 if outcome["status"] in {"done", "needs_input"} else 1
    except (ValueError, FileNotFoundError, RuntimeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _measurement_command(args):
    from .telemetry import Telemetry
    from .observe import report, serve, session_fetcher
    from .storage import task_dir
    store = Telemetry()
    if args.command == 'bench':
        from .benchmarks import run_suite, compare
        if args.operation == 'import':
            state = load(args.task)
            value = {'run_id': store.import_state(state, task_dir(state['id']))}
        elif args.operation == 'run':
            cfg = load_config(args.config)
            if args.mock:
                cfg['mode'] = 'mock'
            elif args.live:
                cfg['mode'] = 'live'
            registry = json.loads(args.checks.read_text(encoding='utf-8'))
            if args.reconcile:
                import os
                if not os.environ.get('OPENROUTER_API_KEY'):
                    raise ValueError('OPENROUTER_API_KEY is required for --reconcile')
            value = run_suite(args.suite, args.variant, args.repeats, cfg, registry, seed=args.seed)
            if args.reconcile:
                from .accounting import ReconciliationQueue
                queue = ReconciliationQueue(store)
                try:
                    for run in value:
                        queue.submit(run['run_id'])
                finally:
                    queue.close()
                value = [store.metrics(run['run_id']) for run in value]
        else:
            if args.min_trials < 1 or not 0 <= args.min_coverage <= 1:
                raise ValueError('Invalid comparison thresholds')
            suite = args.suite
            if Path(suite).is_file():
                suite = json.loads(Path(suite).read_text(encoding='utf-8'))['suite_id']
            value = compare(store, suite, args.baseline, args.candidate,
                min_trials=args.min_trials, min_coverage=args.min_coverage)
    elif args.command == 'costs':
        from .accounting import OpenRouterClient
        if args.opencode_url:
            from .recovery import import_session_tree
            fetch = session_fetcher(args.opencode_url)
            linked = {e['payload']['session_id']: e['task_id'] for e in store.timeline(args.run) if e['kind'] == 'session.linked'}
            for session, task in linked.items():
                import_session_tree(store, args.run, task, session, fetch)
        if args.operation == 'reconcile':
            OpenRouterClient().reconcile(store, args.run, args.scope)
        from .observe import api
        value = api(store, '/api/v1/runs/' + args.run + '/costs')
        if args.key_check:
            value['key_usage_crosscheck_only'] = OpenRouterClient().key_usage()
    elif args.command == 'observe' and args.operation == 'serve':
        server = serve(store, args.host, args.port)
        print(f'Read-only telemetry: http://{args.host}:{server.server_port}/api/v1/runs')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    else:
        run_id = args.run
        if not run_id:
            state = load(args.task)
            run_id = state.get('telemetry_run_id')
            if not run_id:
                run_id = store.import_state(state, task_dir(state['id']))
        if args.command == 'metrics':
            value = store.metrics(run_id)
        elif args.operation == 'tree':
            value = store.tree(run_id)
        elif args.operation == 'timeline':
            value = store.timeline(run_id, args.task)
        else:
            if not args.task:
                raise ValueError('observe task requires --task')
            value = store.metrics(run_id, args.task)
    print(report(value, getattr(args, 'format', getattr(args, 'output', 'json'))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
