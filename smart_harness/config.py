"""Load user configuration without requiring credentials or a repository."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "mode": "mock",
    "director": {
        "backend": "openrouter",
        "model": "anthropic/claude-opus-5.5",
        "agent": "smart-harness-director",
        "review_stipulations": [],
        "timeout_seconds": 180,
    },
    "worker": {
        "backend": "opencode",
        "model": "openrouter/z-ai/glm-5.3-flash",
        "medium_model": "openrouter/z-ai/glm-5.3-flash",
        "high_model": "openrouter/z-ai/glm-5.3-flash",
        "long_assignment_seconds": 900,
        "expensive_assignment_usd": 0.25,
        "agent": "build",
        "read_only_agent": "plan",
        "timeout_seconds": 1800,
        "max_report_chars": 4000,
        "working_directory": None,
        "workspace_hints": [],
        "auto_approve": False,
    },
    "limits": {
        "budget_usd": 50.0,
    },
}


def data_dir() -> Path:
    """Allow tests/custom installs to isolate all state."""
    return Path(os.environ.get("SMART_HARNESS_HOME", str(Path.home() / ".smart-harness"))).expanduser().resolve()


def config_path() -> Path:
    directory = data_dir()
    jsonc = directory / "config.jsonc"
    legacy = directory / "config.json"
    return jsonc if jsonc.exists() or not legacy.exists() else legacy


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for k, v in override.items():
        if k not in base:
            raise ValueError(f"Unknown configuration field: {k}")
        if isinstance(v, dict) and isinstance(base[k], dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _strip_comments(text: str) -> str:
    """Remove // comments outside JSON strings, preserving line numbers."""
    result = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            result.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            result.append(c)
        elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        else:
            result.append(c)
        i += 1
    return "".join(result)



def load_config(path: Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    if path.is_file():
        loaded = json.loads(_strip_comments(path.read_text(encoding="utf-8-sig")))
        if not isinstance(loaded, dict):
            raise ValueError("config file must be a JSON object")
        director = loaded.get("director", {})
        if isinstance(director, dict):
            old_model = director.pop("opencode_model", None)
            old_agent = director.pop("opencode_agent", None)
            if director.get("backend") == "opencode" and old_model:
                director["model"] = old_model.removesuffix("#high")
            if old_agent:
                director.setdefault("agent", old_agent)
        for key in ("max_output_tokens", "max_context_chars", "price_input_per_million", "price_output_per_million", "request_price_multiplier", "equivalent_input_per_million", "equivalent_output_per_million", "equivalent_cache_read_per_million", "equivalent_cache_write_per_million"):
            director.pop(key, None)
        limits = loaded.get("limits", {})
        if isinstance(limits, dict):
            old_budgets = [limits[k] for k in ("premium_budget_usd", "worker_budget_usd") if isinstance(limits.get(k), (int, float)) and not isinstance(limits[k], bool) and limits[k] > 0]
            if "budget_usd" not in limits and old_budgets:
                limits["budget_usd"] = min(old_budgets)
            for key in ("premium_budget_usd", "worker_budget_usd", "max_rounds", "max_director_calls", "max_workers_per_round", "max_total_workers", "max_parallel_workers", "max_attempts_per_assignment", "max_reviews_per_subject", "director_token_budget", "worker_token_budget", "token_budget"):
                limits.pop(key, None)
        _merge(config, loaded)
    if config["mode"] not in {"mock", "live"}:
        raise ValueError("mode must be mock or live")
    if config["director"]["backend"] not in {"openrouter", "opencode"}:
        raise ValueError("director.backend must be openrouter or opencode")
    if not isinstance(config["director"]["review_stipulations"], list) or any(not isinstance(s, str) or not s.strip() for s in config["director"]["review_stipulations"]):
        raise ValueError("director.review_stipulations must be a list of non-empty strings")
    if config["worker"]["backend"] != "opencode":
        raise ValueError("first-pass worker.backend must be opencode")
    for k, v in config["limits"].items():
        if not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"limits.{k} must be positive")
    for key in ("long_assignment_seconds", "expensive_assignment_usd"):
        if not isinstance(config["worker"][key], (int, float)) or config["worker"][key] <= 0:
            raise ValueError(f"worker.{key} must be positive")
    for key in ("model", "medium_model", "high_model"):
        if not isinstance(config["worker"][key], str) or not config["worker"][key].strip():
            raise ValueError(f"worker.{key} must be a non-empty OpenCode model ID")
    return config


def initialize_config(force: bool = False) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return path
    path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
