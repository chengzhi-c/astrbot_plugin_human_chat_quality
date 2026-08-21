import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests._support import ensure_plugin_package
from scripts import eval_detector

ensure_plugin_package()


class TestDetectorEvaluation(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).resolve().parents[1]
        self.fixture = self.repo / "tests" / "fixtures" / "detector_eval.json"

    def test_fixture_is_frozen_and_covers_required_categories(self):
        rows = json.loads(self.fixture.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(rows), 120)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual({row["split"] for row in rows}, {"dev", "holdout"})
        self.assertGreaterEqual(len([row for row in rows if row["split"] == "holdout"]), 40)
        categories = {row["category"] for row in rows}
        required = {"casual", "tech", "steps", "emotion", "formal", "identity", "uncertainty", "role-dialogue"}
        self.assertGreaterEqual(categories, required)
        for split in ("dev", "holdout"):
            self.assertGreaterEqual({row["category"] for row in rows if row["split"] == split}, required)
            formal = [row for row in rows if row["split"] == split and row["category"] == "formal"]
            self.assertGreaterEqual(len([row for row in formal if row["formal_bypass"]]), 7)
            self.assertGreaterEqual(len([row for row in formal if not row["formal_bypass"]]), 7)
        for row in rows:
            self.assertTrue(row["id"])
            self.assertIsInstance(row["user"], str)
            self.assertIsInstance(row["answer"], str)
            self.assertIsInstance(row["expected_signals"], list)
            self.assertIsInstance(row["formal_bypass"], bool)
            self.assertTrue(row["rationale"])

    def test_eval_script_reports_category_metrics_for_dev_and_holdout(self):
        result = subprocess.run(
            [sys.executable, "-S", "scripts/eval_detector.py"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(set(report), {"dev", "holdout"})
        for split in report.values():
            self.assertTrue(split["categories"])
            self.assertEqual(set(split["formal_bypass"]), {"precision", "recall", "fp", "fn"})
            self.assertEqual((split["formal_bypass"]["fp"], split["formal_bypass"]["fn"]), (0, 0))
            for metrics in split["categories"].values():
                self.assertEqual(set(metrics), {"precision", "recall", "fp", "fn"})
                self.assertEqual((metrics["fp"], metrics["fn"]), (0, 0))

    def test_check_mode_rejects_any_false_positive_or_negative(self):
        report = {
            "dev": {"categories": {"casual": {"fp": 0, "fn": 0}}, "formal_bypass": {"fp": 0, "fn": 0}},
            "holdout": {
                "categories": {"casual": {"fp": 1, "fn": 0}},
                "formal_bypass": {"fp": 0, "fn": 0},
            },
        }
        self.assertTrue(eval_detector.has_errors(report))


if __name__ == "__main__":
    unittest.main()
