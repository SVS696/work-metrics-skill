from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import review_yield


class ReviewYieldTests(unittest.TestCase):
    def test_aggregate_separates_verified_duplicate_and_unclassified_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent-ledger.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "case_id": "case-1",
                        "runs": [
                            {
                                "role": "spec-reviewer",
                                "role_mode": "final",
                                "model": "m1",
                                "status": "completed",
                                "lenses": ["logic@1", "project@2"],
                                "duration_seconds": 10,
                                "retries": 0,
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "findings": {"blocker": 0, "major": 2, "minor": 1},
                                "verification": {
                                    "dispositions": {
                                        "accepted": 2,
                                        "rejected": 0,
                                        "duplicate": 1,
                                        "verified": 1,
                                    }
                                },
                            },
                            {
                                "role": "spec-reviewer",
                                "role_mode": "final",
                                "model": "m1",
                                "status": "degraded",
                                "lenses": [],
                                "duration_seconds": 5,
                                "retries": 1,
                                "input_tokens": None,
                                "output_tokens": None,
                                "findings": {"blocker": 0, "major": 0, "minor": 0},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = review_yield.aggregate([path])
            self.assertEqual(result["totals"]["runs"], 2)
            self.assertEqual(result["totals"]["duplicate"], 1)
            self.assertEqual(result["totals"]["verified"], 1)
            self.assertEqual(result["totals"]["unclassified_runs"], 1)
            self.assertEqual(result["totals"]["duplicate_per_reported"], 0.3333)
            self.assertEqual(result["by"]["lens"]["logic@1"]["runs"], 1)
            self.assertEqual(result["by"]["lens"]["unversioned"]["runs"], 1)


if __name__ == "__main__":
    unittest.main()
