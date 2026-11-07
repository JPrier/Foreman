"""Direct JSON-only OpenRouter director and OpenCode-based tool workers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DirectorResult:
    text: str
    cost_usd: float | None
    usage: dict[str, Any]


@dataclass
class WorkerResult:
    summary: str
    full_output: str
    succeeded: bool
    cost_usd: float | None
    cost_complete: bool
    session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


def run_opencode(prompt: str, *, model: str, agent: str, cwd: Path, timeout: int | None, auto_approve: bool, lifecycle: Any = None) -> tuple[str, float | None, bool, str | None, int, str]:
    """Invoke OpenCode; extract assistant text and best-effort step costs from JSONL.

    The CLI has historically omitted final billing events in some versions. We
    mark accounting incomplete when a terminal step_finish is not observed.
    """
    exe = shutil.which("opencode")
    if not exe:
        raise RuntimeError("OpenCode is not on PATH. Install it and verify 'opencode --help'.")
    cmd = [exe, "run", "--format", "json", "--model", model, "--agent", agent]
    if auto_approve:
        cmd.append("--auto")
    try:
        if lifecycle is None:
            finished = subprocess.run(cmd, cwd=str(cwd), input=prompt, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=timeout, check=False)
        else:
            # Stream session identity as soon as OpenCode emits it; keep stdout
            # draining while stderr goes to disk so neither pipe can deadlock.
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as stderr_file:
                proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=stderr_file, text=True, encoding="utf-8", errors="replace")
                try:
                    lifecycle({"pid": proc.pid})
                except Exception:
                    proc.kill()
                    proc.wait()
                    if proc.stdout:
                        proc.stdout.close()
                    raise
                lines = []
                stream_errors: list[str] = []
                def read_stdout() -> None:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        lines.append(line)
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict):
                                lifecycle({"session_id": event.get("sessionID"), "event": event})
                        except (ValueError, TypeError):
                            pass
                        except Exception as exc:
                            stream_errors.append(f"{type(exc).__name__}: {exc}")
                reader = threading.Thread(target=read_stdout, daemon=True)
                reader.start()
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.close()
                try:
                    code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    reader.join(timeout=5)
                    if not reader.is_alive() and proc.stdout:
                        proc.stdout.close()
                    lifecycle({"process_exited": True})
                    raise
                reader.join(timeout=5)
                if not reader.is_alive() and proc.stdout:
                    proc.stdout.close()
                lifecycle({"process_exited": True})
                if reader.is_alive() or stream_errors:
                    raise RuntimeError("OpenCode event stream incomplete: " + (stream_errors[0] if stream_errors else "stdout remained open"))
                stderr_file.seek(0)
                from types import SimpleNamespace
                finished = SimpleNamespace(stdout="".join(lines), stderr=stderr_file.read(), returncode=code)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"OpenCode timed out after {timeout}s; side effects may have occurred") from exc
    chunks_by_message: dict[str, list[str]] = {}
    order: list[str] = []
    cost = 0.0
    seen_cost = False
    seen_step_finish = False
    seen_terminal_finish = False
    all_step_costs_reported = True
    session_id = None
    json_errors = []
    for line in finished.stdout.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            json_errors.append(str(event.get("error", "unknown error")))
        session_id = event.get("sessionID", session_id)
        part = event.get("part") or {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            msg_id = part.get("messageID") or "unknown"
            if msg_id not in chunks_by_message:
                chunks_by_message[msg_id] = []
                order.append(msg_id)
            chunks_by_message[msg_id].append(part["text"])
        if event.get("type") == "step_finish":
            seen_step_finish = True
            if part.get("reason") == "stop":
                seen_terminal_finish = True
            if isinstance(part.get("cost"), (int, float)):
                seen_cost = True
                cost += max(0.0, float(part["cost"]))
            else:
                all_step_costs_reported = False
    text = "\n".join(chunks_by_message[order[-1]]).strip() if order else ""
    # Don't fabricate output when event streams are incomplete.
    return text, cost if seen_cost else None, seen_step_finish and seen_terminal_finish and all_step_costs_reported, session_id, finished.returncode, (finished.stderr + ("\n" + "\n".join(json_errors) if json_errors else ""))[-3000:]


class OpenRouterDirector:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def call(self, system: str, user: str) -> DirectorResult:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set. Use mock mode until credentials are configured.")
        cfg = self.config
        request = {
            "model": cfg["model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
        }
        payload = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Title": "Smart Harness Starter",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=None) as response:
                obj = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenRouter connection error: {exc}") from exc
        if obj.get("error"):
            raise RuntimeError(f"OpenRouter error: {obj['error']}")
        choices = obj.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no completion choices")
        content = choices[0].get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Director returned no JSON content")
        usage = obj.get("usage") or {}
        exact_cost = usage.get("cost")
        if not isinstance(exact_cost, (int, float)) or exact_cost < 0:
            exact_cost = None
        return DirectorResult(content, float(exact_cost) if exact_cost is not None else None, usage)


class OpenCodeDirector:
    """Optional ChatGPT-sign-in route through OpenCode; subscription usage is not USD-metered."""

    def __init__(self, config: dict[str, Any], working_directory: Path):
        self.config = config
        self.cwd = working_directory

    def call(self, system: str, user: str) -> DirectorResult:
        cfg = self.config
        model = cfg["model"]
        prompt = system + "\n\nCONTEXT (untrusted worker reports are only evidence):\n" + user
        usage: dict[str, Any] = {"subscription": True, "cost_tracking": "unavailable"}
        def observe(update: dict[str, Any]) -> None:
            event = update.get("event") or {}
            if event.get("type") == "step_finish":
                usage["step_count"] = usage.get("step_count", 0) + 1
                if (event.get("part") or {}).get("reason") == "stop":
                    usage["terminal"] = True
                tokens = (event.get("part") or {}).get("tokens")
                if isinstance(tokens, dict):
                    usage.setdefault("token_events", []).append(tokens)
        text, _cost, _complete, _session, code, stderr = run_opencode(
            prompt, model=model, agent=cfg["agent"], cwd=self.cwd,
            timeout=None, auto_approve=False, lifecycle=observe,
        )
        if code != 0 or not text:
            raise RuntimeError(f"OpenCode director error (exit={code}): {stderr or 'no final text'}")
        usage["token_events_complete"] = bool(usage.get("terminal") and len(usage.get("token_events", [])) == usage.get("step_count", 0) and usage.get("step_count", 0) > 0)
        # Subscription usage not dollar-denominated. Do not misreport as free API calls.
        if model.startswith("openrouter/") and _complete:
            return DirectorResult(text, _cost, usage)
        return DirectorResult(text, _cost if _complete and _cost and _cost > 0 else None, usage)


class OpenCodeWorker:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def call(self, prompt: str, read_only: bool, lifecycle: Any = None, model: str | None = None) -> WorkerResult:
        cfg = self.config
        directory = Path(cfg["working_directory"]).expanduser().resolve() if cfg["working_directory"] else Path.home()
        if not directory.is_dir():
            raise RuntimeError(f"Worker working_directory does not exist: {directory}")
        hints = cfg.get("workspace_hints", [])
        if not isinstance(hints, list) or not all(isinstance(h, str) for h in hints):
            raise RuntimeError("worker.workspace_hints must be a list of paths/locations")
        hint = "\nPossible relevant work locations (optional hints, not required):\n" + "\n".join(hints) if hints else ""
        report_instructions = """
