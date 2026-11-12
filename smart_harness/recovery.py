"""Conservative recovery of abandoned coordinator and worker processes."""
from __future__ import annotations

import os
from typing import Any
from .storage import save, utcnow


def process_identity(pid: int) -> str | None:
    """Best-effort start identity avoids mistaking a reused PID for a worker."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return f"windows:{created.dwHighDateTime}:{created.dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    try:
        from pathlib import Path
        stat = Path(f"/proc/{pid}/stat").read_text()
        start = stat.rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"linux:{boot}:{start}"
    except (OSError, IndexError):
        return None


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # unknown/access denied is conservatively alive
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        # A zombie has exited even if its parent has not reaped its PID yet.
        try:
            from pathlib import Path
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except (OSError, IndexError):
            pass
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def process_matches(pid: int, identity: str | None) -> bool:
    if not process_exists(pid):
        return False
    current = process_identity(pid) if identity else None
    return current is None or current == identity


def reconcile(state: dict[str, Any]) -> str:
    if state.get("status") not in {"workers_running", "recovery_blocked"}:
        return "ready"
    coordinator = state.get("coordinator_pid")
    if coordinator and process_matches(coordinator, state.get("coordinator_identity")):
        return "coordinator_running"
    active = []
    for ident, item in state.setdefault("assignments", {}).items():
        if item.get("status") not in {"running", "queued", "interrupted"}:
            continue
        pid = item.get("pid")
        if pid and not item.get("process_exited") and process_matches(pid, item.get("process_identity")):
            active.append(ident)
        item["status"] = "interrupted"
        item["finished_at"] = utcnow()
        item["note"] = "Inspect possible side effects and incomplete accounting before retrying."
        state["worker_cost_complete"] = False
    state.pop("coordinator_pid", None)
    state.pop("coordinator_identity", None)
    if not any(e.get("kind") == "note" and "Previous worker dispatch was interrupted" in e.get("message", "") for e in state["events"][-3:]):
        state["events"].append({"kind": "note", "at": utcnow(), "message": "Previous worker dispatch was interrupted. Side effects and completion are unknown. Investigate before repeating."})
    state["status"] = "recovery_blocked" if active else "interrupted"
    state["answer"] = ("Worker process may still be active: " + ", ".join(active) + ". Wait for it to exit before continuing." if active else
                       "Interrupted assignments require inspection before retrying; side effects and accounting are unknown.")
    save(state)
    return "worker_running" if active else "interrupted"


def import_session_tree(store, run_id, task_id, session_id, fetch, *, model=None):
    """Recursively backfill native sessions. fetch(path) is a versioned API adapter.

    Supported wire contract: GET /session/{id}/message -> {info,parts}[];
    GET /session/{id}/children -> Session[]. Unknown shapes fail visibly.
    """
    import urllib.parse
    from .telemetry import digest
    visited = set()

    def visit(ident, parent=None):
        if ident in visited:
            return
        visited.add(ident)
        store.event(run_id, 'session.linked', task_id=task_id,
            event_id=digest([run_id, ident, 'linked']), payload={'session_id': ident, 'parent_session_id': parent})
        try:
            path = '/session/' + urllib.parse.quote(ident, safe='')
            messages = fetch(path + '/message')
            children = fetch(path + '/children')
            if not isinstance(messages, list) or not isinstance(children, list):
                raise ValueError('Unsupported OpenCode session response')
            terminal = False
            for message in messages:
                info, parts = message['info'], message['parts']
                if info.get('role') != 'assistant':
                    continue
                if not isinstance(parts, list):
                    raise ValueError('Unsupported OpenCode parts')
                steps = [p for p in parts if p.get('type') == 'step-finish']
                if not steps and (info.get('tokens') or info.get('cost') is not None):
                    # A retained assistant message can replace a missing stream step.
                    # Use message identity; never sum message aggregates with known parts.
                    existing = [c for c in store.calls(run_id) if c.get('session_id') == ident and c.get('message_id') == info.get('id')]
                    if not existing:
                        steps = [{'id': info['id'], 'messageID': info['id'], 'tokens': info.get('tokens'),
                                  'cost': info.get('cost'), 'reason': info.get('finish')}]
                terminal = info.get('finish') == 'stop' or any(p.get('reason') == 'stop' for p in steps)
                for part in steps:
                    native_model = info.get('modelID') or model
                    if info.get('providerID') == 'openrouter' and native_model and not native_model.startswith('openrouter/'):
                        native_model = 'openrouter/' + native_model
                    part = {**part, 'messageID': part.get('messageID') or info['id']}
                    store.opencode(run_id, task_id, {'sessionID': ident, 'type': 'step_finish', 'part': part},
                        role='compaction' if info.get('summary') else 'worker', model=native_model)
            store.event(run_id, 'session.recovered' if terminal else 'session.incomplete', task_id=task_id,
                event_id=digest([run_id, ident, 'history_terminal', terminal]),
                payload={'session_id': ident, 'reason': 'terminal_history' if terminal else 'missing_terminal_history'})
            for child in children:
                if not isinstance(child, dict) or not isinstance(child.get('id'), str):
                    raise ValueError('Unsupported child session')
                visit(child['id'], ident)
            store.event(run_id, 'session.enumerated', task_id=task_id,
                event_id=digest([run_id, ident, 'enumerated']), payload={'session_id': ident})
        except (OSError, ValueError, KeyError, TypeError):
            store.event(run_id, 'session.incomplete', task_id=task_id,
                event_id=digest([run_id, ident, 'enumeration_failed']), payload={'session_id': ident, 'reason': 'session_api_or_schema_failure'})
    visit(session_id)
    return sorted(visited)
