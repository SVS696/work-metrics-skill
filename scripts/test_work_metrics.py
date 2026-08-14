#!/usr/bin/env python3
"""Regression tests for portable work-event reconciliation."""

from __future__ import annotations

import unittest

import work_metrics


def source(
    source_id: str,
    events: list[dict[str, object]],
    *,
    complete: bool = True,
    required: bool = True,
) -> dict[str, object]:
    return {
        "id": source_id,
        "kind": "harness",
        "required_for_coverage": required,
        "coverage": {
            "status": "complete" if complete else "partial",
            "started_at": "2026-08-13T10:00:00+00:00",
            "ended_at": "2026-08-13T11:00:00+00:00",
            "reason": None,
        },
        "events": events,
    }


def bundle(
    sources: list[dict[str, object]],
    *,
    declaration: str = "complete",
    terminal: bool = True,
    started_at: str = "2026-08-13T10:00:00+00:00",
    ended_at: str = "2026-08-13T11:00:00+00:00",
    business_calendar: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "work_item": {"id": "case-1", "project_key": "project-1", "kind": "test"},
        "window": {
            "started_at": started_at,
            "ended_at": ended_at,
            "terminal": terminal,
        },
        "policy": {"idle_threshold_seconds": 300, "pulse_grace_seconds": 30},
        "business_calendar": business_calendar,
        "coverage_declaration": declaration,
        "sources": sources,
    }


def pulse(event_id: str, at: str) -> dict[str, object]:
    return {
        "id": event_id,
        "type": "activity_pulse",
        "at": at,
        "category": "harness",
        "attributes": {},
    }


def activity_values(result: dict[str, object]) -> dict[str, object]:
    metrics = result["metric_results"]
    assert isinstance(metrics, list)
    provider = next(item for item in metrics if item["provider"] == "activity-time")
    return provider["values"]


