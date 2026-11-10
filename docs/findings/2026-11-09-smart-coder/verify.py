"""Reproduce sanitized audit measurements offline; no credentials or model calls."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MILLION = Decimal(1_000_000)


def close(actual, expected):
    assert abs(Decimal(str(actual)) - Decimal(str(expected))) < Decimal("0.000000001"), (actual, expected)


def main():
    evidence = json.loads((ROOT / "evidence.json").read_text(encoding="utf-8"))
    steps = [json.loads(line) for line in (ROOT / "worker-steps.jsonl").read_text(encoding="utf-8").splitlines()]
    grouped = defaultdict(list)
    for step in steps:
        grouped[step["worker"]].append(step)
        expected = (
            Decimal(step["input"]) * Decimal("0.0335")
            + Decimal(step["cache_read"]) * Decimal("0.03")
            + Decimal(step["output"] + step["reasoning"]) * Decimal("0.083")
        ) / MILLION
        close(step["accounted_usd"], expected)
    assert len(steps) == 1562
    assert len(grouped) == len(evidence["workers"]) == 10
    for worker in evidence["workers"]:
        rows = grouped[worker["id"]]
        assert [r["step"] for r in rows] == list(range(1, len(rows) + 1))
        assert len(rows) == worker["steps"]
        for key in ("input", "cache_read", "output", "reasoning"):
            assert sum(r[key] for r in rows) == worker[key]
        close(sum(Decimal(str(r["accounted_usd"])) for r in rows), worker["cost"])
        assert worker["summary_chars"] == 4000 < worker["report_chars"]

    directors = evidence["directors"]
    assert len(directors) == 18
    actions = defaultdict(int)
    higher = 0
    lower_only_cost = Decimal(0)
    for call in directors:
        t = call["tokens"]
        context = t["input"] + t["cache"]["read"] + t["cache"]["write"]
        over = context > 200_000
        higher += over
        p, w, r, o = map(Decimal, ("4", "5", ".2", "15") if over else ("2", "2.5", ".1", "10"))
        cost = (t["input"] * p + t["cache"]["write"] * w + t["cache"]["read"] * r + (t["output"] + t["reasoning"]) * o) / MILLION
        close(call["accounted_usd"], cost)
        lower_only_cost += (t["input"] * Decimal(2) + t["cache"]["write"] * Decimal("2.5") + t["cache"]["read"] * Decimal(".1") + (t["output"] + t["reasoning"]) * Decimal(10)) / MILLION
        assert t["cache"]["read"] == 0
        assert call["without_operator_fields_chars"] < call["packet_chars"]
        actions[call["next_event"] if call["next_event"] == "retrieval" else call["action"]] += 1
    assert higher == 8
    assert actions == {"ask_user": 1, "retrieval": 8, "dispatch": 8, "ledger_rejected": 1}
    assert len(evidence["all_assignments"]) == 37

    worker_cost = sum(Decimal(str(r["accounted_usd"])) for r in steps)
    director_cost = sum(Decimal(str(r["accounted_usd"])) for r in directors)
    nested = sum(Decimal(str(r["accounted_usd"])) for r in evidence["nested_sessions"])
    close(worker_cost, "7.2696718855")
    close(director_cost, "13.3882885")
    close(nested, "0.1791492675")
    close(worker_cost + director_cost, "20.6579603855")
    assert sum(r["cache_read"] for r in steps) == 223_989_504
    assert sum(r["input"] for r in steps) == 14_460_695
    assert sum(r["output"] + r["reasoning"] for r in steps) == 789_801
    for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        assert name in {"README.md", "evidence.json", "worker-steps.jsonl", "verify.py"}
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    print("Verified: 10 workers / 1,562 steps; 18 director calls; 37 assignments; evidence hashes.")
    print(f"Accounted worker=${worker_cost:.10f}; director=${director_cost:.7f}; nested delta=${nested:.10f}")
    print(f"Legacy-tier calculation minus lower-tier counterfactual=${director_cost-lower_only_cost:.7f}")
    print("These calculations reproduce local accounting, not authoritative provider charges.")


if __name__ == "__main__":
    main()
