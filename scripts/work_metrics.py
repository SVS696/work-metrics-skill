#!/usr/bin/env python3
"""Reconcile portable work-event bundles into extensible human metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
EVENT_TYPES = {
    "activity_interval",
    "activity_pulse",
    "pause_interval",
    "state_marker",
    "metric_observation",
}
MARKER_STATES = {
    "work_started",
    "pause_started",
    "limit_exhausted",
    "deferred",
    "resume",
    "work_finished",
    "ready_for_handoff",
    "handoff",
}
DEFAULT_IDLE_THRESHOLD_SECONDS = 300
DEFAULT_PULSE_GRACE_SECONDS = 30
CALENDAR_SCHEMA_VERSION = 1


class WorkMetricsError(RuntimeError):
    """Invalid event bundle or impossible reconciliation."""


Interval = tuple[datetime, datetime]
PauseRecord = tuple[datetime, datetime, str]


def canonical_fingerprint(payload: Any) -> str:
    material = (
        {key: value for key, value in payload.items() if key != "fingerprint"}
        if isinstance(payload, dict)
        else payload
    )
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkMetricsError(f"Cannot read JSON {path}: {exc}") from exc


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorkMetricsError(f"{field} must be an ISO-8601 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkMetricsError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise WorkMetricsError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def non_negative_number(value: Any, *, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise WorkMetricsError(f"{field} must be a non-negative number")
    return float(value)


def parse_clock(value: Any, *, field: str) -> time:
    if not isinstance(value, str) or not value.strip():
        raise WorkMetricsError(f"{field} must be HH:MM")
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise WorkMetricsError(f"{field} must be HH:MM") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise WorkMetricsError(f"{field} must be a local HH:MM without seconds")
    return parsed


def validate_window_rules(value: Any, *, field: str) -> None:
    if not isinstance(value, list) or not value:
        raise WorkMetricsError(f"{field} must be a non-empty array")
    for index, rule in enumerate(value, start=1):
        label = f"{field}[{index}]"
        if not isinstance(rule, dict):
            raise WorkMetricsError(f"{label} must be an object")
        weekdays = rule.get("weekdays")
        if (
            not isinstance(weekdays, list)
            or not weekdays
            or any(
                not isinstance(item, int)
                or isinstance(item, bool)
                or item < 1
                or item > 7
                for item in weekdays
            )
        ):
            raise WorkMetricsError(f"{label}.weekdays must contain ISO weekdays 1..7")
        if len(set(weekdays)) != len(weekdays):
            raise WorkMetricsError(f"{label}.weekdays must not contain duplicates")
        start = parse_clock(rule.get("start"), field=f"{label}.start")
        end = parse_clock(rule.get("end"), field=f"{label}.end")
        if end <= start:
            raise WorkMetricsError(f"{label} must finish after it starts on the same day")


def validate_business_calendar(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("schema") != CALENDAR_SCHEMA_VERSION:
        raise WorkMetricsError("business calendar has unsupported schema")
    calendar_id = payload.get("calendar_id")
    if not isinstance(calendar_id, str) or not calendar_id.strip():
        raise WorkMetricsError("business calendar calendar_id is required")
    timezone_name = payload.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise WorkMetricsError("business calendar timezone is required")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise WorkMetricsError("business calendar timezone is unknown") from exc
    validate_window_rules(payload.get("working_windows"), field="working_windows")
    handoff_windows = payload.get("handoff_windows")
    if handoff_windows is not None:
        validate_window_rules(handoff_windows, field="handoff_windows")
    holidays = payload.get("holidays", [])
    if not isinstance(holidays, list):
        raise WorkMetricsError("business calendar holidays must be an array")
    seen: set[date] = set()
    for index, value in enumerate(holidays, start=1):
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise WorkMetricsError(
                f"business calendar holidays[{index}] must be YYYY-MM-DD"
            ) from exc
        if parsed in seen:
            raise WorkMetricsError("business calendar holidays must not contain duplicates")
        seen.add(parsed)


def calendar_intervals(
    payload: dict[str, Any],
    window: Interval,
    *,
    field: str = "working_windows",
) -> list[Interval]:
    """Materialize project-local schedule windows intersecting one UTC interval."""
    validate_business_calendar(payload)
    rules = payload.get(field)
    if rules is None:
        rules = payload["working_windows"]
    timezone = ZoneInfo(payload["timezone"])
    holidays = {date.fromisoformat(value) for value in payload.get("holidays", [])}
    local_start = window[0].astimezone(timezone).date()
    local_end = window[1].astimezone(timezone).date()
    current = local_start
    intervals: list[Interval] = []
    while current <= local_end:
        if current not in holidays:
            weekday = current.isoweekday()
            for rule in rules:
                if weekday not in rule["weekdays"]:
                    continue
                start = datetime.combine(
                    current,
                    parse_clock(rule["start"], field=f"{field}.start"),
                    tzinfo=timezone,
                ).astimezone(UTC)
                end = datetime.combine(
                    current,
                    parse_clock(rule["end"], field=f"{field}.end"),
                    tzinfo=timezone,
                ).astimezone(UTC)
                clamped = clamp_interval((start, end), window)
                if clamped:
                    intervals.append(clamped)
        current += timedelta(days=1)
    return merge_intervals(intervals)


def validate_bundle(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
        raise WorkMetricsError("event bundle has unsupported schema")
    work_item = payload.get("work_item")
    if not isinstance(work_item, dict):
        raise WorkMetricsError("work_item must be an object")
    for field in ("id", "project_key"):
        if not isinstance(work_item.get(field), str) or not work_item[field].strip():
            raise WorkMetricsError(f"work_item.{field} is required")
    if work_item.get("cycle_kind") is not None and (
        not isinstance(work_item["cycle_kind"], str) or not work_item["cycle_kind"].strip()
    ):
        raise WorkMetricsError("work_item.cycle_kind must be non-empty text or null")
    if work_item.get("parent_id") is not None and (
        not isinstance(work_item["parent_id"], str) or not work_item["parent_id"].strip()
    ):
        raise WorkMetricsError("work_item.parent_id must be non-empty text or null")
    window = payload.get("window")
    if not isinstance(window, dict) or not isinstance(window.get("terminal"), bool):
        raise WorkMetricsError("window with boolean terminal is required")
    started = parse_timestamp(window.get("started_at"), field="window.started_at")
    ended = parse_timestamp(window.get("ended_at"), field="window.ended_at")
    if ended < started:
        raise WorkMetricsError("window.ended_at precedes window.started_at")
    if payload.get("coverage_declaration") not in {"complete", "partial"}:
        raise WorkMetricsError("coverage_declaration must be complete or partial")
    policy = payload.get("policy", {})
    if not isinstance(policy, dict):
        raise WorkMetricsError("policy must be an object")
    for field, default in (
        ("idle_threshold_seconds", DEFAULT_IDLE_THRESHOLD_SECONDS),
        ("pulse_grace_seconds", DEFAULT_PULSE_GRACE_SECONDS),
    ):
        non_negative_number(policy.get(field, default), field=f"policy.{field}")
    calendar = payload.get("business_calendar")
    if calendar is not None:
        validate_business_calendar(calendar)
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise WorkMetricsError("sources must be an array")
    source_ids: set[str] = set()
    event_ids: set[str] = set()
    for source_index, source in enumerate(sources, start=1):
        label = f"sources[{source_index}]"
        if not isinstance(source, dict):
            raise WorkMetricsError(f"{label} must be an object")
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise WorkMetricsError(f"{label}.id is required")
        if source_id in source_ids:
            raise WorkMetricsError(f"duplicate source id {source_id}")
        source_ids.add(source_id)
        if not isinstance(source.get("kind"), str) or not source["kind"].strip():
            raise WorkMetricsError(f"{label}.kind is required")
        if not isinstance(source.get("required_for_coverage"), bool):
            raise WorkMetricsError(f"{label}.required_for_coverage must be boolean")
        coverage = source.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("status") not in {
            "complete",
            "partial",
        }:
            raise WorkMetricsError(f"{label}.coverage.status is invalid")
        for field in ("started_at", "ended_at"):
            if coverage.get(field) is not None:
                parse_timestamp(coverage[field], field=f"{label}.coverage.{field}")
        events = source.get("events")
        if not isinstance(events, list):
            raise WorkMetricsError(f"{label}.events must be an array")
        for event_index, event in enumerate(events, start=1):
            event_label = f"{label}.events[{event_index}]"
            if not isinstance(event, dict):
                raise WorkMetricsError(f"{event_label} must be an object")
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id.strip():
                raise WorkMetricsError(f"{event_label}.id is required")
            qualified_id = f"{source_id}:{event_id}"
            if qualified_id in event_ids:
                raise WorkMetricsError(f"duplicate event id {qualified_id}")
            event_ids.add(qualified_id)
            event_type = event.get("type")
            if event_type not in EVENT_TYPES:
                raise WorkMetricsError(f"{event_label}.type is invalid")
            if event_type == "activity_interval":
                event_start = parse_timestamp(
                    event.get("started_at"), field=f"{event_label}.started_at"
                )
                event_end = parse_timestamp(
                    event.get("finished_at"), field=f"{event_label}.finished_at"
                )
                if event_end < event_start:
                    raise WorkMetricsError(f"{event_label} finishes before it starts")
            elif event_type == "activity_pulse":
                parse_timestamp(event.get("at"), field=f"{event_label}.at")
            elif event_type == "pause_interval":
                pause_start = parse_timestamp(
                    event.get("started_at"), field=f"{event_label}.started_at"
                )
                pause_end = parse_timestamp(
                    event.get("finished_at"), field=f"{event_label}.finished_at"
                )
                if pause_end < pause_start:
                    raise WorkMetricsError(f"{event_label} finishes before it starts")
                reason = event.get("reason", "explicit_pause")
                if not isinstance(reason, str) or not reason.strip():
                    raise WorkMetricsError(f"{event_label}.reason must be non-empty text")
            elif event_type == "state_marker":
                parse_timestamp(event.get("at"), field=f"{event_label}.at")
                if event.get("state") not in MARKER_STATES:
                    raise WorkMetricsError(f"{event_label}.state is invalid")
            elif event_type == "metric_observation":
                parse_timestamp(event.get("at"), field=f"{event_label}.at")
                for field in ("metric", "unit"):
                    if not isinstance(event.get(field), str) or not event[field].strip():
                        raise WorkMetricsError(f"{event_label}.{field} is required")
                non_negative_number(event.get("value"), field=f"{event_label}.value")
                if not isinstance(event.get("dimensions", {}), dict):
                    raise WorkMetricsError(f"{event_label}.dimensions must be an object")


def clamp_interval(interval: Interval, window: Interval) -> Interval | None:
    start = max(interval[0], window[0])
    end = min(interval[1], window[1])
    return (start, end) if end > start else None


def merge_intervals(intervals: list[Interval], *, bridge_seconds: float = 0) -> list[Interval]:
    ordered = sorted((item for item in intervals if item[1] > item[0]), key=lambda x: x[0])
    merged: list[Interval] = []
    bridge = timedelta(seconds=bridge_seconds)
    for start, end in ordered:
        if not merged or start > merged[-1][1] + bridge:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def subtract_intervals(intervals: list[Interval], cuts: list[Interval]) -> list[Interval]:
    result = merge_intervals(intervals)
    for cut_start, cut_end in merge_intervals(cuts):
        next_result: list[Interval] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                next_result.append((start, end))
                continue
            if start < cut_start:
                next_result.append((start, cut_start))
            if cut_end < end:
                next_result.append((cut_end, end))
        result = next_result
    return result


def interval_seconds(intervals: list[Interval]) -> int:
    return round(sum((end - start).total_seconds() for start, end in merge_intervals(intervals)))


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def marker_pause_records(
    markers: list[dict[str, Any]],
    observed: list[Interval],
    window: Interval,
) -> tuple[list[PauseRecord], list[str]]:
    pauses: list[PauseRecord] = []
    warnings: list[str] = []
    opened_at: datetime | None = None
    opened_state: str | None = None
    seen: set[tuple[str, datetime]] = set()
    for marker in sorted(markers, key=lambda item: parse_timestamp(item["at"], field="marker.at")):
        at = parse_timestamp(marker["at"], field="marker.at")
        state = marker["state"]
        if (state, at) in seen:
            continue
        seen.add((state, at))
        if state in {"pause_started", "limit_exhausted", "deferred"}:
            if opened_at is None:
                opened_at, opened_state = at, state
            else:
                warnings.append(f"redundant_{state}_marker")
        elif state == "resume":
            if opened_at is None:
                warnings.append("orphan_resume_marker")
            elif at >= opened_at:
                pauses.append((opened_at, at, str(opened_state)))
                opened_at = None
                opened_state = None
        elif state in {"work_finished", "handoff"} and opened_at is not None:
            if at >= opened_at:
                pauses.append((opened_at, at, str(opened_state)))
            opened_at = None
            opened_state = None
    if opened_at is not None:
        pauses.append((opened_at, window[1], str(opened_state)))

    reconciled: list[PauseRecord] = []
    for pause_start, pause_end, reason in pauses:
        implicit_resume: datetime | None = None
        for active_start, active_end in observed:
            if active_end <= pause_start or active_start >= pause_end:
                continue
            implicit_resume = max(pause_start, active_start)
            break
        if implicit_resume is not None:
            if implicit_resume > pause_start:
                reconciled.append((pause_start, implicit_resume, reason))
            warnings.append("implicit_resume_from_activity")
        else:
            reconciled.append((pause_start, pause_end, reason))
    normalized: list[PauseRecord] = []
    for start, end, reason in reconciled:
        clamped = clamp_interval((start, end), window)
        if clamped:
            normalized.append((clamped[0], clamped[1], reason))
    return normalized, sorted(set(warnings))


def marker_pauses(
    markers: list[dict[str, Any]],
    observed: list[Interval],
    window: Interval,
) -> tuple[list[Interval], list[str]]:
    """Compatibility wrapper returning merged intervals without pause reasons."""
    records, warnings = marker_pause_records(markers, observed, window)
    return merge_intervals([(start, end) for start, end, _ in records]), warnings


def observed_counter_results(
    events: list[dict[str, Any]], window: Interval
) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "metric_observation":
            continue
        at = parse_timestamp(event["at"], field="metric_observation.at")
        if not window[0] <= at <= window[1]:
            continue
        dimensions = event.get("dimensions", {})
        key_material = {
            "metric": event["metric"],
            "unit": event["unit"],
            "dimensions": dimensions,
        }
        key = canonical_fingerprint(key_material)
        entry = totals.setdefault(
            key,
            {**key_material, "value": 0.0, "observation_count": 0},
        )
        entry["value"] += float(event["value"])
        entry["observation_count"] += 1
    normalized: list[dict[str, Any]] = []
    for entry in totals.values():
        value = entry["value"]
        entry["value"] = int(value) if value.is_integer() else round(value, 6)
        normalized.append(entry)
    normalized.sort(
        key=lambda item: (
            item["metric"],
            item["unit"],
            json.dumps(item["dimensions"], ensure_ascii=False, sort_keys=True),
        )
    )
    return {"provider": "observed-counters", "schema": 1, "values": normalized}


def reconcile(payload: Any) -> dict[str, Any]:
    validate_bundle(payload)
    window_payload = payload["window"]
    window: Interval = (
        parse_timestamp(window_payload["started_at"], field="window.started_at"),
        parse_timestamp(window_payload["ended_at"], field="window.ended_at"),
    )
    policy = payload.get("policy", {})
    idle_threshold = float(
        policy.get("idle_threshold_seconds", DEFAULT_IDLE_THRESHOLD_SECONDS)
    )
    pulse_grace = float(policy.get("pulse_grace_seconds", DEFAULT_PULSE_GRACE_SECONDS))
    events = [event for source in payload["sources"] for event in source["events"]]
    observed: list[Interval] = []
    explicit_pause_records: list[PauseRecord] = []
    markers: list[dict[str, Any]] = []
    for event in events:
        event_type = event["type"]
        interval: Interval | None = None
        if event_type == "activity_interval":
            interval = (
                parse_timestamp(event["started_at"], field="activity_interval.started_at"),
                parse_timestamp(event["finished_at"], field="activity_interval.finished_at"),
            )
        elif event_type == "activity_pulse":
            at = parse_timestamp(event["at"], field="activity_pulse.at")
            interval = (at, at + timedelta(seconds=pulse_grace))
        elif event_type == "pause_interval":
            pause = clamp_interval(
                (
                    parse_timestamp(event["started_at"], field="pause_interval.started_at"),
                    parse_timestamp(event["finished_at"], field="pause_interval.finished_at"),
                ),
                window,
            )
            if pause:
                explicit_pause_records.append(
                    (pause[0], pause[1], event.get("reason", "explicit_pause"))
                )
        elif event_type == "state_marker":
            markers.append(event)
        if interval is not None:
            clamped = clamp_interval(interval, window)
            if clamped:
                observed.append(clamped)
    observed = merge_intervals(observed)
    state_pause_records, warnings = marker_pause_records(markers, observed, window)
    pause_records = explicit_pause_records + state_pause_records
    pauses = merge_intervals([(start, end) for start, end, _ in pause_records])
    deferred_intervals = merge_intervals(
        [
            (start, end)
            for start, end, reason in pause_records
            if reason == "deferred"
        ]
    )
    deferred_observed = [
        item
        for interval in observed
        for item in (
            clamp_interval(interval, deferred_interval)
            for deferred_interval in deferred_intervals
        )
        if item
    ]
    observed_without_pauses = merge_intervals(
        subtract_intervals(observed, pauses) + deferred_observed
    )
    clustered = merge_intervals(observed, bridge_seconds=idle_threshold)
    active = merge_intervals(subtract_intervals(clustered, pauses) + deferred_observed)

    required_sources = [
        source for source in payload["sources"] if source["required_for_coverage"]
    ]
    required_activity_present = any(
        event.get("type") in {"activity_interval", "activity_pulse"}
        for source in required_sources
        for event in source["events"]
    )
    coverage_complete = (
        payload["coverage_declaration"] == "complete"
        and bool(required_sources)
        and all(source["coverage"]["status"] == "complete" for source in required_sources)
        and required_activity_present
    )
    coverage = {
        "status": "complete" if coverage_complete else "partial",
        "declaration": payload["coverage_declaration"],
        "required_source_count": len(required_sources),
            "complete_required_source_count": sum(
                source["coverage"]["status"] == "complete" for source in required_sources
            ),
        "required_activity_present": required_activity_present,
        "sources": [
            {
                "id": source["id"],
                "kind": source["kind"],
                "required_for_coverage": source["required_for_coverage"],
                "status": source["coverage"]["status"],
            }
            for source in payload["sources"]
        ],
    }
    elapsed_seconds = round((window[1] - window[0]).total_seconds())
    active_seconds = interval_seconds(active)
    active_observed_seconds = interval_seconds(observed_without_pauses)
    explicit_pause_seconds = interval_seconds(pauses)
    inferred_idle_seconds = max(
        0,
        elapsed_seconds - interval_seconds(merge_intervals(active + pauses)),
    )
    business_calendar = payload.get("business_calendar")
    scheduled: list[Interval] = []
    business: list[Interval] = []
    if isinstance(business_calendar, dict):
        scheduled = calendar_intervals(business_calendar, window)
        # Explicit deferral suspends WIP, but observed work outside the schedule
        # remains real work and must never be erased by the calendar.
        business = merge_intervals(subtract_intervals(scheduled, deferred_intervals) + active)
    scheduled_nonworking_seconds = (
        max(0, elapsed_seconds - interval_seconds(scheduled))
        if isinstance(business_calendar, dict)
        else None
    )
    off_schedule_active_seconds = (
        interval_seconds(subtract_intervals(active, scheduled))
        if isinstance(business_calendar, dict)
        else None
    )
    deferred_seconds = interval_seconds(deferred_intervals)
    ready_markers = sorted(
        parse_timestamp(marker["at"], field="ready_for_handoff.at")
        for marker in markers
        if marker.get("state") == "ready_for_handoff"
        and window[0] <= parse_timestamp(marker["at"], field="ready_for_handoff.at") <= window[1]
    )
    handoff_markers = sorted(
        parse_timestamp(marker["at"], field="handoff.at")
        for marker in markers
        if marker.get("state") == "handoff"
        and window[0] <= parse_timestamp(marker["at"], field="handoff.at") <= window[1]
    )
    ready_at = ready_markers[-1] if ready_markers else None
    handoff_at = handoff_markers[0] if handoff_markers else None
    handoff_wait_seconds = (
        round((handoff_at - ready_at).total_seconds())
        if ready_at is not None and handoff_at is not None and handoff_at >= ready_at
        else None
    )
    handoff_wait_business_seconds = None
    if (
        ready_at is not None
        and handoff_at is not None
        and handoff_at >= ready_at
        and isinstance(business_calendar, dict)
    ):
        handoff_window = (ready_at, handoff_at)
        handoff_scheduled = calendar_intervals(business_calendar, handoff_window)
        handoff_deferred = [
            item
            for item in (clamp_interval(interval, handoff_window) for interval in deferred_intervals)
            if item
        ]
        handoff_active = [
            item
            for interval in active
            if interval[0] > ready_at
            for item in (clamp_interval(interval, handoff_window),)
            if item
        ]
        handoff_available = subtract_intervals(handoff_scheduled, handoff_deferred)
        handoff_wait_business_seconds = interval_seconds(handoff_available)
        # Ready means the primary analysis is complete. Available office time
        # spent only waiting for the human handoff must not teach the model that
        # the analysis itself took longer. Real activity remains real work.
        business = merge_intervals(
            subtract_intervals(business, handoff_available) + handoff_active
        )
        if handoff_active:
            warnings.append("activity_after_ready_for_handoff")
    business_elapsed_seconds = (
        interval_seconds(business) if isinstance(business_calendar, dict) else None
    )
    training_eligible = bool(
        window_payload["terminal"] and coverage_complete and active_seconds > 0
    )
    activity_values = {
        "active_observed_seconds": active_observed_seconds,
        "active_seconds": active_seconds,
        "elapsed_seconds": elapsed_seconds,
        "calendar_elapsed_seconds": elapsed_seconds,
        "business_elapsed_seconds": business_elapsed_seconds,
        "explicit_pause_seconds": explicit_pause_seconds,
        "inferred_idle_seconds": inferred_idle_seconds,
        "scheduled_nonworking_seconds": scheduled_nonworking_seconds,
        "off_schedule_active_seconds": off_schedule_active_seconds,
        "deferred_seconds": deferred_seconds,
        "ready_for_handoff_at": isoformat(ready_at) if ready_at is not None else None,
        "handoff_wait_seconds": handoff_wait_seconds,
        "handoff_wait_business_seconds": handoff_wait_business_seconds,
        "calendar_fingerprint": (
            canonical_fingerprint(business_calendar)
            if isinstance(business_calendar, dict)
            else None
        ),
        "active_intervals": [
            {"started_at": isoformat(start), "finished_at": isoformat(end)}
            for start, end in active
        ],
        "pause_intervals": [
            {"started_at": isoformat(start), "finished_at": isoformat(end)}
            for start, end in pauses
        ],
        "deferred_intervals": [
            {"started_at": isoformat(start), "finished_at": isoformat(end)}
            for start, end in deferred_intervals
        ],
        "business_intervals": [
            {"started_at": isoformat(start), "finished_at": isoformat(end)}
            for start, end in business
        ],
    }
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "work_item": payload["work_item"],
        "window": payload["window"],
        "quality": "reconciled_measured" if training_eligible else "recovered_partial",
        "coverage": coverage,
        "training_eligible": training_eligible,
        "warnings": warnings,
        "metric_results": [
            {"provider": "activity-time", "schema": 2, "values": activity_values},
            observed_counter_results(events, window),
        ],
    }
    result["fingerprint"] = canonical_fingerprint(result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--bundle", required=True)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--bundle", required=True)
    reconcile_parser.add_argument("--write")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = read_json(Path(args.bundle).expanduser().resolve())
        if args.command == "validate":
            validate_bundle(payload)
            print(f"PASS bundle={Path(args.bundle).expanduser().resolve()}")
            return 0
        result = reconcile(payload)
        if args.write:
            atomic_json(Path(args.write).expanduser().resolve(), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, WorkMetricsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
