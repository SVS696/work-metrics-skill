#!/usr/bin/env python3
"""Regression tests for the optional Vigers adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import vigers_adapter
import work_metrics


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class VigersAdapterTests(unittest.TestCase):
    def test_complete_multisession_logs_produce_training_candidate_and_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            forecast = {
                "schema": 1,
                "purpose": "human_information_only",
                "project_key": "project-key",
                "generated_at": "2026-08-13T10:00:00+00:00",
            }
            forecast["fingerprint"] = work_metrics.canonical_fingerprint(forecast)
            write_json(root / "timing-forecast.json", forecast)
            write_json(
                root / "automation-timing.json",
                {
                    "schema": 1,
                    "case_id": "case-1",
                    "updated_at": "2026-08-13T10:30:00+00:00",
                    "stages": [
                        {
                            "id": "P01",
                            "started_at": "2026-08-13T10:00:00+00:00",
                            "finished_at": "2026-08-13T10:20:00+00:00",
                            "pauses": [],
                            "pause_started_at": None,
                            "checklist": [],
                        }
                    ],
                    "milestones": [
                        {
                            "kind": "publication",
                            "at": "2026-08-13T10:20:00+00:00",
                        },
                        {
                            "kind": "ready_for_handoff",
                            "at": "2026-08-13T10:20:00+00:00",
                        },
                        {
                            "kind": "development_handoff",
                            "at": "2026-08-13T10:30:00+00:00",
                        },
                    ],
                },
            )
            write_json(
                root / "agent-ledger.json",
                {
                    "schema": 1,
                    "case_id": "case-1",
                    "runs": [
                        {
                            "at": "2026-08-13T10:05:00+00:00",
                            "duration_seconds": 60,
                            "role": "analyst",
                            "model": "test",
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "retries": 1,
                            "findings": {"blocker": 0, "major": 2, "minor": 1},
                        }
                    ],
                },
            )
            log_a = root / "session-a.jsonl"
            log_a.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"timestamp": "2026-08-13T10:00:00+00:00", "type": "session_meta"},
                        {"timestamp": "2026-08-13T10:01:00+00:00", "type": "assistant"},
                        {"timestamp": "2026-08-13T10:03:00+00:00", "type": "limit_exhausted"},
                    ]
                ),
                encoding="utf-8",
            )
            log_b = root / "session-b.jsonl"
            log_b.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"timestamp": "2026-08-13T10:10:00+00:00", "type": "assistant"},
                        {"timestamp": "2026-08-13T10:12:00+00:00", "type": "tool"},
                        {"timestamp": "2026-08-13T11:30:00+00:00", "type": "assistant"},
                    ]
                ),
                encoding="utf-8",
            )
            bundle = vigers_adapter.build_bundle(
                case_root=root,
                forecast_path=root / "timing-forecast.json",
                harness_paths=[log_a, log_b],
                logs_complete=True,
                idle_threshold_seconds=300,
                pulse_grace_seconds=30,
                business_calendar={
                    "schema": 1,
                    "calendar_id": "project-key",
                    "timezone": "UTC",
                    "working_windows": [
                        {
                            "weekdays": [1, 2, 3, 4, 5],
                            "start": "09:00",
                            "end": "18:00",
                        }
                    ],
                    "holidays": [],
                },
            )
            result = work_metrics.reconcile(bundle)
            self.assertTrue(result["training_eligible"])
            self.assertEqual(result["work_item"]["id"], "case-1")
            self.assertEqual(
                result["work_item"]["cycle_kind"], "initial-specification"
            )
            activity = next(
                item for item in result["metric_results"] if item["provider"] == "activity-time"
            )
            self.assertEqual(activity["values"]["elapsed_seconds"], 1800)
            self.assertEqual(activity["values"]["business_elapsed_seconds"], 1200)
            self.assertEqual(activity["values"]["handoff_wait_seconds"], 600)
            self.assertTrue(
                all(
                    interval["finished_at"] <= "2026-08-13T10:30:00+00:00"
                    for interval in activity["values"]["active_intervals"]
                )
            )
            self.assertIn("implicit_resume_from_activity", result["warnings"])
            counters = next(
                item for item in result["metric_results"] if item["provider"] == "observed-counters"
            )
            token_total = next(item for item in counters["values"] if item["metric"] == "input_tokens")
            self.assertEqual(token_total["value"], 100)

    def test_logs_complete_requires_an_actual_harness_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            forecast = {"project_key": "project-key"}
            forecast["fingerprint"] = work_metrics.canonical_fingerprint(forecast)
            write_json(root / "forecast.json", forecast)
            write_json(
                root / "automation-timing.json",
                {
                    "schema": 1,
                    "case_id": "case-1",
                    "stages": [
                        {"id": "P01", "started_at": "2026-08-13T10:00:00+00:00"}
                    ],
                    "milestones": [],
                },
            )
            with self.assertRaisesRegex(work_metrics.WorkMetricsError, "at least one"):
                vigers_adapter.build_bundle(
                    case_root=root,
                    forecast_path=root / "forecast.json",
                    harness_paths=[],
                    logs_complete=True,
                    idle_threshold_seconds=300,
                    pulse_grace_seconds=30,
                )

    def test_followup_cycle_requires_parent_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            forecast = {"project_key": "project-key"}
            forecast["fingerprint"] = work_metrics.canonical_fingerprint(forecast)
            write_json(root / "forecast.json", forecast)
            write_json(
                root / "automation-timing.json",
                {
                    "schema": 1,
                    "case_id": "followup-case",
                    "updated_at": "2026-08-13T10:01:00+00:00",
                    "stages": [
                        {"id": "P01", "started_at": "2026-08-13T10:00:00+00:00"}
                    ],
                    "milestones": [],
                },
            )
            with self.assertRaisesRegex(work_metrics.WorkMetricsError, "parent-case-id"):
                vigers_adapter.build_bundle(
                    case_root=root,
                    forecast_path=root / "forecast.json",
                    harness_paths=[],
                    logs_complete=False,
                    idle_threshold_seconds=300,
                    pulse_grace_seconds=30,
                    cycle_kind="post-handoff-followup",
                )
            followup = vigers_adapter.build_bundle(
                case_root=root,
                forecast_path=None,
                project_key_override="project-key",
                harness_paths=[],
                logs_complete=False,
                idle_threshold_seconds=300,
                pulse_grace_seconds=30,
                cycle_kind="post-handoff-followup",
                parent_case_id="original-case",
            )
            self.assertEqual(
                followup["work_item"],
                {
                    "id": "followup-case",
                    "project_key": "project-key",
                    "kind": "specification",
                    "cycle_kind": "post-handoff-followup",
                    "parent_id": "original-case",
                },
            )


if __name__ == "__main__":
    unittest.main()
