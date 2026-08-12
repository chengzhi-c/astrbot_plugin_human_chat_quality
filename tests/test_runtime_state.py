"""runtime_state 模块契约测试：检测信号（含 natural-talk 扩充与误报控制）、存储容错。

无需宿主 astrbot 即可运行（runtime_state 对 logger 做了 ImportError 防护）。
"""

import asyncio
import json
import os
import time
import unittest
from unittest import mock

import sys
from pathlib import Path

from tests._support import temporary_directory

# 插件目录本身即包根目录（astrbot_plugin_human_chat_quality），
# 需把仓库根目录的父目录加入 sys.path 才能以包方式导入（仓库可位于任意路径）
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from astrbot_plugin_human_chat_quality.runtime_state import (
    RuntimeStateStore,
    detect_cliches,
    extract_opener,
    group_id_from_event,
    is_session_disabled,
    match_keys,
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
        self.dir = temporary_directory(self)

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


class TestWindowThresholdBoundaries(unittest.TestCase):
    """C3：窗口与重复阈值的边界行为。"""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_window_three_exact_hit(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "w3.json"), 14, 3, ())
            for _ in range(3):
                await s.record_response("g", "好的，回答")
            self.assertIn("好的", s.get("g").avoid_openers)

        asyncio.run(run())

    def test_two_repeats_not_hit(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "w8.json"), 14, 8, ())
            for _ in range(2):
                await s.record_response("g", "好的，回答")
            self.assertEqual(s.get("g").avoid_openers, [])

        asyncio.run(run())

    def test_repeat_order_is_first_reach_order(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "ord.json"), 14, 8, ())
            # 两种开头交替出现；avoid_openers 按窗口扫描序（最近→最旧）取先达 3 次者
            for opener in ["好的", "可以", "好的", "可以", "好的", "可以"]:
                await s.record_response("g", opener + "，回答")
            self.assertEqual(s.get("g").avoid_openers, ["可以", "好的"])

        asyncio.run(run())


class TestReset(unittest.TestCase):
    """C9：reset 清空目标会话并持久化，不影响其它会话。"""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_reset_clears_target_session_only(self):
        async def run():
            p = os.path.join(self.dir, "rst.json")
            s = RuntimeStateStore(p, 14, 8, ())
            await s.record_response("g1", "好的，回答")
            await s.record_response("g2", "可以，回答")
            await s.reset("g1")
            self.assertNotIn("g1", s.sessions)
            self.assertIn("g2", s.sessions)
            s2 = RuntimeStateStore(p, 14, 8)
            self.assertNotIn("g1", s2.sessions)
            self.assertIn("g2", s2.sessions)

        asyncio.run(run())


class TestPruneExpired(unittest.TestCase):
    """C9：过期会话按 retention 清理（时间轴经 _now 注入）。"""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_expired_removed(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "pr.json"), 7, 8, ())
            await s.record_response("old", "好的，回答")
            with mock.patch(
                "astrbot_plugin_human_chat_quality.runtime_state._now",
                return_value=time.time() + 8 * 86400,
            ):
                s._prune_expired()
            self.assertEqual(s.sessions, {})

        asyncio.run(run())

    def test_fresh_within_retention_kept(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "pr2.json"), 7, 8, ())
            await s.record_response("fresh", "好的，回答")
            s._prune_expired()
            self.assertIn("fresh", s.sessions)

        asyncio.run(run())


class TestBackupRotation(unittest.TestCase):
    """C9：损坏备份按 mtime 轮转，只保留最近 5 份。"""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_backup_capped_at_five(self):
        p = os.path.join(self.dir, "rot.json")
        for i in range(6):
            bp = os.path.join(self.dir, f"rot.corrupt.2026010{i + 1}-000000-{1000 + i}.json")
            with open(bp, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(bp, (1000 + i, 1000 + i))
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken json")
        RuntimeStateStore(p, 14, 8)
        backups = [n for n in os.listdir(self.dir) if "rot.corrupt" in n]
        self.assertEqual(len(backups), 5)


class TestSaveFailureIsolation(unittest.TestCase):
    """C12（Store 层）：写盘失败不向调用方传播，内存状态已更新。"""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_write_failure_swallowed(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "sf.json"), 14, 8, ())
            with mock.patch.object(s, "_write_snapshot_sync", side_effect=OSError("disk full")):
                await s.record_response("g", "好的，回答")  # 不应抛出
            self.assertIn("g", s.sessions)

        asyncio.run(run())


