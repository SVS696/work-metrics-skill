#!/usr/bin/env python3
"""Aggregate auditable model-run cost and finding yield across workflow ledgers."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


class YieldError(RuntimeError):
    """Invalid or incomplete model-run ledger."""


COUNTERS = (
    "runs",
    "duration_seconds",
    "retries",
    "input_tokens",
    "output_tokens",
    "reported",
    "accepted",
    "rejected",
    "duplicate",
    "verified",
    "unclassified_runs",
)


def empty_counters() -> dict[str, float | int]:
    return {name: 0 for name in COUNTERS}


def load_ledger(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YieldError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise YieldError(f"Unsupported agent ledger: {path}")
    if not isinstance(payload.get("case_id"), str) or not isinstance(payload.get("runs"), list):
        raise YieldError(f"Invalid agent ledger identity: {path}")
    return payload


def add_run(counters: dict[str, float | int], run: dict[str, Any]) -> None:
    counters["runs"] += 1
    for field in ("duration_seconds", "retries", "input_tokens", "output_tokens"):
        value = run.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            counters[field] += value
    findings = run.get("findings", {})
    if isinstance(findings, dict):
        counters["reported"] += sum(
            value
            for value in findings.values()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        )
    verification = run.get("verification")
    dispositions = verification.get("dispositions") if isinstance(verification, dict) else None
    if not isinstance(dispositions, dict):
        counters["unclassified_runs"] += 1
        return
    for field in ("accepted", "rejected", "duplicate", "verified"):
        value = dispositions.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counters[field] += value


def finalize(counters: dict[str, float | int]) -> dict[str, Any]:
    result = dict(counters)
    reported = counters["reported"]
    accepted = counters["accepted"]
    result["verified_per_reported"] = round(counters["verified"] / reported, 4) if reported else None
    result["duplicate_per_reported"] = round(counters["duplicate"] / reported, 4) if reported else None
    result["verified_per_accepted"] = round(counters["verified"] / accepted, 4) if accepted else None
    return result


def aggregate(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise YieldError("At least one --agent-ledger is required")
    total = empty_counters()
    grouped: dict[str, dict[str, dict[str, float | int]]] = {
        "role": defaultdict(empty_counters),
        "role_mode": defaultdict(empty_counters),
        "model": defaultdict(empty_counters),
        "lens": defaultdict(empty_counters),
        "status": defaultdict(empty_counters),
    }
    case_ids: list[str] = []
    for path in paths:
        payload = load_ledger(path)
        case_ids.append(payload["case_id"])
        for run in payload["runs"]:
            if not isinstance(run, dict):
                raise YieldError(f"{path}: run must be an object")
            add_run(total, run)
            dimensions = {
                "role": [str(run.get("role", "unknown"))],
                "role_mode": [str(run.get("role_mode", "unknown"))],
                "model": [str(run.get("model", "unknown"))],
                "lens": [str(item) for item in run.get("lenses", [])] or ["unversioned"],
                "status": [str(run.get("status", "completed"))],
            }
            for dimension, values in dimensions.items():
                for value in values:
                    add_run(grouped[dimension][value], run)
    return {
        "schema": 1,
        "ledger_count": len(paths),
        "case_ids": case_ids,
        "totals": finalize(total),
        "by": {
            dimension: {
                key: finalize(value) for key, value in sorted(groups.items())
            }
            for dimension, groups in grouped.items()
        },
        "notes": [
            "unclassified_runs have no final verification receipt and do not count as zero yield",
            "a run with multiple lenses contributes to every named lens and is not additive across lens rows",
        ],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--agent-ledger", type=Path, action="append", required=True)
    root.add_argument("--write", type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        result = aggregate(args.agent_ledger)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.write:
            args.write.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.write.with_suffix(args.write.suffix + ".tmp")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(args.write)
        print(rendered, end="")
        return 0
    except YieldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
