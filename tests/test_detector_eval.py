import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from scripts import eval_detector
from tests._support import ensure_plugin_package

ensure_plugin_package()


class TestDetectorEvaluation(unittest.TestCase):
    def setUp(self):
        self.repo = Path(__file__).resolve().parents[1]
        self.fixture = self.repo / "tests" / "fixtures" / "detector_eval.json"

    def test_fixture_is_frozen_and_covers_required_categories(self):
        """契约锁：fixture 与实现同源演进，此测试锁"实现未变 + 覆盖面下限"，不是泛化证据。

        新增样例须逐条可解释（rationale 写清判据），独立泛化验证靠人工标注的 holdout。
        """
        rows = json.loads(self.fixture.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(rows), 120)
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual({row["split"] for row in rows}, {"dev", "holdout"})
        holdout = [row for row in rows if row["split"] == "holdout"]
        self.assertGreaterEqual(len(holdout), 40)
        # split 不得塌缩成开发集独大：holdout 至少占三分之一
        self.assertGreaterEqual(len(holdout) * 3, len(rows))
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
        self.assertEqual(set(report), {"dev", "holdout", "uncovered_signals"})
        self.assertEqual(report["uncovered_signals"], [])
        for name in ("dev", "holdout"):
            split = report[name]
            self.assertTrue(split["categories"])
            self.assertEqual(set(split["formal_bypass"]), {"precision", "recall", "fp", "fn", "n"})
            self.assertEqual((split["formal_bypass"]["fp"], split["formal_bypass"]["fn"]), (0, 0))
            for metrics in split["categories"].values():
                self.assertEqual(set(metrics), {"precision", "recall", "fp", "fn", "n"})
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

    def test_check_mode_rejects_uncovered_builtin_names(self):
        self.assertTrue(eval_detector.has_errors({"dev": {}, "holdout": {}}, ["作为AI"]))
        self.assertFalse(
            eval_detector.has_errors(
                {
                    "dev": {"categories": {"casual": {"fp": 0, "fn": 0}}, "formal_bypass": {"fp": 0, "fn": 0}},
                    "holdout": {"categories": {"casual": {"fp": 0, "fn": 0}}, "formal_bypass": {"fp": 0, "fn": 0}},
                }
            )
        )

    def test_opening_negative_exemption_is_locked_by_reverse_failure(self):
        from astrbot_plugin_human_chat_quality import signal_detectors as sd

        text = "让我们先来点音乐吧，把气氛热一下。"
        self.assertEqual(sd.detect_cliches(text), [])
        with mock.patch.object(sd, "_OPENING_NEGATIVE_EXACT", frozenset()):
            self.assertIn("让我们先来", sd.detect_cliches(text))


if __name__ == "__main__":
    unittest.main()
