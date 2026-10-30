"""Plan/delegate/evaluate loop; no directory, programming or repo assumptions."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .backends import (
    MockDirector, MockWorker, OpenCodeDirector, OpenCodeWorker, OpenRouterDirector,
    WorkerResult,
)
from .contracts import DecisionError, director_system, director_user_message, parse_decision
from .storage import save, save_worker_output, utcnow

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


class Engine:
    def __init__(self, cfg: dict[str, Any], *, director: Any = None, worker: Any = None, log: Log = print):
        self.cfg = cfg
        self.log = log
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
        result = self.director.call(system, user)
        if result.cost_usd is not None:
            if needs_usd_budget:
                state["premium_reserved_usd"] = round(max(0, state["premium_reserved_usd"] - reserved), 6)
            state["premium_spent_usd"] = round(state["premium_spent_usd"] + result.cost_usd, 6)
        # If provider omits the cost, the conservative reserve remains allocated.
        state["events"].append({
            "kind": "director", "at": utcnow(), "cost_usd": result.cost_usd,
            "reserved_usd": reserved, "usage": result.usage,
        })
        save(state)
        return parse_decision(result.text, int(limits["max_workers_per_round"]))

    def _one_worker(self, task: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], WorkerResult]:
        prompt = task["prompt"]
        read_only = task.get("read_only", False)
        try:
            result = self.worker.call(prompt, read_only)
        except Exception as exc:
            message = f"WORKER ERROR: {type(exc).__name__}: {exc}. Worker may have performed partial side effects."
            result = WorkerResult(message, message, False, 0.0, False)
        return task, result

    def _dispatch(self, state: dict[str, Any], decision: dict[str, Any]) -> None:
        tasks = decision["tasks"]
        count = len(tasks)
        limits = self.cfg["limits"]
        if state["worker_count"] + count > int(limits["max_total_workers"]):
            raise BudgetError("maximum total delegated workers reached")
        previous_ids = {e["id"] for e in state["events"] if e.get("kind") == "worker"}
        previous_ids.update(t["id"] for e in state["events"] if e.get("kind") == "dispatch" for t in e.get("tasks", []))
        if any(t["id"] in previous_ids for t in tasks):
            raise DecisionError("director reused a worker task ID")
        state["round"] += 1
        state["worker_count"] += count
        state["events"].append({"kind": "dispatch", "at": utcnow(), "round": state["round"], "tasks": tasks})
        state["status"] = "workers_running"
        save(state)
        self.log(f"Round {state['round']}: dispatching {count} worker(s)")
        # Parallel is opt-in by the director AND only read-only assignments.
        parallel = (decision.get("parallel", False) and count > 1 and
                    all(t.get("read_only", False) for t in tasks))
        if parallel:
            with ThreadPoolExecutor(max_workers=min(count, int(limits["max_parallel_workers"]))) as pool:
                results = list(pool.map(lambda task: self._one_worker(task, state), tasks))
        else:
            results = [self._one_worker(task, state) for task in tasks]
        for task, result in results:
            path = save_worker_output(state, task["id"], result.full_output)
            state["events"].append({
                "kind": "worker", "id": task["id"], "at": utcnow(),
                "ok": result.succeeded, "summary": result.summary,
                "output_path": path, "session_id": result.session_id,
                "reported_cost_usd": result.cost_usd,
                "cost_complete": result.cost_complete,
            })
            state["worker_cost_reported_usd"] = round(state["worker_cost_reported_usd"] + result.cost_usd, 6)
            state["worker_cost_complete"] = state["worker_cost_complete"] and result.cost_complete
            self.log(f"  {task['id']}: {'ok' if result.succeeded else 'failed'}; report: {path}")
            save(state)
        state["status"] = "running"
        save(state)

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        if state["status"] == "done":
            raise ValueError("Task already completed. Start a new task instead.")
        # Interrupted or crashed dispatch should not be silently rerun.
        if state["status"] == "workers_running":
            state["events"].append({"kind": "note", "at": utcnow(),
                                    "message": "Previous worker dispatch was interrupted. Side effects and completion are unknown. Investigate before repeating."})
            state["status"] = "running"
            save(state)
        if isinstance(self.director, MockDirector):
            self.director.calls = state["director_calls"]
        while True:
            try:
                decision = self._premium_decision(state)
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
                    state["status"] = "done"
                    state["answer"] = decision["answer"]
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
                state["answer"] = f"{type(exc).__name__}: {exc}"
                state["events"].append({"kind": "note", "at": utcnow(), "message": state["answer"]})
                save(state)
                return state