class TestThreadedSave(unittest.TestCase):
    """C10：状态写盘经 asyncio.to_thread 在工作线程执行。"""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_save_runs_in_thread_pool(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "th.json"), 14, 8, ())
            real = asyncio.to_thread
            with mock.patch(
                "astrbot_plugin_human_chat_quality.runtime_state.asyncio.to_thread",
                new=mock.AsyncMock(wraps=real),
            ) as m:
                await s.record_response("g", "好的，回答")
            m.assert_awaited_once()
            # bound method 每次访问是新对象，用相等断言（同函数同实例即相等）
            self.assertEqual(m.await_args.args[0], s._write_snapshot_sync)

        asyncio.run(run())


class TestDisabledMatch(unittest.TestCase):
    """C8：disabled_sessions 匹配形态（origin 全串/群号/前缀/# base/大小写）。"""

    def test_match_keys_all_shapes(self):
        keys = match_keys("aiocqhttp:GroupMessage:222", "222")
        self.assertIn("aiocqhttp:groupmessage:222", keys)
        self.assertIn("222", keys)
        self.assertIn("group:222", keys)
        self.assertIn("groupmessage:222", keys)

    def test_match_keys_base_split_for_topic(self):
        # Telegram topic 群：会话号形如 222#thread
        keys = match_keys("telegram:GroupMessage:222#5", "222#5")
        self.assertIn("222#5", keys)
        self.assertIn("222", keys)
        self.assertIn("group:222", keys)

    def test_match_keys_case_insensitive(self):
        keys = match_keys("AIOCQHTTP:GROUPMESSAGE:222", "222")
        self.assertIn("aiocqhttp:groupmessage:222", keys)
        self.assertIn("group:222", keys)

    def test_match_keys_empty(self):
        self.assertEqual(match_keys("", ""), frozenset())

    def test_group_id_from_event_getter_first(self):
        class Ev:
            unified_msg_origin = "aiocqhttp:GroupMessage:111"

            def get_group_id(self):
                return "222"

        self.assertEqual(group_id_from_event(Ev()), "222")

    def test_group_id_from_event_getter_exception_falls_back(self):
        class Ev:
            unified_msg_origin = "aiocqhttp:GroupMessage:111"

            def get_group_id(self):
                raise RuntimeError("boom")

        self.assertEqual(group_id_from_event(Ev()), "111")

    def test_group_id_from_event_origin_fallback(self):
        class Ev:
            unified_msg_origin = "aiocqhttp:GroupMessage:333"

        self.assertEqual(group_id_from_event(Ev()), "333")

    def test_group_id_from_event_private_message_empty(self):
        class Ev:
            unified_msg_origin = "aiocqhttp:PrivateMessage:444"

        self.assertEqual(group_id_from_event(Ev()), "")

    def test_is_session_disabled_event_none_uses_origin(self):
        self.assertTrue(is_session_disabled(frozenset({"222"}), "aiocqhttp:GroupMessage:222", None))
        self.assertFalse(is_session_disabled(frozenset({"333"}), "aiocqhttp:GroupMessage:222", None))

    def test_is_session_disabled_event_group_id_wins(self):
        class Ev:
            unified_msg_origin = "aiocqhttp:GroupMessage:222"

            def get_group_id(self):
                return "333"

        self.assertTrue(is_session_disabled(frozenset({"333"}), "aiocqhttp:GroupMessage:222", Ev()))
        self.assertFalse(is_session_disabled(frozenset({"222"}), "aiocqhttp:GroupMessage:222", Ev()))

    def test_is_session_disabled_empty(self):
        self.assertFalse(is_session_disabled(frozenset(), "aiocqhttp:GroupMessage:222", None))


if __name__ == "__main__":
    unittest.main()
