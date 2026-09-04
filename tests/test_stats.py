"""QualityStats contracts (process-local counters)."""

import unittest

from tests._support import ensure_plugin_package

ensure_plugin_package()

from astrbot_plugin_human_chat_quality.core import QualityStats


class TestQualityStats(unittest.TestCase):
    def test_top_cliches_sort_ties_stably(self):
        stats = QualityStats(cliche_hits={"zeta": 2, "alpha": 2, "middle": 1})
        self.assertEqual(stats.top_cliches(3), [("alpha", 2), ("zeta", 2), ("middle", 1)])

    def test_record_request_counts_matrix(self):
        stats = QualityStats()
        stats.record_request(True, False)
        stats.record_request(False, True)
        stats.record_request(True, True)
        stats.record_request(False, False)
        self.assertEqual(stats.stable_rules_injected, 2)
        self.assertEqual(stats.runtime_hints_injected, 2)
        self.assertEqual(stats.total_injections, 3)

    def test_record_cliche_hit_accumulates(self):
        stats = QualityStats()
        stats.record_cliche_hit("好的")
        stats.record_cliche_hit("好的")
        self.assertEqual(stats.cliche_hits, {"好的": 2})
        self.assertEqual(stats.top_cliches(5), [("好的", 2)])


if __name__ == "__main__":
    unittest.main()
