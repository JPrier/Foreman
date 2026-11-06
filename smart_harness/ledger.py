"""Task obligations and evidence, with conservative filesystem invalidation."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from .contracts import DecisionError, TASK_ID


def initialize(state: dict[str, Any]) -> None:
    ledger = state.setdefault("ledger", {})
    ledger.setdefault("objective", {"kind": "outcome", "text": state["objective"], "status": "open"})
    for i, followup in enumerate(state.get("followups", [])):
        ledger.setdefault(f"followup_{i + 1}", {"kind": "outcome", "text": followup, "status": "open"})


def digest(path: str) -> str | None:
    try:
        p = Path(path).expanduser()
        if not p.is_file():
            return None
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def git_snapshot(path: str) -> dict[str, str] | None:
    """Hash HEAD and tracked/untracked working state, including binary content."""
    try:
        def git(*args: str) -> bytes:
            return subprocess.run(["git", "-C", path, *args], check=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=30).stdout
        root = git("rev-parse", "--show-toplevel").decode("utf-8").strip()
        commit = git("rev-parse", "HEAD").decode("ascii").strip()
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        diff = git("diff", "--binary", "HEAD", "--")
        index_diff = git("diff", "--binary", "--cached", "HEAD", "--")
        untracked = git("ls-files", "--others", "--exclude-standard", "-z")
        h = hashlib.sha256()
        for item in (commit.encode(), status, diff, index_diff):
            h.update(len(item).to_bytes(8, "big")); h.update(item)
        for name in untracked.split(b"\0"):
            if not name:
                continue
            p = Path(root) / os.fsdecode(name)
            contents = digest(str(p))
            if contents is None:
                return None
            h.update(len(name).to_bytes(8, "big")); h.update(name); h.update(contents.encode())
        return {"commit": commit, "fingerprint": h.hexdigest()}
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def stale(evidence: dict[str, Any]) -> bool:
    return (any(digest(path) != expected for path, expected in evidence.get("artifacts", {}).items()) or
            any(git_snapshot(path) != expected for path, expected in evidence.get("repositories", {}).items()))


def apply_updates(state: dict[str, Any], updates: Any) -> None:
    if updates is None:
        return
    if not isinstance(updates, list) or len(updates) > 100:
        raise DecisionError("ledger_updates must be a list of at most 100 entries")
    initialize(state)
    # Work on a copy so an invalid update cannot partially mutate the ledger.
    import copy
    ledger = copy.deepcopy(state["ledger"])
    workers = {e.get("id"): e for e in state["events"] if e.get("kind") == "worker"}
    for update in updates:
        if not isinstance(update, dict):
            raise DecisionError("ledger update must be an object")
        ident = update.get("id")
        if not isinstance(ident, str) or not TASK_ID.fullmatch(ident):
            raise DecisionError("invalid ledger id")
        previous = ledger.get(ident, {})
        if ident.startswith("followup_") and not previous:
            raise DecisionError("followup_ IDs are reserved for user requirements")
        kind = update.get("kind", previous.get("kind"))
        text = update.get("text", previous.get("text"))
        status = update.get("status", previous.get("status", "open"))
        authoritative = ident == "objective" or (ident.startswith("followup_") and ident in ledger)
        if kind not in {"outcome", "delivery", "finding", "blocker"} or not isinstance(text, str) or not text.strip() or (not authoritative and len(text) > 4000):
            raise DecisionError("ledger entries need a valid kind and description")
        if authoritative:
            if kind != previous.get("kind") or text != previous.get("text"):
                raise DecisionError("authoritative requirements cannot be replaced")
        if status not in {"open", "blocked", "confirmed", "hypothesis", "deferred", "resolved", "satisfied"}:
            raise DecisionError("invalid ledger status")
        refs = update.get("evidence", previous.get("evidence", []))
        if not isinstance(refs, list) or len(refs) > 20:
            raise DecisionError("evidence must be a list")
        evidence = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("worker_id") not in workers:
                raise DecisionError("evidence must reference a recorded worker")
            w = workers[ref["worker_id"]]
            artifacts = ref.get("artifacts", {})
            repositories = ref.get("repositories", {})
            if not isinstance(artifacts, dict) or len(artifacts) > 30:
                raise DecisionError("artifacts must map paths to SHA-256 hashes")
            if any(not isinstance(p, str) or not isinstance(h, str) or len(h) != 64 for p, h in artifacts.items()):
                raise DecisionError("invalid artifact hash")
            if not isinstance(repositories, dict) or len(repositories) > 10 or any(not isinstance(p, str) or not isinstance(v, dict) or set(v) != {"commit", "fingerprint"} or not all(isinstance(x, str) for x in v.values()) for p, v in repositories.items()):
                raise DecisionError("invalid repository snapshot")
            try:
                report = Path(w["output_path"]).read_text(encoding="utf-8")
            except (OSError, KeyError):
                raise DecisionError("referenced worker report is unavailable")
            if any(p not in report or h not in report for p, h in artifacts.items()):
                raise DecisionError("artifact paths and hashes must appear in the worker report")
            if any(p not in report or v["commit"] not in report or v["fingerprint"] not in report for p, v in repositories.items()):
                raise DecisionError("repository path, commit and fingerprint must appear in the worker report")
            evidence.append({"worker_id": ref["worker_id"], "report": w.get("output_path"),
                             "artifacts": artifacts, "repositories": repositories, "verification": "worker_report"})
        if status in {"satisfied", "resolved"} and (not evidence or any(not workers[e["worker_id"]].get("ok") for e in evidence)):
            raise DecisionError("resolved entries require successful worker evidence")
        ledger[ident] = {"kind": kind, "text": text, "status": status, "evidence": evidence,
                         "owner": update.get("owner", previous.get("owner")),
                         "reason": update.get("reason", previous.get("reason", ""))}
    state["ledger"] = ledger


def outstanding(state: dict[str, Any]) -> list[str]:
    initialize(state)
    issues = []
    for ident, entry in state["ledger"].items():
        if entry["status"] not in {"satisfied", "resolved"}:
            issues.append(f"{ident}: {entry['status']} — {entry['text']}")
        elif not entry.get("evidence") or any(stale(e) or superseded(state, e) for e in entry["evidence"]):
            issues.append(f"{ident}: missing or stale evidence — {entry['text']}")
    return issues


def superseded(state: dict[str, Any], evidence: dict[str, Any]) -> bool:
    """Unscoped evidence is invalidated by later mutating work or corrections."""
    if evidence.get("artifacts") or evidence.get("repositories"):
        return False
    events = state.get("events", [])
    indices = [i for i, e in enumerate(events) if e.get("kind") == "worker" and e.get("id") == evidence["worker_id"]]
    if not indices:
        return True
    return any((e.get("kind") == "dispatch" and any(not t.get("read_only", False) for t in e.get("tasks", [])))
               or e.get("kind") == "followup" for e in events[indices[-1] + 1:])
