#!/usr/bin/env python3
"""Install Work Metrics discovery links after a mutation-free preflight."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SKILL_ROOT = Path(__file__).resolve().parent.parent


class InstallerError(RuntimeError):
    """Unsafe discovery-link state."""


@dataclass(frozen=True)
class LinkState:
    source: Path
    target: Path
    status: str


def targets(user_home: Path) -> list[Path]:
    return [
        user_home / ".agents" / "skills" / "work-metrics",
        user_home / ".claude" / "skills" / "work-metrics",
    ]


def inspect(skill_root: Path, user_home: Path) -> list[LinkState]:
    source = skill_root.expanduser().resolve()
    states: list[LinkState] = []
    for target in targets(user_home.expanduser().resolve()):
        if not source.exists():
            status = "source-missing"
        elif target.is_symlink() and target.resolve(strict=False) == source:
            status = "installed"
        elif target.exists() or target.is_symlink():
            status = "conflict"
        else:
            status = "missing"
        states.append(LinkState(source, target, status))
    return states


def install(
    skill_root: Path, user_home: Path, *, dry_run: bool = False
) -> list[LinkState]:
    states = inspect(skill_root, user_home)
    blockers = [item for item in states if item.status in {"source-missing", "conflict"}]
    if blockers:
        raise InstallerError(
            "Preflight failed; no links were changed: "
            + ", ".join(f"{item.status} {item.target}" for item in blockers)
        )
    if dry_run:
        return states
    for item in states:
        if item.status != "missing":
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.symlink_to(item.source, target_is_directory=True)
    verified = inspect(skill_root, user_home)
    if any(item.status != "installed" for item in verified):
        raise InstallerError("Post-install discovery verification failed")
    return verified


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        states = (
            inspect(args.skill_root, args.home)
            if args.check
            else install(args.skill_root, args.home, dry_run=args.dry_run)
        )
        for item in states:
            print(f"{item.status}\t{item.target}\t{item.source}")
        if args.check and any(item.status != "installed" for item in states):
            return 1
        return 0
    except (OSError, InstallerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
