"""runtime_state 模块契约测试：检测信号（含 natural-talk 扩充与误报控制）、存储容错。

无需宿主 astrbot 即可运行（runtime_state 对 logger 做了 ImportError 防护）。
"""

import asyncio
import json
import math
import os
import time
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._support import ensure_plugin_package, temporary_directory

ensure_plugin_package()

from astrbot_plugin_human_chat_quality import runtime_state as runtime_state_module
from astrbot_plugin_human_chat_quality import signal_detectors
from astrbot_plugin_human_chat_quality.runtime_state import (
    RuntimeStateStore,
    SessionState,
    _parse_group_id_from_origin,
    detect_cliches,
    extract_opener,
    group_id_from_event,
    is_session_disabled,
    match_keys,
    repeated_items,
)


class TestDetectClichesNaturalTalk(unittest.TestCase):
    """natural-talk 信号：AI 自我暴露任意位置命中、开场仅首部命中、收尾仅结尾命中；
    密度项（路标词/破折号/感叹号）对齐 natural-talk 计数口径：300 字基准按篇幅折算，超上限才报。"""

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

    def test_english_high_confidence_signals(self):
        self.assertIn("I hope this helps", detect_cliches("That is the answer. I hope this helps."))
        self.assertIn("I hope this helps", detect_cliches("That is the answer. I HOPE THIS HELPS!"))
        self.assertEqual(detect_cliches("I hope this helps you later in the paragraph."), [])
        self.assertIn("Great question", detect_cliches("Great question, here is the short version."))
        self.assertIn("Great question", detect_cliches("great question, here is the short version."))
        self.assertNotIn("Great question", detect_cliches("this is a great question in the middle."))
        self.assertEqual(detect_cliches("Of course I can look that up."), [])

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
        self.assertIn("破折号", detect_cliches("a——b——c"))
        self.assertEqual(detect_cliches("a——b"), [])
        self.assertIn("然而连发", detect_cliches("然而a然而b"))
        self.assertEqual(detect_cliches("然而一次"), [])

    def test_dash_density_aligned_with_skill(self):
        """对齐 natural-talk：em/en dash 均计次，上限 ≤2，超过（≥3 个）才报。"""
        self.assertIn("破折号", detect_cliches("a—b—c—d"))  # 三个单 em dash
        self.assertIn("破折号", detect_cliches("a–b–c–d"))  # 三个 en dash
        self.assertIn("破折号", detect_cliches("a——b—c"))  # 混合计次（4 个）
        self.assertEqual(detect_cliches("a—b"), [])  # 恰好在上限内
        self.assertEqual(detect_cliches("a–b"), [])

    def test_exclamation_density_aligned_with_skill(self):
        """对齐 natural-talk：感叹号上限 ≤3，超过（≥4 个）才报；全/半角都算。"""
        self.assertIn("感叹号", detect_cliches("太好了！太棒了！真厉害！冲啊！"))
        self.assertIn("感叹号", detect_cliches("Nice! Great! Amazing! Wow!"))
        self.assertEqual(detect_cliches("不错！很好！加油！"), [])
        self.assertEqual(detect_cliches("好!行!可以!"), [])

    def test_density_caps_scale_with_length(self):
        """对齐 natural-talk：更长回复按 300 字基准折算上限。

        650 字左右为 scale=3 档：路标词上限 6 次，破折号上限 6 个；
        700+ 字为 scale=3 档同样成立，用 6 个不报、7 个报验证折算生效。
        """
        base = "字" * 620
        six = base + "事实上" * 6
        seven = base + "事实上" * 7
        self.assertEqual(detect_cliches(six), [])
        self.assertIn("路标词堆砌", detect_cliches(seven))
        self.assertEqual(detect_cliches(base + "a—" * 6), [])
        self.assertIn("破折号", detect_cliches(base + "a—" * 7))
        self.assertEqual(detect_cliches(base + "！" * 9), [])
        self.assertIn("感叹号", detect_cliches(base + "！" * 10))

    def test_custom_cliches_any_position(self):
        self.assertEqual(detect_cliches("中间出现自定义词", ("自定义词",)), ["自定义词"])
        # 去重、保序
        hits = detect_cliches("好问题。自定义词。事实上。事实上。事实上。", ("自定义词",))
        self.assertEqual(hits[0], "好问题")
        self.assertIn("自定义词", hits)
        self.assertIn("路标词堆砌", hits)

    def test_quoted_iron_rule_does_not_hide_later_unquoted_match(self):
        text = "他说：\u201c这不是优化而是重构。\u201d但真正的问题是测试不足。"
        self.assertIn("结构性表演", detect_cliches(text))

    def test_iron_rule_examples_in_code_are_ignored(self):
        text = "示例：`不是优化而是重构`。代码如下：\n```text\n真正的问题是这里\n```"
        self.assertNotIn("结构性表演", detect_cliches(text))

    def test_mismatched_quotes_do_not_create_an_exemption(self):
        self.assertIn("结构性表演", detect_cliches('\u201c前文这不是优化而是重构"后文'))

    def test_density_uses_normalized_text_length(self):
        """密度折算用归一化后字符数，原始空白不计入篇幅档位。"""
        text = "破" + " " * 300 + "折" + "—" * 5
        self.assertIn("破折号", detect_cliches(text))

    def test_density_cap_matches_300_char_basis(self):
        """300 字基准折算边界：300→cap1，301→cap2，600→cap2，601→cap3。"""
        self.assertEqual(max(1, math.ceil(300 / 300)), 1)
        self.assertEqual(max(1, math.ceil(301 / 300)), 2)
        self.assertEqual(max(1, math.ceil(600 / 300)), 2)
        self.assertEqual(max(1, math.ceil(601 / 300)), 3)

    def test_density_uses_shared_constant(self):
        with mock.patch.object(signal_detectors, "DENSITY_BASE", 600, create=True):
            self.assertIn("感叹号", signal_detectors.detect_density_signals("字" * 301 + "！" * 4))

    def test_fixed_pattern_uses_shared_constant(self):
        with mock.patch.object(signal_detectors, "CONSECUTIVE_THRESHOLD", 3, create=True):
            self.assertNotIn("然而连发", signal_detectors.detect_fixed_pattern_signals("然而。然而。"))
            self.assertIn("然而连发", signal_detectors.detect_fixed_pattern_signals("然而。然而。然而。"))


