"""Plan/delegate/evaluate loop; no directory, programming or repo assumptions."""
from __future__ import annotations

import hashlib
import json
import os
import time
import threading
import math
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .backends import (
    MockDirector, MockWorker, OpenCodeDirector, OpenCodeWorker, OpenRouterDirector,
    WorkerResult,
)
from .contracts import DecisionError, director_system, director_user_message, parse_decision
from .ledger import apply_updates, initialize, outstanding
from .recovery import reconcile, process_identity
from .storage import save, save_worker_output, utcnow, execution_lock

Log = Callable[[str], None]


class BudgetError(RuntimeError):
    pass


def subscription_equivalent(usage: dict[str, Any], cfg: dict[str, Any]) -> float | None:
    """Estimate only with explicitly configured rates and complete token categories."""
    keys = ("input", "output", "cache_read", "cache_write")
    rates = [cfg.get(f"equivalent_{key}_per_million") for key in keys]
    events = usage.get("token_events")
    if any(r is None for r in rates) or not isinstance(events, list) or not events or not usage.get("token_events_complete"):
        return None
    totals = [0, 0, 0, 0]
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("cache"), dict):
            return None
        values = [event.get("input"), event.get("output"), event["cache"].get("read"), event["cache"].get("write")]
        if any(not isinstance(x, (int, float)) or isinstance(x, bool) or x < 0 for x in values):
            return None
        totals = [a + b for a, b in zip(totals, values)]
    return sum(count * rate for count, rate in zip(totals, rates)) / 1_000_000


