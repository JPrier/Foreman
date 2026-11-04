"""Plan/delegate/evaluate loop; no directory, programming or repo assumptions."""
from __future__ import annotations

import json
import os
import time
import threading
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


def estimated_reservation(cfg: dict[str, Any], system: str, user: str) -> float:
    """Approximate upper-bound preflight, not a billing guarantee.

    Assume one paid input token per character for ASCII-heavy tasks; actual
    provider tokenization, pricing, retries, and reasoning can be different.
    """
    return round((
        len(system + user) * float(cfg["price_input_per_million"]) / 1_000_000
        + int(cfg["max_output_tokens"]) * float(cfg["price_output_per_million"]) / 1_000_000
    ) * float(cfg["request_price_multiplier"]), 6)


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
        self.cfg = cfg
        self.log = log
        self._lock = threading.RLock()
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
        if state["director_calls"] >= int(limits["max_director_calls"]):
            raise BudgetError("maximum director calls reached")
        system = director_system()
        user = director_user_message(state, int(self.cfg["director"]["max_context_chars"]))
        needs_usd_budget = state["mode"] == "live" and self.cfg["director"]["backend"] == "openrouter"
        reserved = estimated_reservation(self.cfg["director"], system, user) if needs_usd_budget else 0.0
        current = state["premium_spent_usd"] + state["premium_reserved_usd"]
        if needs_usd_budget and current + reserved > float(limits["premium_budget_usd"]):
            raise BudgetError(
                f"Premium preflight exceeds ${limits['premium_budget_usd']:.2f} budget "
                f"(already spent/reserved ${current:.3f}, next reserve ${reserved:.3f})."
            )
        state["director_calls"] += 1
        # Reserve before request: if network fails after billing, do not silently refund budget.
        state["premium_reserved_usd"] = round(state["premium_reserved_usd"] + reserved, 6)
        save(state)
        started = time.monotonic()
        try:
            result = self.director.call(system, user)
        except Exception as exc:
            state["events"].append({"kind": "director", "at": utcnow(), "outcome": "error",
                                    "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3),
                                    "context_chars": len(system) + len(user), "reported_cost_usd": None,
                                    "reserved_usd": reserved, "usage": None})
            save(state)
            raise
        if result.cost_usd is not None:
            if needs_usd_budget:
                state["premium_reserved_usd"] = round(max(0, state["premium_reserved_usd"] - reserved), 6)
            state["premium_spent_usd"] = round(state["premium_spent_usd"] + result.cost_usd, 6)
        # If provider omits the cost, the conservative reserve remains allocated.
        state["events"].append({
            "kind": "director", "at": utcnow(), "cost_usd": result.cost_usd,
            "reserved_usd": reserved, "usage": result.usage,
            "context_chars": len(system) + len(user),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "model": self.cfg["director"]["model" if needs_usd_budget else "opencode_model"],
            "api_equivalent_estimate_usd": subscription_equivalent(result.usage, self.cfg["director"]) if result.usage.get("subscription") else None,
        })
        save(state)
        return parse_decision(result.text, int(limits["max_workers_per_round"]))

    def _one_worker(self, task: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], WorkerResult]:
        prompt = task["prompt"]
        read_only = task.get("read_only", False)
        try:
            if isinstance(self.worker, OpenCodeWorker):
                def lifecycle(update: dict[str, Any]) -> None:
                    if not update:
                        return
                    with self._lock:
                        event = update.pop("event", None)
                        if event is not None:
                            from .storage import task_dir
                            path = task_dir(state["id"]) / "workers" / (task["id"] + ".jsonl")
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with path.open("a", encoding="utf-8") as stream:
                                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
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
        return task, result

    def _dispatch(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        tasks = decision["tasks"]
        count = len(tasks)
        limits = self.cfg["limits"]
        if state["worker_count"] + count > int(limits["max_total_workers"]):
            raise BudgetError("maximum total delegated workers reached")
        if state["worker_cost_reported_usd"] >= float(limits["worker_budget_usd"]):
            raise BudgetError("reported worker spending reached the worker budget; unknown costs may be higher")
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
            if attempts[root] > int(limits["max_attempts_per_assignment"]):
                raise BudgetError(f"retry limit reached for {root}; surface the remaining blocker")
            task["retry_root"] = root
            if task.get("review_of"):
                subject = task["review_of"]
                reviews[subject] = reviews.get(subject, 0) + 1
                if reviews[subject] > int(limits["max_reviews_per_subject"]):
                    raise BudgetError(f"review cycle limit reached for {subject}; surface unresolved findings")
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
            path = save_worker_output(state, task["id"], result.full_output)
            state["events"].append({
                "kind": "worker", "id": task["id"], "at": utcnow(),
                "ok": result.succeeded, "summary": result.summary,
                "report_chars": len(result.full_output),
                "output_path": path, "session_id": result.session_id,
                "reported_cost_usd": result.cost_usd,
                "cost_complete": result.cost_complete and result.cost_usd is not None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "model": task["selected_model"], "usage": result.usage or None,
                "estimated_cost_usd": None,
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
            with ThreadPoolExecutor(max_workers=min(count, int(limits["max_parallel_workers"]))) as pool:
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
                if state["worker_cost_reported_usd"] >= float(limits["worker_budget_usd"]):
                    for pending in tasks:
                        if assignments[pending["id"]]["status"] == "queued":
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

    def run(self, state: dict[str, Any], *, already_locked: bool = False) -> dict[str, Any]:
        if already_locked:
            return self._run(state)
        with execution_lock(state["id"]):
            return self._run(state)

    def _run(self, state: dict[str, Any]) -> dict[str, Any]:
        initialize(state)
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
            try:
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
                    if state["round"] >= int(self.cfg["limits"]["max_rounds"]):
                        raise BudgetError("Maximum worker rounds reached; no completion verified")
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