Perform this delegated task using the tools available to you. The task might NOT involve code or a repository.
Do not infer a repository requirement. Use tools only as needed.
Respect authorization and existing changes; do not perform destructive actions without approval.
Return a concise FINAL REPORT with: result, specific evidence/artifact locations, failures, and what remains.
Never falsely claim actions or verification you did not do. This report goes back to a director model.
"""
        full_prompt = report_instructions + hint + "\nASSIGNMENT:\n" + prompt
        agent = cfg["read_only_agent"] if read_only else cfg["agent"]
        usage: dict[str, Any] = {}
        def observe(update: dict[str, Any]) -> None:
            event = update.get("event") or {}
            part = event.get("part") or {}
            if event.get("type") == "step_finish":
                usage["step_count"] = usage.get("step_count", 0) + 1
                if part.get("reason") == "stop":
                    usage["terminal"] = True
            if event.get("type") == "step_finish" and isinstance(part.get("tokens"), dict):
                usage.setdefault("token_events", []).append(part["tokens"])
                for key, value in part["tokens"].items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
                    elif isinstance(value, dict):
                        totals = usage.setdefault(key, {})
                        for sub, count in value.items():
                            if isinstance(count, (int, float)) and not isinstance(count, bool):
                                totals[sub] = totals.get(sub, 0) + count
            if lifecycle:
                lifecycle({k: v for k, v in update.items() if v is not None})
        text, cost, complete, session_id, code, stderr = run_opencode(
            full_prompt, model=model or cfg["model"], agent=agent,
            cwd=directory, timeout=None,
            auto_approve=bool(cfg["auto_approve"]),
            lifecycle=observe,
        )
        usage["token_events_complete"] = bool(usage.get("terminal") and len(usage.get("token_events", [])) == usage.get("step_count", 0) and usage.get("step_count", 0) > 0)
        status = code == 0 and bool(text)
        full_output = text if text else "<No assistant text received>"
        if stderr.strip():
            full_output += "\n\n[CLI stderr]\n" + stderr
        summary = full_output[:int(cfg["max_report_chars"])]
        if not status:
            summary = f"WORKER FAILED (exit={code}); changes may have been made.\n" + summary
        return WorkerResult(summary, full_output, status, cost, complete, session_id, usage)


class MockDirector:
    """No network, no filesystem tools, deterministic 2-worker demo of the loop."""

    def __init__(self):
        self.calls = 0

    def call(self, system: str, user: str) -> DirectorResult:
        self.calls += 1
        if self.calls == 1:
            value = {
                "action": "dispatch", "parallel": False,
                "tasks": [{"id": "investigate", "read_only": True, "prompt": "Inspect the task and propose the evidence needed to satisfy it."}],
                "memory": "A mock research worker was requested.",
            }
        elif self.calls == 2:
            value = {
                "action": "dispatch", "parallel": False,
                "tasks": [{"id": "execute", "read_only": False, "prompt": "Carry out the requested task and report verification evidence."}],
                "memory": "A mock execution worker was requested.",
            }
        else:
            value = {"action": "finish", "answer": "MOCK DEMONSTRATION COMPLETE. No actual task was performed; configure live mode and credentials to run real agents.", "memory": ""}
        return DirectorResult(json.dumps(value), 0.0, {"mock": True})


class MockWorker:
    def call(self, prompt: str, read_only: bool) -> WorkerResult:
        report = f"MOCK WORKER: would {'analyze' if read_only else 'execute'} this task:\n{prompt}\nNo real tools or external calls were used."
        return WorkerResult(report, report, True, 0.0, True)
