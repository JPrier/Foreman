"""Compare independently adjudicated Foreman and direct runs from JSONL records."""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def compare(records):
    pairs = defaultdict(dict)
    for row in records:
        task, method = row.get("task_id"), row.get("method")
        if not task or method not in {"foreman", "direct"}:
            raise ValueError("each row needs task_id and method (foreman or direct)")
        if method in pairs[task]:
            raise ValueError(f"duplicate {method} run for {task}")
        pairs[task][method] = row
    metrics = ("outcome_correct", "delivery_correct", "review_findings", "elapsed_seconds", "human_minutes",
               "retries", "reported_provider_cost_usd", "subscription_equivalent_estimate_usd",
               "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    result = {"paired_tasks": [], "unpaired_tasks": [], "totals": {}}
    for task, methods in pairs.items():
        (result["paired_tasks"] if len(methods) == 2 else result["unpaired_tasks"]).append(task)
    for method in ("foreman", "direct"):
        totals = {}
        for metric in metrics:
            values = [pairs[t][method].get(metric) for t in result["paired_tasks"]]
            known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if metric in {"outcome_correct", "delivery_correct"}:
                known = [int(v) for v in values if isinstance(v, bool)]
            totals[metric] = {"known_total": sum(known) if known else None, "known_count": len(known), "unknown_count": len(values) - len(known)}
        result["totals"][method] = totals
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path, help="One independently adjudicated run per JSONL line")
    args = parser.parse_args()
    records = [json.loads(line) for line in args.records.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(compare(records), indent=2))


if __name__ == "__main__":
    main()
