"""Durable JSON session state. Stores complete worker outputs separately."""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import data_dir


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def root() -> Path:
    p = data_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_dir(task_id: str) -> Path:
    if not task_id.startswith("task_") or not task_id[5:].isalnum():
        raise ValueError("invalid task id")
    return root() / "tasks" / task_id


@contextmanager
def execution_lock(task_id: str):
    """One coordinator per task; OS releases the lock if its owner dies."""
    path = task_dir(task_id) / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another coordinator is already running this task") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def new_task(objective: str, mode: str) -> dict[str, Any]:
    task_id = "task_" + uuid.uuid4().hex[:16]
    state = {
        "id": task_id,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "objective": objective,
        "followups": [],
        "mode": mode,
        "status": "running",
        "round": 0,
        "worker_count": 0,
        "director_calls": 0,
        "premium_spent_usd": 0.0,
        "premium_reserved_usd": 0.0,
        "worker_cost_reported_usd": 0.0,
        "worker_cost_complete": True,
        "budget_reported_usd": 0.0,
        "budget_estimated_usd": 0.0,
        "budget_cost_complete": True,
        "memory": "",
        "events": [],
        "assignments": {},
        "answer": None,
    }
    save(state)
    return state


def save(state: dict[str, Any]) -> None:
    state["updated_at"] = utcnow()
    path = task_dir(state["id"]) / "state.json"
    atomic_json(path, state)
    atomic_json(root() / "last.json", {"task_id": state["id"]})


def load(task_id: str | None = None) -> dict[str, Any]:
    if not task_id:
        pointer = root() / "last.json"
        if not pointer.is_file():
            raise FileNotFoundError("No previous task. Start with: smart-coder 'your task'")
        task_id = json.loads(pointer.read_text(encoding="utf-8"))["task_id"]
    path = task_dir(task_id) / "state.json"
    if not path.is_file():
        raise FileNotFoundError(f"Task not found: {task_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_worker_output(state: dict[str, Any], job_id: str, text: str) -> str:
    path = task_dir(state["id"]) / "workers" / f"{job_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def recent_tasks(max_items: int = 12) -> list[dict[str, Any]]:
    files = sorted((root() / "tasks").glob("task_*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [json.loads(p.read_text(encoding="utf-8")) for p in files[:max_items]]