class WorkMetricsTests(unittest.TestCase):
    def test_night_work_counts_without_manual_pause_or_background_business_time(self) -> None:
        calendar: dict[str, object] = {
            "schema": 1,
            "calendar_id": "project-1",
            "timezone": "Europe/Moscow",
            "working_windows": [
                {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
            ],
            "holidays": [],
        }
        result = work_metrics.reconcile(
            bundle(
                [
                    source(
                        "night-session",
                        [
                            pulse("late-work", "2026-08-14T22:00:00+03:00"),
                            pulse("night-work", "2026-08-15T02:00:00+03:00"),
                        ],
                    )
                ],
                started_at="2026-08-14T21:00:00+03:00",
                ended_at="2026-08-15T03:00:00+03:00",
                business_calendar=calendar,
            )
        )
        values = activity_values(result)
        self.assertEqual(values["active_seconds"], 60)
        self.assertEqual(values["business_elapsed_seconds"], 60)
        self.assertEqual(values["off_schedule_active_seconds"], 60)
        self.assertEqual(values["calendar_elapsed_seconds"], 6 * 3600)
        self.assertEqual(values["explicit_pause_seconds"], 0)

    def test_business_calendar_excludes_weekend_and_deferral_but_keeps_real_work(self) -> None:
        calendar: dict[str, object] = {
            "schema": 1,
            "calendar_id": "project-1",
            "timezone": "Europe/Moscow",
            "working_windows": [
                {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
            ],
            "handoff_windows": [
                {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
            ],
            "holidays": [],
        }
        events = [
            pulse("evening-work", "2026-08-14T20:00:00+03:00"),
            {
                "id": "deferred",
                "type": "pause_interval",
                "started_at": "2026-08-14T17:30:00+03:00",
                "finished_at": "2026-08-17T09:30:00+03:00",
                "reason": "deferred",
            },
        ]
        result = work_metrics.reconcile(
            bundle(
                [source("session-a", events)],
                started_at="2026-08-14T17:00:00+03:00",
                ended_at="2026-08-17T10:00:00+03:00",
                business_calendar=calendar,
            )
        )
        values = activity_values(result)
        self.assertEqual(values["calendar_elapsed_seconds"], 65 * 3600)
        self.assertEqual(values["business_elapsed_seconds"], 3630)
        self.assertEqual(values["scheduled_nonworking_seconds"], 63 * 3600)
        self.assertEqual(values["off_schedule_active_seconds"], 30)
        self.assertEqual(values["deferred_seconds"], 64 * 3600)

    def test_ready_to_handoff_wait_is_reported_separately(self) -> None:
        calendar: dict[str, object] = {
            "schema": 1,
            "calendar_id": "project-1",
            "timezone": "Europe/Moscow",
            "working_windows": [
                {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
            ],
            "holidays": [],
        }
        events = [
            pulse("work", "2026-08-14T17:00:00+03:00"),
            {
                "id": "ready",
                "type": "state_marker",
                "at": "2026-08-14T19:00:00+03:00",
                "state": "ready_for_handoff",
            },
            {
                "id": "handoff",
                "type": "state_marker",
                "at": "2026-08-17T10:00:00+03:00",
                "state": "handoff",
            },
        ]
        result = work_metrics.reconcile(
            bundle(
                [source("session-a", events)],
                started_at="2026-08-14T17:00:00+03:00",
                ended_at="2026-08-17T10:00:00+03:00",
                business_calendar=calendar,
            )
        )
        values = activity_values(result)
        self.assertEqual(values["ready_for_handoff_at"], "2026-08-14T16:00:00+00:00")
        self.assertEqual(values["handoff_wait_seconds"], 63 * 3600)
        self.assertEqual(values["handoff_wait_business_seconds"], 3600)
        self.assertEqual(values["business_elapsed_seconds"], 3600)

    def test_real_activity_after_ready_is_kept_but_does_not_restore_idle_wait(self) -> None:
        calendar: dict[str, object] = {
            "schema": 1,
            "calendar_id": "project-1",
            "timezone": "Europe/Moscow",
            "working_windows": [
                {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
            ],
            "holidays": [],
        }
        events = [
            pulse("before-ready", "2026-08-14T17:00:00+03:00"),
            {
                "id": "ready",
                "type": "state_marker",
                "at": "2026-08-14T19:00:00+03:00",
                "state": "ready_for_handoff",
            },
            pulse("post-ready-work", "2026-08-17T09:30:00+03:00"),
            {
                "id": "handoff",
                "type": "state_marker",
                "at": "2026-08-17T10:00:00+03:00",
                "state": "handoff",
            },
        ]
        result = work_metrics.reconcile(
            bundle(
                [source("session-a", events)],
                started_at="2026-08-14T17:00:00+03:00",
                ended_at="2026-08-17T10:00:00+03:00",
                business_calendar=calendar,
            )
        )
        values = activity_values(result)
        self.assertEqual(values["business_elapsed_seconds"], 3630)
        self.assertEqual(values["handoff_wait_business_seconds"], 3600)
        self.assertIn("activity_after_ready_for_handoff", result["warnings"])

    def test_short_gaps_merge_and_long_gaps_become_inferred_idle(self) -> None:
        result = work_metrics.reconcile(
            bundle(
                [
                    source(
                        "session-a",
                        [
                            pulse("p1", "2026-08-13T10:00:00+00:00"),
                            pulse("p2", "2026-08-13T10:03:00+00:00"),
                            pulse("p3", "2026-08-13T10:20:00+00:00"),
                            pulse("p4", "2026-08-13T10:22:00+00:00"),
                        ],
                    )
                ]
            )
        )
        values = activity_values(result)
        self.assertEqual(values["active_observed_seconds"], 120)
        self.assertEqual(values["active_seconds"], 360)
        self.assertEqual(values["inferred_idle_seconds"], 3240)
        self.assertTrue(result["training_eligible"])

    def test_explicit_pause_wins_and_limit_pause_resumes_on_activity(self) -> None:
        events = [
            pulse("p1", "2026-08-13T10:00:00+00:00"),
            pulse("p2", "2026-08-13T10:04:00+00:00"),
            {
                "id": "user-pause",
                "type": "pause_interval",
                "started_at": "2026-08-13T10:01:00+00:00",
                "finished_at": "2026-08-13T10:03:00+00:00",
                "reason": "user_pause",
            },
            {
                "id": "limit",
                "type": "state_marker",
                "at": "2026-08-13T10:05:00+00:00",
                "state": "limit_exhausted",
            },
            pulse("p3", "2026-08-13T10:10:00+00:00"),
            pulse("p4", "2026-08-13T10:12:00+00:00"),
        ]
        result = work_metrics.reconcile(bundle([source("session-a", events)]))
        values = activity_values(result)
        self.assertEqual(values["explicit_pause_seconds"], 420)
        self.assertIn("implicit_resume_from_activity", result["warnings"])
        for interval in values["active_intervals"]:
            self.assertFalse(
                interval["started_at"] < "2026-08-13T10:03:00+00:00"
                and interval["finished_at"] > "2026-08-13T10:01:00+00:00"
            )

    def test_multiple_harnesses_are_unioned_without_double_counting(self) -> None:
        shared = {
            "id": "same-time-a",
            "type": "activity_interval",
            "started_at": "2026-08-13T10:00:00+00:00",
            "finished_at": "2026-08-13T10:02:00+00:00",
            "category": "model",
            "attributes": {},
        }
        duplicate = {**shared, "id": "same-time-b"}
        result = work_metrics.reconcile(
            bundle(
                [
                    source("codex", [shared]),
                    source("claude", [duplicate]),
                ]
            )
        )
        values = activity_values(result)
        self.assertEqual(values["active_observed_seconds"], 120)
        self.assertEqual(values["active_seconds"], 120)
        self.assertTrue(result["training_eligible"])

    def test_partial_or_empty_required_logs_never_train(self) -> None:
        partial = work_metrics.reconcile(
            bundle(
                [source("session-a", [pulse("p1", "2026-08-13T10:00:00+00:00")], complete=False)],
                declaration="partial",
            )
        )
        self.assertFalse(partial["training_eligible"])
        empty = work_metrics.reconcile(bundle([source("session-a", [])]))
        self.assertFalse(empty["training_eligible"])
        self.assertFalse(empty["coverage"]["required_activity_present"])

    def test_observed_counter_provider_is_extensible_and_window_bounded(self) -> None:
        events: list[dict[str, object]] = [pulse("p1", "2026-08-13T10:00:00+00:00")]
        for index, (at, value) in enumerate(
            [
                ("2026-08-13T10:10:00+00:00", 3),
                ("2026-08-13T10:20:00+00:00", 4),
                ("2026-08-13T12:00:00+00:00", 99),
            ],
            start=1,
        ):
            events.append(
                {
                    "id": f"metric-{index}",
                    "type": "metric_observation",
                    "at": at,
                    "metric": "reviews",
                    "value": value,
                    "unit": "count",
                    "dimensions": {"severity": "major"},
                }
            )
        result = work_metrics.reconcile(bundle([source("session-a", events)]))
        providers = result["metric_results"]
        counters = next(item for item in providers if item["provider"] == "observed-counters")
        self.assertEqual(counters["values"][0]["value"], 7)
        self.assertEqual(counters["values"][0]["observation_count"], 2)


if __name__ == "__main__":
    unittest.main()
