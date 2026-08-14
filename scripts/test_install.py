#!/usr/bin/env python3
"""Regression tests for Work Metrics discovery installation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import install


class InstallerTests(unittest.TestCase):
    def test_install_is_complete_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            first = install.install(install.DEFAULT_SKILL_ROOT, home)
            second = install.install(install.DEFAULT_SKILL_ROOT, home)
            self.assertTrue(all(item.status == "installed" for item in first))
            self.assertTrue(all(item.status == "installed" for item in second))

    def test_conflict_aborts_before_creating_any_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            conflict = home / ".agents" / "skills" / "work-metrics"
            conflict.parent.mkdir(parents=True)
            conflict.write_text("user-owned", encoding="utf-8")
            with self.assertRaises(install.InstallerError):
                install.install(install.DEFAULT_SKILL_ROOT, home)
            self.assertFalse((home / ".claude" / "skills" / "work-metrics").exists())


if __name__ == "__main__":
    unittest.main()
