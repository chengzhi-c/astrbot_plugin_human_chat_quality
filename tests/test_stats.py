"""QualityStats contracts (process-local counters)."""

import unittest

from tests._support import ensure_plugin_package

ensure_plugin_package()

from astrbot_plugin_human_chat_quality.core import QualityStats


class TestQualityStats(unittest.TestCase):
    def test_top_cliches_sort_ties_stably(self):
        stats = QualityStats(cliche_hits={"zeta": 2, "alpha": 2, "middle": 1})
        self.assertEqual(stats.top_cliches(3), [("alpha", 2), ("zeta", 2), ("middle", 1)])


if __name__ == "__main__":
    unittest.main()
