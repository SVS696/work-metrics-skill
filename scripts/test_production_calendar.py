#!/usr/bin/env python3
"""Regression tests for production-calendar materialization."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import production_calendar


def calendar() -> dict[str, object]:
    return {
        "schema": 1,
        "calendar_id": "project-1",
        "timezone": "Europe/Moscow",
        "working_windows": [
            {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
        ],
        "handoff_windows": [
            {"weekdays": [1, 2, 3, 4, 5], "start": "10:00", "end": "18:00"}
        ],
        "holidays": [],
    }


def default_states(year: int) -> list[str]:
    states: list[str] = []
    current = date(year, 1, 1)
    for _ in range(production_calendar.days_in_year(year)):
        states.append("1" if current.isoweekday() in {6, 7} else "0")
        current += timedelta(days=1)
    return states


class ProductionCalendarTests(unittest.TestCase):
    def test_materializes_holiday_transfer_and_shortened_day(self) -> None:
        states = default_states(2026)
        for raw_date, state in (
            ("2026-01-05", "1"),
            ("2026-01-06", "2"),
            ("2026-01-10", "0"),
        ):
            offset = (date.fromisoformat(raw_date) - date(2026, 1, 1)).days
            states[offset] = state
        overrides = production_calendar.year_overrides(
            calendar(), year=2026, states="".join(states), shortened_minutes=60
        )
        by_date = {item["date"]: item for item in overrides}
        self.assertEqual(by_date["2026-01-05"]["working_windows"], [])
        self.assertEqual(
            by_date["2026-01-06"]["working_windows"],
            [{"start": "09:00", "end": "17:00"}],
        )
        self.assertEqual(
            by_date["2026-01-06"]["handoff_windows"],
            [{"start": "10:00", "end": "17:00"}],
        )
        self.assertEqual(
            by_date["2026-01-10"]["working_windows"],
            [{"start": "09:00", "end": "18:00"}],
        )

    def test_xmlcalendar_normalizes_the_same_three_day_types(self) -> None:
        xml = b"""<calendar year="2026" date="2025.09.30">
        <days>
          <day d="01.05" t="1"/>
          <day d="01.06" t="2"/>
          <day d="01.10" t="3"/>
        </days>
        </calendar>"""
        states, source_date = production_calendar.xmlcalendar_states(2026, xml)
        self.assertEqual(source_date, "2025.09.30")
        self.assertEqual(states[4], "1")
        self.assertEqual(states[5], "2")
        self.assertEqual(states[9], "0")

    def test_materialize_fails_closed_when_sources_disagree(self) -> None:
        states = "".join(default_states(2026)).encode("ascii")
        xml = b"""<calendar year="2026"><days><day d="01.05" t="1"/></days></calendar>"""
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            production_calendar,
            "load_source",
            side_effect=[states, xml],
        ):
            with self.assertRaisesRegex(
                production_calendar.ProductionCalendarError,
                "isdayoff and xmlcalendar disagree",
            ):
                production_calendar.materialize(
                    calendar(),
                    country="ru",
                    years=[2026],
                    cache_dir=Path(directory),
                    offline=False,
                    refresh=False,
                    verify_xmlcalendar=True,
                    shortened_minutes=60,
                )


if __name__ == "__main__":
    unittest.main()