class Engine:
    def __init__(self, cfg: dict[str, Any], *, director: Any = None, worker: Any = None, log: Log = print):
        self.telemetry = None
        self.cfg = cfg
        self.log = log
        self._lock = threading.RLock()
        self.prices = None
        self.prices_updated_at = 0.0
        mode = cfg["mode"]
        if director is not None:
            self.director = director
        elif mode == "mock":
            self.director = MockDirector()
        elif cfg["director"]["backend"] == "openrouter":
            self.director = OpenRouterDirector(cfg["director"])
        else:
            from pathlib import Path
            workdir = cfg["worker"]["working_directory"]
            cwd = Path(workdir).expanduser().resolve() if workdir else Path.home()
            self.director = OpenCodeDirector(cfg["director"], cwd)
        self.worker = worker if worker is not None else (MockWorker() if mode == "mock" else OpenCodeWorker(cfg["worker"]))

    def _premium_decision(self, state: dict[str, Any]) -> dict[str, Any]:
        limits = self.cfg["limits"]
        system = director_system()
        stipulations = self.cfg["director"].get("review_stipulations", [])
        if stipulations:
            system += "\nUser-configured review stipulations:\n" + "\n".join(f"- {item}" for item in stipulations)
        user = director_user_message(state, None)
        reserved = 0.0
        needs_usd_budget = False
        state["director_calls"] += 1
        # Reserve before request: if network fails after billing, do not silently refund budget.
        state["premium_reserved_usd"] = round(state["premium_reserved_usd"] + reserved, 6)
        save(state)
        started = time.monotonic()
        call_id = 'call_' + uuid.uuid4().hex
        run_id = state['telemetry_run_id']
        role = 'director'
        self.telemetry.update_call(call_id, run_id=run_id, task_id=state['id'], attempt_id=self.attempt_id,
            actor_role=role, model_id=self.cfg['director']['model'], provider_id=self.cfg['director']['backend'])
        self.telemetry.event(run_id, 'llm.started', task_id=state['id'], payload={'call_id': call_id})
        def capture(obj):
            from .accounting import normalize
            raw = obj.get('usage') or {}
            self.telemetry.update_call(call_id, generation_id=obj.get('id'), usage=normalize(raw, 'openrouter'),
                finished_utc=utcnow(), accounting_status='observed')
            self.telemetry.charge(call_id, 'default', obj.get('id'), 'openrouter_response', raw.get('cost'), {'usage': raw})
        if isinstance(self.director, OpenRouterDirector):
            self.director.on_response = capture
        if isinstance(self.director, OpenCodeDirector):
            self.director.on_event = lambda event: self.telemetry.opencode(run_id, state['id'], event,
                role='director', attempt_id=self.attempt_id, model=self.cfg['director']['model'],
                billing_mode=None if self.cfg['director']['model'].startswith('openrouter/') else 'subscription')
        try:
            result = self.director.call(system, user)
        except Exception as exc:
            self.telemetry.event(run_id, "llm.unknown", task_id=state["id"], payload={"call_id": call_id, "reason": type(exc).__name__})
            state["events"].append({"kind": "director", "at": utcnow(), "outcome": "error",
                                    "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3),
                                    "context_chars": len(system) + len(user), "reported_cost_usd": None, "telemetry_call_id": call_id,
                                    "reserved_usd": reserved, "usage": None})
            save(state)
            raise
        if isinstance(self.director, OpenCodeDirector):
            if not result.usage.get('token_events_complete'):
                self.telemetry.event(run_id, 'session.incomplete', task_id=state['id'], payload={'reason': 'director_stream_incomplete'})
            # Session steps are billable units; placeholder is only a logical boundary.
            self.telemetry.update_call(call_id, accounting_status='boundary')
        else:
            if not isinstance(self.director, OpenRouterDirector):
                capture({'id': getattr(result, 'generation_id', None), 'usage': result.usage})
            self.telemetry.update_call(call_id, finished_utc=utcnow())
            if state['mode'] == 'mock':
                self.telemetry.charge(call_id, 'default', None, 'mock', 0)
        self.telemetry.event(run_id, 'llm.completed', task_id=state['id'], payload={'call_id': call_id})
        if result.cost_usd is not None:
            if needs_usd_budget:
                state["premium_reserved_usd"] = round(max(0, state["premium_reserved_usd"] - reserved), 6)
            state["premium_spent_usd"] = round(state["premium_spent_usd"] + result.cost_usd, 6)
        # If provider omits the cost, the conservative reserve remains allocated.
        state["events"].append({
            "kind": "director", "at": utcnow(), "cost_usd": result.cost_usd, "telemetry_call_id": call_id,
            "reserved_usd": reserved, "usage": result.usage,
            "context_chars": len(system) + len(user),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "model": self.cfg["director"]["model"],
            "api_equivalent_estimate_usd": None,
        })
        amount, source = self._cost(result.cost_usd, result.usage, self.cfg["director"]["model"], state["mode"])
        if isinstance(self.director, OpenCodeDirector):
            self._estimate_missing_steps(run_id, state['id'], self.cfg['director']['model'], state['mode'])
        if source == 'estimated':
            self.telemetry.charge(call_id, 'default', getattr(result, 'generation_id', None), 'estimated', amount)
        self._record_cost(state, amount, source)
        state["events"][-1].update({"budget_cost_usd": amount, "budget_cost_source": source,
                                    "api_equivalent_estimate_usd": amount if source == "estimated" else None})
        save(state)
        return parse_decision(result.text)

    def _check_budget(self, state):
        if state["mode"] == "mock":
            return
        if not state["budget_cost_complete"]:
            raise BudgetError("Cost accounting incomplete; cannot safely enforce the budget")
        if state["budget_reported_usd"] + state["budget_estimated_usd"] >= self.cfg["limits"]["budget_usd"]:
            raise BudgetError("USD budget reached; task remains unfinished")

    def _cost(self, reported, usage, model, mode):
        if mode == "mock":
            return 0.0, "mock"
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported) and reported >= 0:
            return float(reported), "reported"
        try:
            if self.prices is None or time.monotonic() - self.prices_updated_at > 300:
                with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=15) as response:
                    self.prices = {m["id"]: m["pricing"] for m in json.load(response)["data"]}
                self.prices_updated_at = time.monotonic()
            pricing = self.prices[model.removeprefix("openrouter/")]
            steps = (usage or {}).get("token_events")
            if not steps and usage and usage.get("prompt_tokens") is not None:
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                steps = [{"input": usage["prompt_tokens"] - cached, "output": usage.get("completion_tokens"), "reasoning": 0, "cache": {"read": cached}}]
            if not steps or ("token_events_complete" in (usage or {}) and not usage["token_events_complete"]):
                raise ValueError("complete token breakdown unavailable")
            total = 0.0
            for step in steps:
                inp, out, reasoning = step.get("input"), step.get("output"), step.get("reasoning", 0)
                if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in (inp, out, reasoning)):
                    raise ValueError("token breakdown unavailable")
                cache = step.get("cache") or {}
                if any(not isinstance(cache.get(k, 0), int) or isinstance(cache.get(k, 0), bool) or cache.get(k, 0) < 0 for k in ('read', 'write')):
                    raise ValueError('invalid cache token count')
                size = inp + (cache.get("read") or 0) + (cache.get("write") or 0)
                rates = pricing
                for override in sorted(pricing.get("overrides", []), key=lambda x: x.get("min_prompt_tokens", 0)):
                    if size >= override.get("min_prompt_tokens", 0):
                        rates = {**rates, **override}
                p, c = float(rates["prompt"]), float(rates["completion"])
                w = float(rates.get("input_cache_write", p))
                r = float(rates.get("input_cache_read", p))
                if not all(math.isfinite(x) and x >= 0 for x in (p, c, w, r)):
                    raise ValueError("invalid price")
                total += inp * p + (cache.get("read") or 0) * r + (cache.get("write") or 0) * w + (out + reasoning) * c
            return total, "estimated"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.log(f"Cost unavailable for {model}: {exc}")
            return None, "unavailable"

    def _record_cost(self, state, amount, source):
        if amount is None:
            state["budget_cost_complete"] = False
        elif source in ("reported", "estimated"):
            state[f"budget_{source}_usd"] += amount
        self.log(f"Budget: reported ${state['budget_reported_usd']:.4f} + estimated ${state['budget_estimated_usd']:.4f} / ${self.cfg['limits']['budget_usd']:.2f}")

    def _one_worker(self, task: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], WorkerResult]:
        prompt = task["prompt"]
        read_only = task.get("read_only", False)
        run_id = state['telemetry_run_id']
        worker_attempt = self.attempt_id + '.' + task['id']
        self.telemetry.task(run_id, task['id'], parent_task_id=task.get('parent_task_id') or state['id'], status='running',
            contract_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        self.telemetry.event(run_id, 'task.assigned', task_id=task['id'], attempt_id=worker_attempt)
        try:
            if isinstance(self.worker, OpenCodeWorker):
                def lifecycle(update: dict[str, Any]) -> None:
                    if not update:
                        return
                    with self._lock:
                        event = update.pop("event", None)
                        if event is not None:
                            self.telemetry.opencode(run_id, task['id'], event, attempt_id=worker_attempt,
                                role='reviewer' if task.get('review_of') else 'worker', model=task.get('selected_model'))
                            from .storage import task_dir
                            path = task_dir(state["id"]) / "workers" / (task["id"] + ".jsonl")
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with path.open("a", encoding="utf-8") as stream:
                                from .telemetry import safe_opencode_event
                                stream.write(json.dumps(safe_opencode_event(event), ensure_ascii=False) + "\n")
                        if update:
                            if update.get("pid"):
                                update["process_identity"] = process_identity(update["pid"])
                            assignment = state["assignments"][task["id"]]
                            if any(assignment.get(k) != v for k, v in update.items()):
                                assignment.update(update)
                                save(state)
                result = self.worker.call(prompt, read_only, lifecycle=lifecycle, model=task.get("selected_model"))
            else:
                result = self.worker.call(prompt, read_only)
        except Exception as exc:
            message = f"WORKER ERROR: {type(exc).__name__}: {exc}. Worker may have performed partial side effects."
            result = WorkerResult(message, message, False, None, False)
        self.telemetry.task(run_id, task['id'], status='completed' if result.succeeded else 'failed')
        self.telemetry.event(run_id, 'task.finished', task_id=task['id'], attempt_id=worker_attempt,
            payload={'status': 'completed' if result.succeeded else 'failed'})
        if result.session_id:
            self._recover_sessions(run_id, task['id'], result.session_id, task.get('selected_model'))
            self.telemetry.event(run_id, 'session.finished' if result.cost_complete else 'session.incomplete', task_id=task['id'],
                payload={'session_id': result.session_id})
        elif state['mode'] != 'mock' and not isinstance(self.worker, OpenCodeWorker):
            ident = 'call_' + uuid.uuid4().hex
            self.telemetry.update_call(ident, run_id=run_id, task_id=task['id'], attempt_id=worker_attempt,
                actor_role='worker', model_id=task.get('selected_model'), accounting_status='unknown')
            self.telemetry.charge(ident, 'default', None, 'opencode_observed', result.cost_usd)
        if not result.cost_complete and isinstance(self.worker, OpenCodeWorker):
            self.telemetry.event(run_id, 'session.incomplete', task_id=task['id'], payload={'reason': 'incomplete_stream', 'session_id': result.session_id})
        if state['mode'] == 'mock':
            ident = 'call_' + uuid.uuid4().hex
            self.telemetry.update_call(ident, run_id=run_id, task_id=task['id'], attempt_id=worker_attempt, actor_role='worker')
            self.telemetry.charge(ident, 'default', None, 'mock', 0)
        return task, result

    def _dispatch(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        if self.telemetry is None:
            from .telemetry import Recorder
            state.setdefault('telemetry_run_id', 'run_' + uuid.uuid4().hex)
            self.attempt_id = 'attempt_' + uuid.uuid4().hex
            self.telemetry = Recorder(state)
            self.telemetry.run(state['telemetry_run_id'], task_root_id=state['id'])
        tasks = decision["tasks"]
        count = len(tasks)
        limits = self.cfg["limits"]
        self._check_budget(state)
        previous_ids = {e["id"] for e in state["events"] if e.get("kind") == "worker"}
        previous_ids.update(t["id"] for e in state["events"] if e.get("kind") == "dispatch" for t in e.get("tasks", []))
        if any(t["id"] in previous_ids for t in tasks):
            raise DecisionError("director reused a worker task ID")
        attempts: dict[str, int] = {}
        reviews: dict[str, int] = {}
        for ident, prior in state.get("assignments", {}).items():
            root = prior.get("retry_root", ident)
            attempts[root] = attempts.get(root, 0) + 1
            if prior.get("review_of"):
                subject = prior["review_of"]
                reviews[subject] = reviews.get(subject, 0) + 1
        for task in tasks:
            prior = state.get("assignments", {}).get(task.get("retry_of")) if task.get("retry_of") else None
            if task.get("retry_of") and not prior:
                raise DecisionError("retry_of references an unknown assignment")
            root = prior.get("retry_root", task["retry_of"]) if prior else task["id"]
            attempts[root] = attempts.get(root, 0) + 1
            task["retry_root"] = root
            if task.get("review_of"):
                subject = task["review_of"]
                reviews[subject] = reviews.get(subject, 0) + 1
            difficulty = task.get("difficulty", "routine")
            if prior and prior.get("status") in {"failed", "interrupted"} and difficulty == "routine":
                difficulty = "medium"
            key = {"routine": "model", "medium": "medium_model", "high": "high_model"}[difficulty]
            task["selected_model"] = self.cfg["worker"][key]
        state["round"] += 1
        state["worker_count"] += count
        state["events"].append({"kind": "dispatch", "at": utcnow(), "round": state["round"], "tasks": tasks})
        assignments = state.setdefault("assignments", {})
        for task in tasks:
            self.telemetry.task(state['telemetry_run_id'], task['id'], parent_task_id=state['id'], status='queued')
            self.telemetry.event(state['telemetry_run_id'], 'task.created', task_id=task['id'],
                parent_task_id=state['id'], attempt_id=self.attempt_id)
            if task.get('retry_of'):
                self.telemetry.event(state['telemetry_run_id'], 'task.retried', task_id=task['id'],
                    attempt_id=self.attempt_id, payload={'prior_task_id': task['retry_of'], 'reason': 'director_retry'})
            assignments[task["id"]] = {"prompt": task["prompt"], "read_only": task.get("read_only", False),
                                       "status": "queued", "created_at": utcnow(),
                                       "retry_root": task["retry_root"], "retry_of": task.get("retry_of"),
                                       "review_of": task.get("review_of"),
                                       "long_assignment_seconds": self.cfg["worker"]["long_assignment_seconds"]}
        state["status"] = "workers_running"
        state["coordinator_pid"] = os.getpid()
        state["coordinator_identity"] = process_identity(os.getpid())
        save(state)
        self.log(f"Round {state['round']}: dispatching {count} worker(s)")
        # Parallel is opt-in by the director AND only read-only assignments.
        parallel = (decision.get("parallel", False) and count > 1 and
                    all(t.get("read_only", False) for t in tasks))
        def record(task: dict[str, Any], result: WorkerResult, started: float) -> None:
            amount, source = self._cost(result.cost_usd if result.cost_complete else None, result.usage, task["selected_model"], state["mode"])
            self._record_cost(state, amount, source)
            self._estimate_missing_steps(state['telemetry_run_id'], task['id'], task['selected_model'], state['mode'])
            path = save_worker_output(state, task["id"], result.full_output)
            state["events"].append({
                "kind": "worker", "id": task["id"], "at": utcnow(), "telemetry_call_id": "stream",
                "ok": result.succeeded, "summary": result.summary,
                "report_chars": len(result.full_output),
                "output_path": path, "session_id": result.session_id,
                "reported_cost_usd": result.cost_usd,
                "cost_complete": result.cost_complete and result.cost_usd is not None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "model": task["selected_model"], "usage": result.usage or None,
                "estimated_cost_usd": amount if source == "estimated" else None,
                "budget_cost_usd": amount, "budget_cost_source": source,
                "retry_of": task.get("retry_of"),
            })
            assignments[task["id"]].update({"status": "completed" if result.succeeded else "failed",
                "finished_at": utcnow(), "session_id": result.session_id, "output_path": path})
            state["worker_cost_reported_usd"] = round(state["worker_cost_reported_usd"] + (result.cost_usd or 0), 6)
            state["worker_cost_complete"] = state["worker_cost_complete"] and result.cost_complete and result.cost_usd is not None
            if (time.monotonic() - started >= float(self.cfg["worker"]["long_assignment_seconds"]) or
                    (result.cost_usd is not None and result.cost_usd >= float(self.cfg["worker"]["expensive_assignment_usd"]))):
                state["events"].append({"kind": "note", "at": utcnow(), "message": f"Assignment {task['id']} exceeded a duration or reported-cost warning threshold."})
            self.log(f"  {task['id']}: {'ok' if result.succeeded else 'failed'}; report: {path}")
            save(state)
        if parallel:
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures = {}
                def invoke(task: dict[str, Any]) -> tuple[dict[str, Any], WorkerResult, float]:
                    with self._lock:
                        assignments[task["id"]].update({"status": "running", "started_at": utcnow()})
                        save(state)
                    started = time.monotonic()
                    _, result = self._one_worker(task, state)
                    return task, result, started
                for task in tasks:
                    futures[pool.submit(invoke, task)] = task
                for future in as_completed(futures):
                    task, result, started = future.result()
                    with self._lock:
                        record(task, result, started)
        else:
            for task in tasks:
                if not state["budget_cost_complete"] or state["budget_reported_usd"] + state["budget_estimated_usd"] >= float(limits["budget_usd"]):
                    for pending in tasks:
                        if assignments[pending["id"]]["status"] == "queued":
                            self.telemetry.task(state["telemetry_run_id"], pending["id"], status="blocked")
                            self.telemetry.event(state["telemetry_run_id"], "task.blocked", task_id=pending["id"], payload={"reason": "budget"})
                            assignments[pending["id"]].update({"status": "blocked", "note": "Worker budget reached before this assignment started."})
                    state.pop("coordinator_pid", None)
                    state.pop("coordinator_identity", None)
                    save(state)
                    raise BudgetError("reported worker spending reached the worker budget; unknown costs may be higher")
                assignments[task["id"]].update({"status": "running", "started_at": utcnow()})
                save(state)
                started = time.monotonic()
                _, result = self._one_worker(task, state)
                with self._lock:
                    record(task, result, started)
        state["status"] = "running"
        state.pop("coordinator_pid", None)
        state.pop("coordinator_identity", None)
        save(state)

    def _estimate_missing_steps(self, run_id, task_id, model, mode):
        if not self.telemetry.store:
            return
        for call in self.telemetry.calls(run_id) or []:
            if call.get('task_id') != task_id or call.get('provider_id') not in ('opencode', 'openrouter'):
                continue
            if self.telemetry.call_charges(call['call_id']):
                continue
            usage = call.get('usage') or {}
            if usage.get('normalization_method') != 'opencode-v1-exclusive-input-and-output':
                continue
            amount, source = self._cost(None, {'token_events': [usage['source_raw']],
                'token_events_complete': usage['usage_complete']}, model, mode)
            if source == 'estimated':
                self.telemetry.charge(call['call_id'], 'default', call.get('generation_id'), 'estimated', amount)

    def _recover_sessions(self, run_id, task_id, session_id, model):
        url = self.cfg.get('telemetry', {}).get('opencode_url')
        if url:
            from .recovery import import_session_tree
            from .observe import session_fetcher
            try:
                fetch = session_fetcher(url)
                import_session_tree(self.telemetry, run_id, task_id, session_id, fetch, model=model)
            except (ValueError, OSError):
                self.telemetry.event(run_id, 'session.incomplete', task_id=task_id,
                    payload={'session_id': session_id, 'reason': 'session_api_unavailable'})

    def run(self, state: dict[str, Any], *, already_locked: bool = False) -> dict[str, Any]:
        if already_locked:
            return self._instrumented_run(state)
        with execution_lock(state['id']):
            return self._instrumented_run(state)

    def _instrumented_run(self, state: dict[str, Any]) -> dict[str, Any]:
        from .telemetry import Recorder, digest
        state.setdefault('telemetry_run_id', 'run_' + uuid.uuid4().hex)
        self.attempt_id = 'attempt_' + uuid.uuid4().hex
        self.telemetry = Recorder(state, strict=self.cfg.get('telemetry', {}).get('strict', False))
        run_id = state['telemetry_run_id']
        from .ledger import git_snapshot
        workdir = self.cfg['worker'].get('working_directory')
        revision = git_snapshot(workdir) if workdir else None
        existing = next((r for r in (self.telemetry.runs() or []) if r['run_id'] == run_id), {}) if self.telemetry.store else {}
        fields = {} if existing.get('suite_id') else {'config_sha256': digest(self.cfg), 'repo_revision': revision, 'task_root_id': state['id']}
        self.telemetry.run(run_id, **fields, mode=state['mode'])
        self.telemetry.task(run_id, state['id'], status='running',
            parent_task_id=existing.get('task_root_id') if existing.get('suite_id') else None)
        self.telemetry.event(run_id, 'run.started', task_id=state['id'], attempt_id=self.attempt_id)
        try:
            return self._run(state)
        finally:
            for check_id, entry in state.get('ledger', {}).items():
                self.telemetry.event(run_id, 'verify.finished', task_id=state['id'], payload={
                    'check_id': check_id, 'status': 'unverified', 'reported_status': entry.get('status'),
                    'evidence_source': 'worker_report', 'evidence_sha256': digest(entry.get('evidence', []))})
            self.telemetry.task(run_id, state['id'], status=state['status'])
            self.telemetry.run(run_id, status=state['status'], finished_utc=utcnow(),
                measurement_incomplete=bool(state.get('telemetry_incomplete')), telemetry_overhead={
                    'record_seconds': self.telemetry.store.elapsed_seconds if self.telemetry.store else None,
                    'record_cpu_seconds': self.telemetry.store.cpu_seconds if self.telemetry.store else None})
            self.telemetry.event(run_id, 'run.finished', task_id=state['id'], payload={'status': state['status']})
            save(state)

    def _run(self, state: dict[str, Any]) -> dict[str, Any]:
        initialize(state)
        if "budget_cost_complete" not in state:
            state["budget_reported_usd"] = 0.0
            state["budget_estimated_usd"] = 0.0
            state["budget_cost_complete"] = not (state.get("director_calls", 0) or state.get("worker_count", 0))
        if state["status"] == "done":
            raise ValueError("Task already completed. Start a new task instead.")
        # Interrupted or crashed dispatch should not be silently rerun.
        if state["status"] in {"workers_running", "recovery_blocked"}:
            result = reconcile(state)
            if result == "coordinator_running":
                raise RuntimeError("Previous coordinator may still be running; cannot safely resume")
            if result == "worker_running":
                return state
            state["status"] = "running"
            save(state)
        if isinstance(self.director, MockDirector):
            self.director.calls = state["director_calls"]
        while True:
            self.telemetry.event(state['telemetry_run_id'], 'monitor.heartbeat', task_id=state['id'], payload={'status': state['status']})
            try:
                self._check_budget(state)
                decision = self._premium_decision(state)
                state.pop("requested_reports", None)
                apply_updates(state, decision.get("ledger_updates"))
                resolutions = decision.get("resolved_assignments", {})
                if not isinstance(resolutions, dict):
                    raise DecisionError("resolved_assignments must map assignment IDs to reviewer IDs")
                workers = {e.get("id"): e for e in state["events"] if e.get("kind") == "worker"}
                for ident, reviewer in resolutions.items():
                    assignment = state.get("assignments", {}).get(ident)
                    review = workers.get(reviewer) if isinstance(reviewer, str) else None
                    if not assignment or assignment.get("status") not in {"failed", "interrupted", "blocked"} or not review or not review.get("ok") or reviewer == ident:
                        raise DecisionError("assignment resolution requires a separate successful review worker")
                    assignment.update({"status": "reconciled", "review_worker": reviewer})
                if decision.get("retrieve_reports"):
                    from pathlib import Path
                    reports = {}
                    for request in decision["retrieve_reports"]:
                        ident = request if isinstance(request, str) else request["id"]
                        report = workers.get(ident)
                        if not report or not report.get("output_path"):
                            raise DecisionError(f"unknown worker report: {ident}")
                        content = Path(report["output_path"]).read_text(encoding="utf-8")
                        if isinstance(request, dict):
                            if request["end"] > len(content):
                                raise DecisionError(f"report excerpt out of bounds: {ident}")
                            content = content[request["start"]:request["end"]]
                        reports[ident] = {"request": request, "text": content}
                    state["requested_reports"] = reports
                    state["events"].append({"kind": "retrieval", "at": utcnow(), "worker_ids": list(reports), "requests": decision["retrieve_reports"]})
                    save(state)
                    continue
                state["memory"] = decision.get("memory", state["memory"])
                state["events"].append({"kind": "decision", "at": utcnow(),
                                        "action": decision["action"]})
                save(state)
                if decision["action"] == "dispatch":
                    self._check_budget(state)
                    self._dispatch(state, decision)
                elif decision["action"] == "finish":
                    if not any(e.get("kind") == "worker" for e in state["events"]):
                        raise DecisionError("Director cannot claim completion before at least one worker ran")
                    unresolved = [a for a in state.get("assignments", {}).values()
                                  if a.get("status") in {"failed", "interrupted", "blocked", "queued", "running"}]
                    pending = outstanding(state)
                    if decision.get("requirements_reviewed") is not True:
                        pending.append("Director has not confirmed that all requirements and delivery steps were reviewed.")
                    delivery_required = decision.get("delivery_required")
                    if not isinstance(delivery_required, bool):
                        pending.append("Director has not declared whether delivery is required.")
                    elif delivery_required and not any(e.get("kind") == "delivery" for e in state["ledger"].values()):
                        pending.append("Requested delivery has no ledger entry or verification evidence.")
                    state["status"] = "partial" if unresolved or pending else "done"
                    state["answer"] = decision["answer"]
                    if unresolved or pending:
                        state["answer"] += "\n\nPartial: completion is not verified. Outstanding items:\n" + "\n".join("- " + item for item in pending)
                        if unresolved:
                            state["answer"] += "\n- Failed or interrupted assignments require review."
                    else:
                        state["answer"] += "\n\nEvidence is based on worker reports; independent verification is only claimed where documented."
                    save(state)
                    return state
                else:
                    state["status"] = "needs_input"
                    state["answer"] = decision["question"]
                    save(state)
                    return state
            except (BudgetError, DecisionError) as exc:
                self.telemetry.event(state['telemetry_run_id'], 'monitor.intervention', task_id=state['id'], payload={'reason': type(exc).__name__})
                state["status"] = "budget_exhausted" if isinstance(exc, BudgetError) else "error"
                state["answer"] = str(exc)
                state["events"].append({"kind": "note", "at": utcnow(), "message": str(exc)})
                save(state)
                return state
            except Exception as exc:
                state["status"] = "error"
                detail = str(exc)
                if "context" in detail.lower() and any(word in detail.lower() for word in ("window", "length", "limit", "too long", "exceed")):
                    state["answer"] = "Director context-window error; objective and follow-ups remain saved. Reduce requested evidence or use a larger context. " + detail
                else:
                    state["answer"] = f"{type(exc).__name__}: {detail}"
                state["events"].append({"kind": "note", "at": utcnow(), "message": state["answer"]})
                save(state)
                return state
