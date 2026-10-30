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
        "opencode_model": "openai/gpt-6.1-sol#high",
        "opencode_agent": "smart-harness-director",
        "max_output_tokens": 1400,
        "max_context_chars": 22000,
        "timeout_seconds": 180,
        "price_input_per_million": 4.0,
        "price_output_per_million": 20.0,
        "request_price_multiplier": 1.5,
    },
    "worker": {
        "backend": "opencode",
        "model": "openrouter/z-ai/glm-5.3-flash",
        "agent": "build",
        "read_only_agent": "plan",
        "timeout_seconds": 1800,
        "max_report_chars": 4000,
        "working_directory": None,
        "workspace_hints": [],
        "auto_approve": False,
    },
    "limits": {
        "premium_budget_usd": 0.5,
        "max_rounds": 8,
        "max_director_calls": 9,
        "max_workers_per_round": 3,
        "max_total_workers": 18,
        "max_parallel_workers": 3,
    },
}


def data_dir() -> Path:
    """Allow tests/custom installs to isolate all state."""
    return Path(os.environ.get("SMART_HARNESS_HOME", str(Path.home() / ".smart-harness"))).expanduser().resolve()


def config_path() -> Path:
    return data_dir() / "config.json"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for k, v in override.items():
        if k not in base:
            raise ValueError(f"Unknown configuration field: {k}")
        if isinstance(v, dict) and isinstance(base[k], dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Path | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config file must be a JSON object")
        _merge(config, loaded)
    if config["mode"] not in {"mock", "live"}:
        raise ValueError("mode must be mock or live")
    if config["director"]["backend"] not in {"openrouter", "opencode"}:
        raise ValueError("director.backend must be openrouter or opencode")
    if config["worker"]["backend"] != "opencode":
        raise ValueError("first-pass worker.backend must be opencode")
    for k, v in config["limits"].items():
        if not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"limits.{k} must be positive")
    for k in ("max_rounds", "max_director_calls", "max_workers_per_round", "max_total_workers", "max_parallel_workers"):
        if not isinstance(config["limits"][k], int):
            raise ValueError(f"limits.{k} must be an integer")
    if int(config["director"]["max_output_tokens"]) <= 0:
        raise ValueError("director.max_output_tokens must be positive")
    if int(config["director"]["max_context_chars"]) < 2000:
        raise ValueError("director.max_context_chars must be >= 2000")
    if config["director"]["backend"] == "openrouter":
        for key in ("price_input_per_million", "price_output_per_million", "request_price_multiplier"):
            if not isinstance(config["director"][key], (int, float)) or config["director"][key] <= 0:
                raise ValueError(f"director.{key} must be positive")
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
