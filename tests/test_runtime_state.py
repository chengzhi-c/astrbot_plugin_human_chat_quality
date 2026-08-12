"""runtime_state 模块契约测试：检测信号（含 natural-talk 扩充与误报控制）、存储容错。

无需宿主 astrbot 即可运行（runtime_state 对 logger 做了 ImportError 防护）。
"""

import json
import os
import tempfile
import unittest

import sys
from pathlib import Path

# 插件目录本身即包根目录（astrbot_plugin_human_chat_quality），
# 需把仓库根目录的父目录加入 sys.path 才能以包方式导入（仓库可位于任意路径）
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from astrbot_plugin_human_chat_quality.runtime_state import (
    RuntimeStateStore,
    detect_cliches,
    extract_opener,
    repeated_items,
)


class TestDetectClichesNaturalTalk(unittest.TestCase):
    """natural-talk 信号：AI 自我暴露任意位置命中、开场仅首部命中、收尾仅结尾命中、路标词 ≥3 次。"""

    def test_ai_self_exposure_any_position(self):
        self.assertIn("作为AI", detect_cliches("作为AI，我需要指出一点"))
        self.assertIn("作为AI", detect_cliches("这里补充一点：作为AI我不能承诺。"))
        self.assertIn("根据我的训练", detect_cliches("根据我的训练数据，这通常可行"))
        self.assertIn("截至我的知识更新", detect_cliches("截至我的知识更新，该版本尚未发布"))

    def test_opening_cliches_first_clause_only(self):
        self.assertIn("好问题", detect_cliches("好问题，让我查一下"))
        self.assertIn("让我来", detect_cliches("让我来帮你看看"))
        self.assertIn("感谢你的提问", detect_cliches("感谢你的提问，我说明一下"))
        # 句中不报
        self.assertNotIn("好问题", detect_cliches("这个问题确实是个好问题"))
        self.assertNotIn("让我来", detect_cliches("你先别急，让我来试试看再说"))

    def test_opening_fp_control_human_phrases(self):
        """误报控制：人话开场不受影响。"""
        self.assertEqual(detect_cliches("感谢你的建议，我会试试"), [])
        self.assertEqual(detect_cliches("感谢你的耐心，我先整理下"), [])
        self.assertEqual(detect_cliches("好，我来确认一下再答复你"), [])

    def test_summary_endings_tail_only(self):
        self.assertIn("综上所述", detect_cliches("情况就是这样，综上所述。"))
        self.assertIn("由此可见", detect_cliches("测试全部通过，由此可见。"))
        # 句中不报（“由此可见方案可行”是句中用法，按设计不拦截）
        self.assertNotIn("由此可见", detect_cliches("测试全部通过，由此可见方案可行。"))
        self.assertNotIn("综上所述", detect_cliches("综上所述的问题先放一边，我们看下一个"))

    def test_road_signs_three_plus(self):
        self.assertIn("路标词堆砌", detect_cliches("事实上这样。实际上那样。换句话说都不行。"))
        self.assertNotIn("路标词堆砌", detect_cliches("事实上这样。实际上那样。"))
        self.assertIn("路标词堆砌", detect_cliches("本质上如此。归根结底如此。与此同时还有。"))

    def test_road_signs_single_use_clear(self):
        self.assertEqual(detect_cliches("事实上这是个好办法"), [])


class TestDetectClichesLegacy(unittest.TestCase):
    """回归：既有高置信度信号不受扩充影响。"""

    def test_ending_hit_tail_only(self):
        self.assertIn("希望对你有帮助", detect_cliches("这是正文，希望对你有帮助。"))
        self.assertEqual(detect_cliches("希望对你有帮助，然后我们继续。"), [])

    def test_structure_consecutive(self):
        self.assertIn("破折号连发", detect_cliches("a——b——c"))
        self.assertEqual(detect_cliches("a——b"), [])
        self.assertIn("然而连发", detect_cliches("然而a然而b"))
        self.assertEqual(detect_cliches("然而一次"), [])

    def test_custom_cliches_any_position(self):
        self.assertEqual(detect_cliches("中间出现自定义词", ("自定义词",)), ["自定义词"])
        # 去重、保序
        hits = detect_cliches("好问题。自定义词。事实上。事实上。事实上。", ("自定义词",))
        self.assertEqual(hits[0], "好问题")
        self.assertIn("自定义词", hits)
        self.assertIn("路标词堆砌", hits)


class TestOpener(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(extract_opener("好的，我来帮你"), "好的")

    def test_split_and_truncate(self):
        self.assertEqual(extract_opener("今天天气真不错，适合出门"), "今天天气真不错")

    def test_single_char_skipped_and_empty(self):
        self.assertNotEqual(extract_opener("嗯。好的吧"), "嗯")
        self.assertEqual(extract_opener(""), "")


class TestRepeatedItems(unittest.TestCase):
    def test_threshold_and_order(self):
        self.assertEqual(repeated_items(["好的", "好的", "好的", "好的"], 5), ["好的"])
        self.assertEqual(repeated_items(["a", "b", "a", "b", "a", "b"], 5), ["a", "b"])
        self.assertEqual(repeated_items(["a", "a"], 5), [])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _path(self, name="state.json"):
        return os.path.join(self.dir, name)

    def test_custom_cliches_dedup_and_filter(self):
        s = RuntimeStateStore(self._path(), 14, 8, ["自定义词", "自定义词", "x" * 21])
        self.assertEqual(s.custom_cliches, ("自定义词",))

    def test_record_and_roundtrip(self):
        async def run():
            s = RuntimeStateStore(self._path(), 14, 8, ("自定义词",))
            await s.record_response("g1", "好的，回答一")
            await s.record_response("g1", "好的，回答二（自定义词）")
            st = s.get("g1")
            # 信号只带进下一轮提示（avoid_openers 每轮重算，README 契约）
            self.assertIn("自定义词", st.avoid_openers)
            self.assertLessEqual(len(st.avoid_openers), 5)
            # 持久化往返
            s2 = RuntimeStateStore(self._path(), 14, 8)
            self.assertIn("自定义词", s2.get("g1").avoid_openers)
            # 重复开头达阈值进入清单
            await s.record_response("g1", "好的，回答三")
            self.assertIn("好的", s.get("g1").avoid_openers)

        import asyncio

        asyncio.run(run())

    def test_corrupt_top_level_backup_and_reset(self):
        with open(self._path(), "w", encoding="utf-8") as f:
            f.write("{broken json")
        s = RuntimeStateStore(self._path(), 14, 8)
        self.assertEqual(s.sessions, {})
        self.assertTrue(any("corrupt" in fn for fn in os.listdir(self.dir)))

    def test_entry_level_tolerant(self):
        p = self._path("s2.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"sessions": {"good": {"avoid_openers": ["a"]}, "bad": "notdict"}}, f)
        s = RuntimeStateStore(p, 14, 8)
        self.assertIn("good", s.sessions)
        self.assertNotIn("bad", s.sessions)

    def test_runtime_disabled_persisted(self):
        async def run():
            p = self._path("s3.json")
            s = RuntimeStateStore(p, 14, 8)
            await s.set_enabled("g1", False)
            await s.set_enabled("g1", False)  # 幂等：不报错
            self.assertFalse(s.is_enabled("g1"))
            s2 = RuntimeStateStore(p, 14, 8)
            self.assertFalse(s2.is_enabled("g1"))

        import asyncio

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