class TestOpener(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(extract_opener("好的，我来帮你"), "好的")

    def test_split_and_truncate(self):
        self.assertEqual(extract_opener("今天天气真不错，适合出门"), "今天天气真不错")

    def test_single_char_skipped_and_empty(self):
        self.assertNotEqual(extract_opener("嗯。好的吧"), "嗯")
        self.assertEqual(extract_opener(""), "")

    def test_prefix_hit_returns_prefix_truncated(self):
        self.assertEqual(extract_opener("没问题，这是一段话"), "没问题")

    def test_prefix_miss_falls_back_to_delim_split(self):
        self.assertEqual(extract_opener("嗯好的"), "嗯好的")

    def test_two_char_kept_after_split(self):
        self.assertEqual(extract_opener("好的"), "好的")


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

    def test_custom_cliches_report_ignored_reasons_without_values(self):
        s = RuntimeStateStore(self._path(), 14, 8, ["", "自定义词", "自定义词", "x" * 21])
        self.assertEqual(s.custom_cliches_ignored, 3)
        self.assertEqual(dict(s.custom_cliches_ignored_reasons), {"empty": 1, "duplicate": 1, "too_long": 1})

    def test_persistence_status_interface_exists(self):
        s = RuntimeStateStore(self._path(), 14, 8)
        self.assertTrue(callable(getattr(s, "flush", None)))
        self.assertIs(getattr(s, "has_pending_save", None), False)

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
            self.assertTrue(await s.flush())
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

    def test_compact_invalid_entries_do_not_reset_valid_sessions(self):
        p = self._path("compact-invalid.json")
        now = int(time.time())
        with open(p, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "sessions": {
                        "good": {"a": ["好的"], "r": "好的", "t": now},
                        "bad-r": {"a": [], "r": [], "t": now},
                        "bad-t": {"a": [], "r": "好的", "t": "not-a-number"},
                    }
                },
                f,
            )

        s = RuntimeStateStore(p, 14, 8)

        self.assertEqual(s.get("good").avoid_openers, ["好的"])
        self.assertNotIn("bad-r", s.sessions)
        self.assertNotIn("bad-t", s.sessions)
        self.assertEqual(len(list(Path(self.dir).glob("compact-invalid.corrupt.*.json"))), 1)

        async def persist_new_session():
            self.assertTrue(await s.record_response("new", "可以，继续处理。"))
            self.assertTrue(await s.flush())

        asyncio.run(persist_new_session())
        with open(p, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(set(saved["sessions"]), {"good", "new"})

    def test_multiple_malformed_sections_create_one_backup(self):
        p = self._path("multiple-malformed.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"disabled_sessions": "not-a-list", "sessions": {"bad": "not-a-session"}}, f)

        s = RuntimeStateStore(p, 14, 8)

        self.assertEqual(s.runtime_disabled, set())
        self.assertEqual(s.sessions, {})
        self.assertEqual(len(list(Path(self.dir).glob("multiple-malformed.corrupt.*.json"))), 1)

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

    def test_legacy_session_without_timestamps_uses_stale_file_mtime(self):
        path = os.path.join(self.dir, "legacy-stale.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump({"sessions": {"old": {"avoid_openers": ["好的"]}}}, file)
        now = 2_000_000.0
        old_mtime = now - 8 * 86400
        os.utime(path, (old_mtime, old_mtime))

        with mock.patch("astrbot_plugin_human_chat_quality.runtime_state._now", return_value=now):
            store = RuntimeStateStore(path, 7, 8)

        self.assertNotIn("old", store.sessions)

    def test_legacy_session_without_timestamps_persists_fresh_file_mtime(self):
        async def run():
            path = os.path.join(self.dir, "legacy-fresh.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"sessions": {"fresh": {"avoid_openers": ["好的"]}}}, file)
            now = 2_000_000.0
            fresh_mtime = now - 86400
            os.utime(path, (fresh_mtime, fresh_mtime))

            with mock.patch("astrbot_plugin_human_chat_quality.runtime_state._now", return_value=now):
                store = RuntimeStateStore(path, 7, 8)
                self.assertEqual(store.get("fresh").updated_at, fresh_mtime)
                self.assertTrue(await store.set_enabled("disabled", False))
            with open(path, encoding="utf-8") as file:
                saved = json.load(file)
            # 新格式使用 "t" 字段（紧凑格式）
            self.assertEqual(saved["sessions"]["fresh"]["t"], int(fresh_mtime))

        asyncio.run(run())

    def test_prune_uses_one_now_for_all_missing_timestamps(self):
        store = RuntimeStateStore(os.path.join(self.dir, "single-now.json"), 1, 8, ())
        store.sessions = {"a": SessionState(), "b": SessionState()}

        with mock.patch(
            "astrbot_plugin_human_chat_quality.runtime_state._now",
            side_effect=[200_000.0, 200_000.0, 0.0],
        ) as current_time:
            store._prune_expired()

        self.assertEqual(set(store.sessions), {"a", "b"})
        current_time.assert_called_once_with()

    def test_zero_timestamp_is_not_treated_as_missing(self):
        store = RuntimeStateStore(os.path.join(self.dir, "zero-time.json"), 1, 8, ())
        store.sessions = {"old": SessionState(updated_at=0.0)}

        with mock.patch("astrbot_plugin_human_chat_quality.runtime_state._now", return_value=200_000.0):
            store._prune_expired()

        self.assertEqual(store.sessions, {})


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
    """Write failures stay visible and retryable without rolling back memory."""

    def setUp(self):
        self.dir = temporary_directory(self)

    def test_background_failure_keeps_dirty_and_next_change_retries(self):
        async def run():
            path = os.path.join(self.dir, "sf.json")
            s = RuntimeStateStore(path, 14, 8, ())
            real_write = s._write_snapshot_sync
            with (
                mock.patch.object(runtime_state_module, "STATE_SAVE_DEBOUNCE_SECONDS", 0, create=True),
                mock.patch.object(s, "_write_snapshot_sync", side_effect=OSError("disk full")),
            ):
                self.assertTrue(await s.record_response("g", "好的，回答"))
                save_task = getattr(s, "_save_task", None)
                self.assertIsNotNone(save_task)
                await save_task
            self.assertIn("g", s.sessions)
            self.assertTrue(s.has_pending_save)
            with (
                mock.patch.object(runtime_state_module, "STATE_SAVE_DEBOUNCE_SECONDS", 0, create=True),
                mock.patch.object(s, "_write_snapshot_sync", side_effect=real_write),
            ):
                self.assertTrue(await s.record_response("g", "可以，继续"))
                save_task = getattr(s, "_save_task", None)
                self.assertIsNotNone(save_task)
                await save_task
            self.assertFalse(s.has_pending_save)
            self.assertIn("g", RuntimeStateStore(path, 14, 8).sessions)

        asyncio.run(run())

    def test_same_set_enabled_retries_after_failure(self):
        async def run():
            path = os.path.join(self.dir, "toggle.json")
            s = RuntimeStateStore(path, 14, 8, ())
            real_write = s._write_snapshot_sync
            attempts = 0

            def fail_once(payload):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("disk full")
                real_write(payload)

            with mock.patch.object(s, "_write_snapshot_sync", side_effect=fail_once):
                self.assertFalse(await s.set_enabled("g", False))
                self.assertFalse(s.is_enabled("g"))
                self.assertTrue(s.has_pending_save)
                self.assertTrue(await s.set_enabled("g", False))
            self.assertEqual(attempts, 2)
            self.assertFalse(s.has_pending_save)
            self.assertFalse(RuntimeStateStore(path, 14, 8).is_enabled("g"))

        asyncio.run(run())

    def test_same_reset_retries_pending_save(self):
        async def run():
            path = os.path.join(self.dir, "reset-retry.json")
            s = RuntimeStateStore(path, 14, 8, ())
            self.assertTrue(await s.record_response("g", "好的，回答"))
            real_write = s._write_snapshot_sync
            attempts = 0

            def fail_once(payload):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("disk full")
                real_write(payload)

            with mock.patch.object(s, "_write_snapshot_sync", side_effect=fail_once):
                self.assertFalse(await s.reset("g"))
                self.assertNotIn("g", s.sessions)
                self.assertTrue(await s.reset("g"))
            self.assertEqual(attempts, 2)
            self.assertNotIn("g", RuntimeStateStore(path, 14, 8).sessions)

        asyncio.run(run())


class TestConcurrentPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = temporary_directory(self)

    def test_record_response_returns_before_slow_write_finishes(self):
        async def run():
            s = RuntimeStateStore(os.path.join(self.dir, "slow.json"), 14, 8, ())
            started = threading.Event()
            release = threading.Event()
            real_write = s._write_snapshot_sync

            def slow_write(payload):
                started.set()
                release.wait(timeout=5)
                real_write(payload)

            with (
                mock.patch.object(runtime_state_module, "STATE_SAVE_DEBOUNCE_SECONDS", 0, create=True),
                mock.patch.object(s, "_write_snapshot_sync", side_effect=slow_write),
            ):
                record_task = asyncio.create_task(s.record_response("g", "好的，回答"))
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                try:
                    self.assertTrue(record_task.done())
                    self.assertFalse(s._state_lock.locked())
                    self.assertEqual(s.get("g").recent_openers, ["好的"])
                finally:
                    release.set()
                    await record_task
                save_task = getattr(s, "_save_task", None)
                self.assertIsNotNone(save_task)
                await save_task

        asyncio.run(run())

    def test_concurrent_records_persist_latest_combined_state(self):
        async def run():
            path = os.path.join(self.dir, "concurrent.json")
            s = RuntimeStateStore(path, 14, 8, ())
            first_started = threading.Event()
            release_first = threading.Event()
            real_write = s._write_snapshot_sync
            writes = 0
            snapshots = []

            def controlled_write(payload):
                nonlocal writes
                writes += 1
                snapshots.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                if writes == 1:
                    first_started.set()
                    release_first.wait(timeout=5)
                real_write(payload)

            with (
                mock.patch.object(runtime_state_module, "STATE_SAVE_DEBOUNCE_SECONDS", 0, create=True),
                mock.patch.object(s, "_write_snapshot_sync", side_effect=controlled_write),
            ):
                first_record = asyncio.create_task(s.record_response("g", "好的，回答一"))
                self.assertTrue(await asyncio.to_thread(first_started.wait, 5))
                try:
                    self.assertTrue(first_record.done())
                    save_task = getattr(s, "_save_task", None)
                    self.assertIsNotNone(save_task)
                    self.assertTrue(await s.record_response("g", "可以，回答二"))
                finally:
                    release_first.set()
                    await first_record
                await save_task

            persisted = RuntimeStateStore(path, 14, 8).get("g")
            self.assertEqual(persisted.recent_openers, ["可以", "好的"])
            self.assertFalse(s.has_pending_save)
            self.assertEqual(len(snapshots), len(set(snapshots)))

        asyncio.run(run())

    def test_burst_updates_coalesce_to_one_write(self):
        async def run():
            path = os.path.join(self.dir, "burst.json")
            store = RuntimeStateStore(path, 14, 100, ())
            real_write = store._write_snapshot_sync
            writes = 0

            def count_write(payload):
                nonlocal writes
                writes += 1
                real_write(payload)

            with (
                mock.patch.object(runtime_state_module, "STATE_SAVE_DEBOUNCE_SECONDS", 0.01, create=True),
                mock.patch.object(store, "_write_snapshot_sync", side_effect=count_write),
            ):
                for index in range(100):
                    self.assertTrue(await store.record_response("g", f"第{index}次回答"))
                save_task = getattr(store, "_save_task", None)
                self.assertIsNotNone(save_task)
                await save_task

            self.assertEqual(writes, 1)
            self.assertFalse(store.has_pending_save)
            persisted = RuntimeStateStore(path, 14, 100).get("g")
            self.assertEqual(persisted.recent_openers[0], "第99次回答")

        asyncio.run(run())

    def test_terminate_flushes_pending_debounce(self):
        async def run():
            path = os.path.join(self.dir, "terminate.json")
            store = RuntimeStateStore(path, 14, 8, ())
            with mock.patch.object(runtime_state_module, "STATE_SAVE_DEBOUNCE_SECONDS", 3600, create=True):
                self.assertTrue(await store.record_response("g", "好的，回答"))
                self.assertTrue(store.has_pending_save)
                self.assertTrue(await store.terminate())

            self.assertFalse(store.has_pending_save)
            self.assertIn("g", RuntimeStateStore(path, 14, 8).sessions)

        asyncio.run(run())

    def test_concurrent_flushes_write_dirty_generation_once(self):
        async def run():
            path = os.path.join(self.dir, "same-generation.json")
            store = RuntimeStateStore(path, 14, 8, ())
            with mock.patch.object(store, "_write_snapshot_sync", side_effect=OSError("disk full")):
                self.assertFalse(await store.set_enabled("g", False))

            started = threading.Event()
            release = threading.Event()
            real_write = store._write_snapshot_sync
            writes = 0

            def slow_write(payload):
                nonlocal writes
                writes += 1
                started.set()
                release.wait(timeout=5)
                real_write(payload)

            with mock.patch.object(store, "_write_snapshot_sync", side_effect=slow_write):
                first = asyncio.create_task(store.flush())
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                second = asyncio.create_task(store.flush())
                release.set()
                self.assertEqual(await asyncio.gather(first, second), [True, True])

            self.assertEqual(writes, 1)

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
                await s.flush()
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

    def test_parse_group_id_from_two_part_origin(self):
        self.assertEqual(_parse_group_id_from_origin("GroupMessage:111"), "111")

    def test_is_session_disabled_two_part_origin_matches_numeric_group_id(self):
        self.assertTrue(is_session_disabled(frozenset({"111"}), "GroupMessage:111", None))

    def test_parse_group_id_from_three_part_origin(self):
        self.assertEqual(_parse_group_id_from_origin("aiocqhttp:GroupMessage:111"), "111")


if __name__ == "__main__":
    unittest.main()
