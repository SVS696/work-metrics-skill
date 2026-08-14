#!/usr/bin/env python3
"""Translate Vigers case ledgers and harness JSONL into Work Metrics events."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import work_metrics


STATE_MARKERS = {
    "work_started",
    "pause_started",
    "limit_exhausted",
    "deferred",
    "resume",
    "work_finished",
    "ready_for_handoff",
    "handoff",
}
METADATA_TYPES = {"session_meta", "turn_context"}
CYCLE_KINDS = {"initial-specification", "post-handoff-followup"}


def parse_at(value: Any, *, field: str) -> datetime:
    return work_metrics.parse_timestamp(value, field=field)


def read_json(path: Path) -> Any:
    return work_metrics.read_json(path)


def source_coverage(timestamps: list[datetime], *, complete: bool) -> dict[str, Any]:
    return {
        "status": "complete" if complete else "partial",
        "started_at": min(timestamps).isoformat() if timestamps else None,
        "ended_at": max(timestamps).isoformat() if timestamps else None,
        "reason": None if complete else "source coverage was not declared complete",
    }


def automation_source(ledger: Any, *, window_end: datetime) -> tuple[str, dict[str, Any]]:
    if not isinstance(ledger, dict) or ledger.get("schema") not in {1, 2}:
        raise work_metrics.WorkMetricsError("automation-timing.json has unsupported schema")
    case_id = ledger.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise work_metrics.WorkMetricsError("automation-timing.json has no case_id")
    events: list[dict[str, Any]] = []
    timestamps: list[datetime] = []

    def pulse(event_id: str, at: Any, category: str) -> None:
        if at is None:
            return
        parsed = parse_at(at, field=event_id)
        timestamps.append(parsed)
        events.append(
            {
                "id": event_id,
                "type": "activity_pulse",
                "at": parsed.isoformat(),
                "category": category,
                "attributes": {},
            }
        )

    for stage in ledger.get("stages", []):
        if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
            continue
        stage_id = stage["id"]
        pulse(f"{stage_id}-started", stage.get("started_at"), "checkpoint")
        pulse(f"{stage_id}-finished", stage.get("finished_at"), "checkpoint")
        for pause_index, pause in enumerate(stage.get("pauses", []), start=1):
            if not isinstance(pause, dict):
                continue
            started = parse_at(
                pause.get("started_at"), field=f"{stage_id}.pauses[{pause_index}].started_at"
            )
            finished = parse_at(
                pause.get("finished_at"), field=f"{stage_id}.pauses[{pause_index}].finished_at"
            )
            timestamps.extend((started, finished))
            events.append(
                {
                    "id": f"{stage_id}-pause-{pause_index}",
                    "type": "pause_interval",
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "reason": pause.get("reason", "explicit_pause"),
                }
            )
        if stage.get("pause_started_at") is not None:
            started = parse_at(
                stage["pause_started_at"], field=f"{stage_id}.pause_started_at"
            )
            timestamps.append(started)
            events.append(
                {
                    "id": f"{stage_id}-open-pause",
                    "type": "pause_interval",
                    "started_at": started.isoformat(),
                    "finished_at": window_end.isoformat(),
                    "reason": stage.get("pause_reason", "explicit_pause"),
                }
            )
        for item in stage.get("checklist", []):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            pulse(f"{item['id']}-started", item.get("started_at"), "checkpoint")
            pulse(f"{item['id']}-completed", item.get("completed_at"), "checkpoint")
    for index, milestone in enumerate(ledger.get("milestones", []), start=1):
        if not isinstance(milestone, dict):
            continue
        at = milestone.get("at")
        pulse(f"milestone-{index}-{milestone.get('kind', 'unknown')}", at, "milestone")
        state = {
            "ready_for_handoff": "ready_for_handoff",
            "development_handoff": "handoff",
        }.get(milestone.get("kind"))
        if state is not None:
            events.append(
                {
                    "id": f"milestone-state-{index}",
                    "type": "state_marker",
                    "at": parse_at(at, field=f"milestone[{index}].at").isoformat(),
                    "state": state,
                }
            )
        if milestone.get("kind") in {"publication", "development_handoff"}:
            events.append(
                {
                    "id": f"milestone-count-{index}",
                    "type": "metric_observation",
                    "at": parse_at(at, field=f"milestone[{index}].at").isoformat(),
                    "metric": milestone["kind"],
                    "value": 1,
                    "unit": "count",
                    "dimensions": {},
                }
            )
    for index, event in enumerate(ledger.get("events", []), start=1):
        if not isinstance(event, dict):
            continue
        state = {
            "case_deferred": "deferred",
            "case_resumed": "resume",
        }.get(event.get("kind"))
        if state is None or event.get("at") is None:
            continue
        at = parse_at(event["at"], field=f"events[{index}].at")
        timestamps.append(at)
        events.append(
            {
                "id": f"case-state-{index}",
                "type": "state_marker",
                "at": at.isoformat(),
                "state": state,
            }
        )
    return case_id, {
        "id": "vigers-automation-timing",
        "kind": "process-ledger",
        "required_for_coverage": False,
        "coverage": source_coverage(timestamps, complete=True),
        "events": events,
    }


def agent_source(path: Path, *, expected_case_id: str) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("case_id") != expected_case_id:
        raise work_metrics.WorkMetricsError("agent-ledger.json belongs to another case")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise work_metrics.WorkMetricsError("agent-ledger.json runs must be an array")
    events: list[dict[str, Any]] = []
    timestamps: list[datetime] = []
    for index, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            raise work_metrics.WorkMetricsError(f"agent run {index} must be an object")
        finished = parse_at(run.get("at"), field=f"agent run {index}.at")
        duration = work_metrics.non_negative_number(
            run.get("duration_seconds"), field=f"agent run {index}.duration_seconds"
        )
        started = finished - timedelta(seconds=duration)
        timestamps.extend((started, finished))
        events.append(
            {
                "id": f"run-{index}",
                "type": "activity_interval",
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "category": "model",
                "attributes": {
                    "role": run.get("role"),
                    "model": run.get("model"),
                },
            }
        )
        observations: list[tuple[str, Any, str, dict[str, Any]]] = [
            ("model_run", 1, "count", {"model": run.get("model", "unknown")}),
            ("retries", run.get("retries"), "count", {}),
            ("input_tokens", run.get("input_tokens"), "count", {}),
            ("output_tokens", run.get("output_tokens"), "count", {}),
        ]
        findings = run.get("findings", {})
        if isinstance(findings, dict):
            observations.extend(
                ("findings", findings.get(severity), "count", {"severity": severity})
                for severity in ("blocker", "major", "minor")
            )
        for metric_index, (metric, value, unit, dimensions) in enumerate(
            observations, start=1
        ):
            if value is None:
                continue
            work_metrics.non_negative_number(value, field=f"agent run {index}.{metric}")
            events.append(
                {
                    "id": f"run-{index}-metric-{metric_index}",
                    "type": "metric_observation",
                    "at": finished.isoformat(),
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "dimensions": dimensions,
                }
            )
    return {
        "id": "vigers-agent-ledger",
        "kind": "model-ledger",
        "required_for_coverage": False,
        "coverage": source_coverage(timestamps, complete=True),
        "events": events,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise work_metrics.WorkMetricsError(f"Cannot read JSONL {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise work_metrics.WorkMetricsError(
                f"{path}:{line_number} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise work_metrics.WorkMetricsError(f"{path}:{line_number} must be an object")
        records.append(record)
    return records


def record_timestamp(record: dict[str, Any]) -> Any:
    return record.get("timestamp", record.get("at"))


def record_type(record: dict[str, Any]) -> str | None:
    value = record.get("type")
    if isinstance(value, str):
        return value
    payload = record.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload["type"]
    return None


def record_state(record: dict[str, Any]) -> str | None:
    for candidate in (
        record.get("state"),
        record_type(record),
        record.get("payload", {}).get("state")
        if isinstance(record.get("payload"), dict)
        else None,
    ):
        if candidate in STATE_MARKERS:
            return str(candidate)
    return None


def harness_source(path: Path, *, index: int, complete: bool) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    timestamps: list[datetime] = []
    for record_index, record in enumerate(load_jsonl(path), start=1):
        timestamp = record_timestamp(record)
        if timestamp is None:
            continue
        at = parse_at(timestamp, field=f"{path}:{record_index}.timestamp")
        timestamps.append(at)
        kind = record_type(record)
        if kind in METADATA_TYPES:
            continue
        state = record_state(record)
        if state is not None:
            events.append(
                {
                    "id": f"record-{record_index}-state",
                    "type": "state_marker",
                    "at": at.isoformat(),
                    "state": state,
                }
            )
        else:
            events.append(
                {
                    "id": f"record-{record_index}",
                    "type": "activity_pulse",
                    "at": at.isoformat(),
                    "category": "harness",
                    "attributes": {"record_type": kind},
                }
            )
    return {
        "id": f"harness-{index}-{path.name}",
        "kind": "harness",
        "required_for_coverage": True,
        "coverage": source_coverage(timestamps, complete=complete),
        "events": events,
    }


def build_bundle(
    *,
    case_root: Path,
    forecast_path: Path | None,
    project_key_override: str | None = None,
    harness_paths: list[Path],
    logs_complete: bool,
    idle_threshold_seconds: int,
    pulse_grace_seconds: int,
    business_calendar: dict[str, Any] | None = None,
    cycle_kind: str = "initial-specification",
    parent_case_id: str | None = None,
) -> dict[str, Any]:
    if cycle_kind not in CYCLE_KINDS:
        raise work_metrics.WorkMetricsError(f"unsupported Vigers cycle kind: {cycle_kind}")
    if cycle_kind == "post-handoff-followup" and (
        not isinstance(parent_case_id, str) or not parent_case_id.strip()
    ):
        raise work_metrics.WorkMetricsError(
            "post-handoff follow-up requires --parent-case-id"
        )
    if logs_complete and not harness_paths:
        raise work_metrics.WorkMetricsError(
            "--logs-complete requires at least one harness log"
        )
    forecast: dict[str, Any] | None = None
    if forecast_path is not None:
        candidate = read_json(forecast_path)
        if not isinstance(candidate, dict) or candidate.get(
            "fingerprint"
        ) != work_metrics.canonical_fingerprint(candidate):
            raise work_metrics.WorkMetricsError("timing forecast fingerprint mismatch")
        forecast = candidate
    if cycle_kind == "initial-specification" and forecast is None:
        raise work_metrics.WorkMetricsError(
            "initial specification reconciliation requires --forecast"
        )
    forecast_project_key = forecast.get("project_key") if forecast is not None else None
    if (
        project_key_override is not None
        and forecast_project_key is not None
        and project_key_override != forecast_project_key
    ):
        raise work_metrics.WorkMetricsError(
            "explicit project key differs from timing forecast"
        )
    project_key = project_key_override or forecast_project_key
    if not isinstance(project_key, str) or not project_key.strip():
        raise work_metrics.WorkMetricsError("timing forecast has no project_key")
    automation_path = case_root / "automation-timing.json"
    ledger = read_json(automation_path)
    stages = ledger.get("stages", []) if isinstance(ledger, dict) else []
    starts = [
        parse_at(stage["started_at"], field=f"{stage.get('id', 'stage')}.started_at")
        for stage in stages
        if isinstance(stage, dict) and stage.get("started_at") is not None
    ]
    if not starts:
        raise work_metrics.WorkMetricsError("automation timing has no started stage")
    milestones = ledger.get("milestones", []) if isinstance(ledger, dict) else []
    handoff = next(
        (
            item
            for item in milestones
            if isinstance(item, dict) and item.get("kind") == "development_handoff"
        ),
        None,
    )
    harnesses = [
        harness_source(path, index=index, complete=logs_complete)
        for index, path in enumerate(harness_paths, start=1)
    ]
    if handoff is not None:
        window_end = parse_at(handoff.get("at"), field="development_handoff.at")
    else:
        known_ends: list[datetime] = []
        for value, field in (
            (ledger.get("updated_at"), "automation-timing.updated_at"),
            (
                forecast.get("generated_at") if forecast is not None else None,
                "timing-forecast.generated_at",
            ),
        ):
            if value is not None:
                known_ends.append(parse_at(value, field=field))
        for source in harnesses:
            ended_at = source["coverage"]["ended_at"]
            if ended_at is not None:
                known_ends.append(parse_at(ended_at, field=f"{source['id']}.ended_at"))
        window_end = max(known_ends or starts)
    window_start = min(starts)
    if window_end < window_start:
        raise work_metrics.WorkMetricsError("reconciliation window ends before case start")
    case_id, automation = automation_source(ledger, window_end=window_end)
    sources = [automation]
    agent_path = case_root / "agent-ledger.json"
    if agent_path.exists():
        sources.append(agent_source(agent_path, expected_case_id=case_id))
    sources.extend(harnesses)
    bundle: dict[str, Any] = {
        "schema": 1,
        "work_item": {
            "id": case_id,
            "project_key": project_key,
            "kind": "specification",
            "cycle_kind": cycle_kind,
            "parent_id": parent_case_id.strip() if parent_case_id else None,
        },
        "window": {
            "started_at": window_start.isoformat(),
            "ended_at": window_end.isoformat(),
            "terminal": handoff is not None,
        },
        "policy": {
            "idle_threshold_seconds": idle_threshold_seconds,
            "pulse_grace_seconds": pulse_grace_seconds,
        },
        "business_calendar": business_calendar,
        "coverage_declaration": "complete" if logs_complete else "partial",
        "sources": sources,
    }
    work_metrics.validate_bundle(bundle)
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--case-root", required=True)
    reconcile_parser.add_argument("--forecast")
    reconcile_parser.add_argument("--project-key")
    reconcile_parser.add_argument("--harness-log", action="append", default=[])
    reconcile_parser.add_argument("--logs-complete", action="store_true")
    reconcile_parser.add_argument(
        "--cycle-kind",
        choices=sorted(CYCLE_KINDS),
        default="initial-specification",
    )
    reconcile_parser.add_argument("--parent-case-id")
    reconcile_parser.add_argument(
        "--idle-threshold-seconds", type=int, default=work_metrics.DEFAULT_IDLE_THRESHOLD_SECONDS
    )
    reconcile_parser.add_argument(
        "--pulse-grace-seconds", type=int, default=work_metrics.DEFAULT_PULSE_GRACE_SECONDS
    )
    reconcile_parser.add_argument(
        "--business-calendar",
        help="Project-local business calendar JSON for business elapsed metrics",
    )
    reconcile_parser.add_argument("--write-bundle")
    reconcile_parser.add_argument("--write")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        bundle = build_bundle(
            case_root=Path(args.case_root).expanduser().resolve(),
            forecast_path=(
                Path(args.forecast).expanduser().resolve() if args.forecast else None
            ),
            project_key_override=args.project_key,
            harness_paths=[Path(item).expanduser().resolve() for item in args.harness_log],
            logs_complete=args.logs_complete,
            idle_threshold_seconds=args.idle_threshold_seconds,
            pulse_grace_seconds=args.pulse_grace_seconds,
            business_calendar=(
                read_json(Path(args.business_calendar).expanduser().resolve())
                if args.business_calendar
                else None
            ),
            cycle_kind=args.cycle_kind,
            parent_case_id=args.parent_case_id,
        )
        if args.write_bundle:
            work_metrics.atomic_json(Path(args.write_bundle).expanduser().resolve(), bundle)
        result = work_metrics.reconcile(bundle)
        if args.write:
            work_metrics.atomic_json(Path(args.write).expanduser().resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, work_metrics.WorkMetricsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
