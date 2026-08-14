#!/usr/bin/env python3
"""Materialize country production calendars into project timing calendars."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import work_metrics


ISDAYOFF_URL = "https://isdayoff.ru/api/getdata?year={year}&cc={country}&pre=1"
XMLCALENDAR_URL = "https://xmlcalendar.ru/data/{country}/{year}/calendar.xml"
DEFAULT_CACHE_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "workday-control"
    / "production-calendar"
)


class ProductionCalendarError(RuntimeError):
    """Production-calendar source or materialization is invalid."""


def days_in_year(year: int) -> int:
    return (date(year + 1, 1, 1) - date(year, 1, 1)).days


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_isdayoff(year: int, value: bytes) -> str:
    try:
        calendar = value.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ProductionCalendarError("isdayoff response must be ASCII") from exc
    if len(calendar) != days_in_year(year) or any(item not in "012" for item in calendar):
        raise ProductionCalendarError(
            f"isdayoff calendar for {year} must contain {days_in_year(year)} digits 0, 1, or 2"
        )
    return calendar


def xmlcalendar_states(year: int, value: bytes) -> tuple[str, str | None]:
    try:
        root = ET.fromstring(value)
    except ET.ParseError as exc:
        raise ProductionCalendarError("xmlcalendar response is invalid XML") from exc
    if root.tag != "calendar" or root.get("year") != str(year):
        raise ProductionCalendarError("xmlcalendar response has the wrong year")
    states: list[str] = []
    current = date(year, 1, 1)
    for _ in range(days_in_year(year)):
        states.append("1" if current.isoweekday() in {6, 7} else "0")
        current += timedelta(days=1)
    for item in root.findall("./days/day"):
        raw_date = item.get("d")
        kind = item.get("t")
        if raw_date is None or kind not in {"1", "2", "3"}:
            raise ProductionCalendarError("xmlcalendar day requires d and t=1,2,3")
        try:
            month_text, day_text = raw_date.split(".", maxsplit=1)
            local_day = date(year, int(month_text), int(day_text))
        except (TypeError, ValueError) as exc:
            raise ProductionCalendarError(
                f"xmlcalendar day {raw_date!r} is not MM.DD"
            ) from exc
        offset = (local_day - date(year, 1, 1)).days
        states[offset] = {"1": "1", "2": "2", "3": "0"}[kind]
    return "".join(states), root.get("date")


def cache_path(cache_dir: Path, country: str, year: int, source: str) -> Path:
    suffix = ".txt" if source == "isdayoff" else "-xmlcalendar.xml"
    return cache_dir / f"{country}-{year}{suffix}"


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "work-metrics/1"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ProductionCalendarError(f"production calendar source is unavailable: {url}") from exc


def load_source(
    *,
    cache_dir: Path,
    country: str,
    year: int,
    source: str,
    url: str,
    offline: bool,
    refresh: bool,
) -> bytes:
    target = cache_path(cache_dir, country, year, source)
    if not refresh:
        try:
            return target.read_bytes()
        except FileNotFoundError:
            pass
    if offline:
        raise ProductionCalendarError(f"{source} calendar for {country}-{year} is not cached")
    value = fetch(url)
    atomic_bytes(target, value)
    return value


def daily_windows(
    calendar: dict[str, Any], local_day: date, *, field: str
) -> list[dict[str, str]]:
    rules = calendar.get(field) or calendar["working_windows"]
    return sorted(
        [
            {"start": rule["start"], "end": rule["end"]}
            for rule in rules
            if local_day.isoweekday() in rule["weekdays"]
        ],
        key=lambda item: (item["start"], item["end"]),
    )


def unique_workday_template(
    calendar: dict[str, Any], *, field: str
) -> list[dict[str, str]]:
    weekly = []
    for weekday in range(1, 8):
        sample_day = date(2026, 1, 5) + timedelta(days=weekday - 1)
        windows = daily_windows(calendar, sample_day, field=field)
        if windows:
            weekly.append(json.dumps(windows, sort_keys=True, separators=(",", ":")))
    if not weekly:
        raise ProductionCalendarError(f"{field} has no workday template")
    counts = Counter(weekly)
    template, count = counts.most_common(1)[0]
    if len(counts) > 1 and list(counts.values()).count(count) > 1:
        raise ProductionCalendarError(
            f"{field} has no unique workday template for a transferred working day"
        )
    return json.loads(template)


def clock_minutes(value: str) -> int:
    hour, minute = value.split(":", maxsplit=1)
    return int(hour) * 60 + int(minute)


def format_clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def shorten_windows(
    windows: list[dict[str, str]], minutes: int
) -> list[dict[str, str]]:
    shortened = [dict(item) for item in windows]
    remaining = minutes
    while shortened and remaining:
        last = shortened[-1]
        duration = clock_minutes(last["end"]) - clock_minutes(last["start"])
        if duration > remaining:
            last["end"] = format_clock(clock_minutes(last["end"]) - remaining)
            remaining = 0
        else:
            remaining -= duration
            shortened.pop()
    if remaining or not shortened:
        raise ProductionCalendarError("shortened day removes the whole workday")
    return shortened


def year_overrides(
    calendar: dict[str, Any],
    *,
    year: int,
    states: str,
    shortened_minutes: int,
) -> list[dict[str, Any]]:
    working_template = unique_workday_template(calendar, field="working_windows")
    handoff_template = unique_workday_template(calendar, field="handoff_windows")
    overrides: list[dict[str, Any]] = []
    current = date(year, 1, 1)
    for state in states:
        default_working = daily_windows(calendar, current, field="working_windows")
        default_handoff = daily_windows(calendar, current, field="handoff_windows")
        if state == "1":
            if default_working or default_handoff:
                overrides.append(
                    {
                        "date": current.isoformat(),
                        "kind": "non_working",
                        "working_windows": [],
                        "handoff_windows": [],
                    }
                )
        elif state == "0":
            if not default_working or not default_handoff:
                overrides.append(
                    {
                        "date": current.isoformat(),
                        "kind": "working_transfer",
                        "working_windows": default_working or working_template,
                        "handoff_windows": default_handoff or handoff_template,
                    }
                )
        else:
            overrides.append(
                {
                    "date": current.isoformat(),
                    "kind": "shortened",
                    "working_windows": shorten_windows(
                        default_working or working_template, shortened_minutes
                    ),
                    "handoff_windows": shorten_windows(
                        default_handoff or handoff_template, shortened_minutes
                    ),
                }
            )
        current += timedelta(days=1)
    return overrides


def materialize(
    calendar: dict[str, Any],
    *,
    country: str,
    years: list[int],
    cache_dir: Path,
    offline: bool,
    refresh: bool,
    verify_xmlcalendar: bool,
    shortened_minutes: int,
) -> dict[str, Any]:
    work_metrics.validate_business_calendar(calendar)
    all_overrides: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for year in sorted(set(years)):
        primary_url = ISDAYOFF_URL.format(year=year, country=country)
        primary_raw = load_source(
            cache_dir=cache_dir,
            country=country,
            year=year,
            source="isdayoff",
            url=primary_url,
            offline=offline,
            refresh=refresh,
        )
        states = validate_isdayoff(year, primary_raw)
        source: dict[str, Any] = {
            "year": year,
            "url": primary_url,
            "sha256": sha256(states.encode("ascii")),
        }
        if verify_xmlcalendar:
            verifier_url = XMLCALENDAR_URL.format(year=year, country=country)
            verifier_raw = load_source(
                cache_dir=cache_dir,
                country=country,
                year=year,
                source="xmlcalendar",
                url=verifier_url,
                offline=offline,
                refresh=refresh,
            )
            verifier_states, source_date = xmlcalendar_states(year, verifier_raw)
            if verifier_states != states:
                mismatches = [
                    (date(year, 1, 1) + timedelta(days=index)).isoformat()
                    for index, pair in enumerate(zip(states, verifier_states, strict=True))
                    if pair[0] != pair[1]
                ]
                raise ProductionCalendarError(
                    "isdayoff and xmlcalendar disagree for " + ", ".join(mismatches[:10])
                )
            source["verification"] = {
                "provider": "xmlcalendar.ru",
                "url": verifier_url,
                "sha256": sha256(verifier_raw),
                "source_date": source_date,
                "matched": True,
            }
        sources.append(source)
        all_overrides.extend(
            year_overrides(
                calendar,
                year=year,
                states=states,
                shortened_minutes=shortened_minutes,
            )
        )
    result = dict(calendar)
    result["production_calendar"] = {
        "schema": 1,
        "provider": "isdayoff.ru",
        "country": country,
        "years": sorted(set(years)),
        "shortened_by_minutes": shortened_minutes,
        "sources": sources,
        "day_overrides": sorted(all_overrides, key=lambda item: item["date"]),
    }
    work_metrics.validate_business_calendar(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(prog="production_calendar.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("materialize")
    command.add_argument("--calendar", type=Path, required=True)
    command.add_argument("--country", default="ru")
    command.add_argument("--year", type=int, action="append", required=True)
    command.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    command.add_argument("--offline", action="store_true")
    command.add_argument("--refresh", action="store_true")
    command.add_argument("--skip-xmlcalendar-verification", action="store_true")
    command.add_argument("--shortened-minutes", type=int, default=60)
    command.add_argument("--write", type=Path)
    args = parser.parse_args()
    if args.offline and args.refresh:
        parser.error("--offline and --refresh cannot be combined")
    if args.shortened_minutes <= 0:
        parser.error("--shortened-minutes must be positive")
    try:
        calendar = work_metrics.read_json(args.calendar)
        result = materialize(
            calendar,
            country=args.country.strip().lower(),
            years=args.year,
            cache_dir=args.cache_dir,
            offline=args.offline,
            refresh=args.refresh,
            verify_xmlcalendar=not args.skip_xmlcalendar_verification,
            shortened_minutes=args.shortened_minutes,
        )
        if args.write:
            work_metrics.atomic_json(args.write, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ProductionCalendarError, work_metrics.WorkMetricsError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
