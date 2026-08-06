"""astrbot_plugin_human_chat_quality 测试套件（无第三方依赖，unittest）。

运行：python -m unittest discover -s tests -v
覆盖：套路词/结构信号检测、opener 提取、状态存取与损坏恢复。
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _fakes import FakeEvent

from main import (
    disabled_match_candidates,
    disabled_match_candidates_from_session,
    group_id_from_session_id,
)
from quality_rules import STABLE_RULE_MARKER, build_stable_rules
from runtime_state import (
    RuntimeStateStore,
    detect_cliches,
    extract_opener,
    repeated_items,
)


class DetectClichesTest(unittest.TestCase):
    """红绿灯（v0.6.0）：高置信度末尾模板/连发信号必须命中；正式技术/正常转折/正常列表不得误报。"""

    RED_SAMPLES: ClassVar[list[tuple[str, str]]] = [
        ("客服腔收尾", "我先帮你查了一下，希望这能帮到你"),
        ("客服腔收尾带标点", "没问题，如果还有问题随时问我。"),
        ("空泛鼓励收尾", "别灰心，我们一起加油！"),
        ("升华收尾", "坚持住，未来可期"),
        ("破折号连发", "这个方案——说实话——不太行"),
        ("然而连发", "这个方案不错，然而成本太高，然而时间也不够"),
    ]
    GREEN_SAMPLES: ClassVar[list[str]] = [
        # 日常闲聊
        "这个真的很好吃，我昨天刚试过，你可以去尝尝",
        "嗯嗯，我刚试了下，可以",
        "别急，我查一下再告诉你",
        "另外我还有事，先不聊了，晚点再说",
        # 正式技术回答（正文出现连接词/套话不得误报）
        "首先检查电源，其次看网络配置，最后重启服务。此外，日志里的报错信息也值得看一下",
        "事实上这个方案的可行性很高，众所周知成本也不低，值得注意的是兼容性",
        "总的来说，这个接口可以先赋能业务闭环，抓手是沉淀数据",
        # 正常转折/句式（v0.6.0 不再拦截）
        "我不是在批评你，而是想帮你",
        "你可能会问：为什么这么贵？因为成本高",
        "这个方案不错，然而成本确实高了一点",
        "单破折号是正常停顿——不算连发",
        # 客服词出现在句中而非收尾，不得命中
        "你之前说希望能帮到你，我觉得可以的",
    ]

    def test_red_samples_hit(self):
        for name, text in self.RED_SAMPLES:
            with self.subTest(name=name):
                self.assertTrue(detect_cliches(text), f"{name} 未命中")

    def test_green_samples_clean(self):
        for text in self.GREEN_SAMPLES:
            with self.subTest(text=text[:12]):
                self.assertEqual(detect_cliches(text), [], f"误报: {text}")

    def test_custom_cliche_anywhere_single_hit(self):
        """管理员显式词库：任意位置一次精确命中即提示。"""
        hits = detect_cliches("这句话里有我们群的黑话，在句中", ("我们群的黑话",))
        self.assertEqual(hits, ["我们群的黑话"])

    def test_substring_cliches_both_hit(self):
        """子串关系词同时命中时都记录（去重只挡完全相同的词，固化语义）。"""
        hits = detect_cliches("我们一起去看看，快点", ("看看", "看"))
        self.assertEqual(hits, ["看看", "看"])
        # 完全相同词去重
        self.assertEqual(detect_cliches("看看看看", ("看看", "看看")), ["看看"])


class ExtractOpenerTest(unittest.TestCase):
    def test_prefix_opener(self):
        self.assertEqual(extract_opener("好的，我来看看"), "好的")
        self.assertEqual(extract_opener("没问题，马上处理"), "没问题")

    def test_first_segment(self):
        self.assertEqual(extract_opener("这个方案我觉得可行，明天试试"), "这个方案我觉得可")

    def test_single_char_filtered(self):
        """单字文本（我/你/这）噪声大，不纳入 opener。"""
        self.assertEqual(extract_opener("我"), "")
        self.assertEqual(extract_opener("嗯"), "")

    def test_multi_char_opener_kept(self):
        """多字开头是有价值的重复信号，保留。"""
        self.assertEqual(extract_opener("我觉得可以"), "我觉得可以")

    def test_empty(self):
        self.assertEqual(extract_opener(""), "")
        self.assertEqual(extract_opener("   "), "")


class RepeatedItemsTest(unittest.TestCase):
    def test_threshold_default_three(self):
        """v0.6.0：默认阈值 3 次，两次重复不再视为信号。"""
        self.assertEqual(repeated_items(["a", "b", "a", "c", "b"], limit=5), [])
        self.assertEqual(repeated_items(["a", "b", "a", "a"], limit=5), ["a"])

    def test_explicit_threshold(self):
        self.assertEqual(repeated_items(["a", "b", "a", "c", "b"], limit=5, threshold=2), ["a", "b"])

    def test_threshold_one(self):
        """threshold=1 退化：首次出现即视为重复（固化退化语义）。"""
        self.assertEqual(repeated_items(["a", "b", "a"], limit=5, threshold=1), ["a", "b"])

    def test_threshold_one_single(self):
        """threshold=1 单元素退化：首个元素即视为重复（反向护栏）。"""
        self.assertEqual(repeated_items(["a"], limit=5, threshold=1), ["a"])

    def test_limit(self):
        self.assertEqual(repeated_items(["a", "b", "a", "b", "a", "b"], limit=1, threshold=2), ["a"])


class RuntimeStateStoreTest(unittest.TestCase):
    def _make_store(self, tmp: Path, window=8, custom=None, retention=14):
        return RuntimeStateStore(
            tmp / "state.json",
            retention_days=retention,
            recent_reply_window=window,
            custom_cliches=custom,
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_overlong_cliche_not_recorded(self):
        """超长 custom_cliches（>20 字）构造期即过滤并告警（回归：三态分叉 + 静默无效配置）。"""
        long_phrase = "这是一个超过二十个字的超长黑话短语用来测试"
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td), custom=[long_phrase])
            self.assertNotIn(long_phrase, store.custom_cliches, "构造期即过滤超长词")
            self._run(store.record_response("s1", f"回复里出现了{long_phrase}，命中"))
            self.assertNotIn(long_phrase, store.get("s1").avoid_openers)
            # 短词不受影响
            store2 = self._make_store(Path(td), custom=["短黑话"])
            self.assertIn("短黑话", store2.custom_cliches)
            self._run(store2.record_response("s1", "短黑话出现了"))
            self.assertIn("短黑话", store2.get("s1").avoid_openers)

    def test_load_clamps_overlong_recent_openers(self):
        """外部编辑塞入超长 recent_openers 条目，加载侧截断到 8 字（读写不变量）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            overlong = "这是一个超过八个字的超长开头内容"
            state_file.write_text(
                json.dumps(
                    {
                        "disabled_sessions": [],
                        "sessions": {
                            "s1": {
                                "avoid_openers": [],
                                "recent_openers": [overlong],
                                "last_response_at": time.time(),
                                "updated_at": time.time(),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertEqual(store.sessions["s1"].recent_openers, [overlong[:8]])

    def test_lock_covers_memory_write(self):
        """锁边界覆盖内存写：第一个 record 持锁期间，第二个 record 必须阻塞在锁外
        （回归：正确性只依赖"函数体内无 await"的脆弱不变量；红灯实测确认
        以任务完成状态或内存结果断言均无法区分有锁/无锁，必须检测临界区进入次数）。"""
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td))
            entered = asyncio.Event()
            release = asyncio.Event()
            save_calls: list[str] = []
            real_save = store._save_unlocked

            async def gated_save():
                save_calls.append("enter")
                entered.set()
                await release.wait()
                await real_save()
                save_calls.append("exit")

            store._save_unlocked = gated_save

            async def work():
                first = asyncio.create_task(store.record_response("s1", "第一条回复内容"))
                await entered.wait()
                second = asyncio.create_task(store.record_response("s1", "第二条回复内容"))
                # 让出足够帧数：无锁时第二个协程会推进到 gated_save（save_calls 变 2）；
                # 有锁时它阻塞在 acquire（save_calls 保持 1）。帧数上限不依赖精确调度。
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertEqual(len(save_calls), 1, "锁未覆盖内存写：第二个 record 在第一个持锁期间进入了临界区")
                release.set()
                await asyncio.gather(first, second)
                self.assertEqual(len(save_calls), 4, "两个 record 应串行完成 enter/exit")

            self._run(work())
            # 两条 opener 都完整记录且按写入顺序排列（锁内串行使顺序由 enter 顺序决定）
            self.assertEqual(store.get("s1").recent_openers, ["第二条回复内容", "第一条回复内容"])

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._make_store(tmp)
            self._run(store.record_response("s1", "好的，我来看看这个问题"))
            store2 = self._make_store(tmp)
            self.assertIn("s1", store2.sessions)
            state = store2.sessions["s1"]
            self.assertEqual(state.recent_openers[0], "好的")

    def test_window_follows_config(self):
        """持久化截断跟随 recent_reply_window（回归：硬编码 20 的旧行为）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._make_store(tmp, window=30)
            for i in range(25):
                self._run(store.record_response("s1", f"回复内容第{i}条，用于填充窗口"))
            store2 = self._make_store(tmp, window=30)
            self.assertEqual(len(store2.sessions["s1"].recent_openers), 25)

    def test_corrupt_file_backup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text("{corrupt json!!!", encoding="utf-8")
            store = self._make_store(tmp)
            self.assertEqual(store.sessions, {})
            backups = list(tmp.glob("state.corrupt.*.json"))
            self.assertEqual(len(backups), 1)

    def test_load_missing_keys_ok(self):
        """旧版产物缺 disabled_sessions 键不应误判损坏（回归：M2 升级路径清空）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "sessions": {
                            "s1": {
                                "avoid_openers": [],
                                "recent_openers": ["好的"],
                                "last_response_at": None,
                                "updated_at": time.time(),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertIn("s1", store.sessions)
            self.assertEqual(store.sessions["s1"].recent_openers, ["好的"])
            self.assertEqual(store.runtime_disabled, set())
            self.assertEqual(list(tmp.glob("state.corrupt.*.json")), [], "不应生成损坏备份")

    def test_load_bad_disabled_type_keeps_sessions(self):
        """disabled_sessions 类型错误（dict）走条目级容错：备份现场、跳过坏键、
        sessions 不受影响（回归：旧行为连带清空全部会话）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "disabled_sessions": {"a": 1},
                        "sessions": {
                            "s1": {
                                "avoid_openers": [],
                                "recent_openers": [],
                                "last_response_at": time.time(),
                                "updated_at": time.time(),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertIn("s1", store.sessions)
            self.assertEqual(store.runtime_disabled, set())
            self.assertEqual(len(list(tmp.glob("state.corrupt.*.json"))), 1, "条目畸形应备份现场")

    def test_load_bad_session_entry_keeps_others(self):
        """sessions 内单条非 dict：备份现场、跳过坏键，其余会话保留（回归：静默吞掉坏键）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "disabled_sessions": [],
                        "sessions": {
                            "good": {
                                "avoid_openers": [],
                                "recent_openers": [],
                                "last_response_at": time.time(),
                                "updated_at": time.time(),
                            },
                            "bad": "not-a-dict",
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertIn("good", store.sessions)
            self.assertNotIn("bad", store.sessions)
            self.assertEqual(len(list(tmp.glob("state.corrupt.*.json"))), 1)

    def test_corrupt_backup_cap(self):
        """损坏备份只保留最近 5 份，磁盘不堆积（回归：M2 附项）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for _ in range(6):
                (tmp / "state.json").write_text("{corrupt!!!", encoding="utf-8")
                self._make_store(tmp)
            backups = sorted(tmp.glob("state.corrupt.*.json"))
            self.assertEqual(len(backups), 5)

    def test_corrupt_backup_cap_keeps_newest(self):
        """损坏备份按 mtime 保留最新 5 份（回归：文件名串序在 time_ns 变长整数时与时间序不一致）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            names = [
                "state.corrupt.20260807-000000-1234567890.json",  # 串序最小但 mtime 最旧
                "state.corrupt.20260807-000000-987.json",  # 串序大
                "state.corrupt.20260807-000000-8.json",
                "state.corrupt.20260807-000000-7.json",
                "state.corrupt.20260807-000000-6.json",
            ]
            for i, name in enumerate(names):
                (tmp / name).write_text("old", encoding="utf-8")
                os.utime(tmp / name, (1000 + i, 1000 + i))
            state_file.write_text("{corrupt!!!", encoding="utf-8")
            self._make_store(tmp)
            remaining = list(tmp.glob("state.corrupt.*.json"))
            self.assertEqual(len(remaining), 5)
            self.assertNotIn(tmp / names[0], remaining, "mtime 最旧的应被删除（即使串序最小）")

    def test_prune_expired_sessions(self):
        """超过 retention_days 未更新的会话在加载时被清理（回归：retention 零覆盖）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            stale = time.time() - 15 * 86400
            fresh = time.time() - 1 * 86400
            state_file.write_text(
                json.dumps(
                    {
                        "disabled_sessions": [],
                        "sessions": {
                            "stale": {
                                "avoid_openers": [],
                                "recent_openers": [],
                                "last_response_at": stale,
                                "updated_at": stale,
                            },
                            "fresh": {
                                "avoid_openers": [],
                                "recent_openers": [],
                                "last_response_at": fresh,
                                "updated_at": fresh,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertNotIn("stale", store.sessions)
            self.assertIn("fresh", store.sessions)

    def test_prune_expired_falls_back_to_now(self):
        """updated_at/last_response_at 均为 None 的条目按当前时间计，不被清理。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "disabled_sessions": [],
                        "sessions": {
                            "no_time": {
                                "avoid_openers": [],
                                "recent_openers": [],
                                "last_response_at": None,
                                "updated_at": None,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertIn("no_time", store.sessions)

    def test_custom_cliches_kept_separate(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td), custom=["我们群的黑话"])
            self.assertIn("我们群的黑话", store.custom_cliches)
            hits = detect_cliches("这句话里有我们群的黑话", store.custom_cliches)
            self.assertIn("我们群的黑话", hits)

    def test_opener_needs_three_repeats(self):
        """v0.6.0：同一 opener 出现 3 次才进提醒列表，2 次不进（降低误报）。"""
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td))
            self._run(store.record_response("s1", "好的，第一回"))
            self._run(store.record_response("s1", "好的，第二回"))
            self.assertEqual(store.get("s1").avoid_openers, [])
            self._run(store.record_response("s1", "好的，第三回"))
            self.assertIn("好的", store.get("s1").avoid_openers)

    def test_same_text_keeps_updated_at_fresh(self):
        """连续同文本回复仍需刷新 updated_at，不得被脏检查跳过（回归：D6 语义底线）。"""
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td))
            self._run(store.record_response("s1", "重复的内容填充回复"))
            first = store.get("s1").updated_at
            self._run(store.record_response("s1", "重复的内容填充回复"))
            store2 = self._make_store(Path(td))
            reloaded = store2.sessions["s1"]
            self.assertGreaterEqual(reloaded.updated_at, first)

    def test_snapshot_has_no_version_field(self):
        """v0.6.0：不再写入假 version 字段，旧文件中的同名字段继续被忽略。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._make_store(tmp)
            self._run(store.record_response("s1", "内容填充回复测试"))
            raw = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("version", raw)
            # 带 version 的旧文件仍可正常加载
            (tmp / "state2.json").write_text(
                json.dumps({"version": 1, "disabled_sessions": [], "sessions": {}}), encoding="utf-8"
            )
            store2 = RuntimeStateStore(tmp / "state2.json", retention_days=14, recent_reply_window=8)
            self.assertEqual(store2.sessions, {})

    def test_write_goes_through_thread_writer(self):
        """持久化经可替换的同步 writer（asyncio.to_thread 封装点），单测不依赖机器耗时。"""
        import threading

        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td))
            calls: list[tuple[int, dict]] = []

            def spy(payload):
                calls.append((threading.get_ident(), payload))
                real_writer(payload)

            real_writer = store._write_snapshot_sync
            store._write_snapshot_sync = spy
            self._run(store.record_response("s1", "内容填充回复测试"))
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0][0], threading.get_ident(), "写入应在工作线程执行")
            self.assertIn("s1", calls[0][1]["sessions"])

    def test_concurrent_save(self):
        """多会话并发写盘不丢状态（asyncio.Lock 保护）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._make_store(tmp)

            async def work():
                await asyncio.gather(
                    store.record_response("s1", "第一条回复内容，用来填充状态"),
                    store.record_response("s2", "第二条回复内容，用来填充状态"),
                    store.record_response("s3", "第三条回复内容，用来填充状态"),
                )

            self._run(work())
            store2 = self._make_store(tmp)
            self.assertIn("s1", store2.sessions)
            self.assertIn("s2", store2.sessions)
            self.assertIn("s3", store2.sessions)

    def test_save_failure_swallowed(self):
        """写盘失败不应抛出（回归：旧版 raise 污染回复链路），且应留下 warning 日志。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._make_store(tmp)
            store.state_path = tmp / "no_such_dir" / "state.json"
            store.state_path.parent.mkdir(parents=True)
            # 把父目录变成文件，使 write_text 失败
            store.state_path.parent.rmdir()
            (tmp / "no_such_dir").write_text("i am a file", encoding="utf-8")
            from unittest import mock

            with mock.patch("runtime_state.logger.warning") as warn:
                try:
                    self._run(store.record_response("s1", "这是一条会触发保存失败的消息"))
                except Exception:
                    self.fail("_save 失败不应向调用方抛异常")
                warn.assert_called_once()
                self.assertIn("state save failed", warn.call_args.args[0])


class GroupMatchTest(unittest.TestCase):
    """群号提取与禁用匹配（main.py 纯函数）。"""

    def test_group_id_from_session(self):
        self.assertEqual(group_id_from_session_id("aiocqhttp:GroupMessage:123456#abc"), "123456#abc")
        self.assertEqual(group_id_from_session_id("PrivateMessage:123"), "")

    def test_disabled_from_event_splits_base(self):
        event = FakeEvent(origin="aiocqhttp:GroupMessage:123456#abc", group_id="123456#abc")
        candidates = disabled_match_candidates(event)
        self.assertIn("123456", candidates)
        self.assertIn("group:123456", candidates)
        self.assertIn("groupmessage:123456#abc", candidates)

    def test_disabled_from_session_splits_base(self):
        """session 路径与 event 路径行为一致（回归：# 拆分缺失）。"""
        candidates = disabled_match_candidates_from_session("aiocqhttp:GroupMessage:123456#abc")
        self.assertIn("123456", candidates)
        self.assertIn("group:123456", candidates)

    def test_disabled_from_event_with_platform_prefix(self):
        """生产形态 origin（带平台前缀）的候选集合显式化（隐含契约补测试）。"""
        event = FakeEvent(origin="aiocqhttp:GroupMessage:123456#abc", group_id="123456#abc")
        candidates = disabled_match_candidates(event)
        self.assertIn("aiocqhttp:groupmessage:123456#abc", candidates)
        self.assertIn("groupmessage:123456#abc", candidates)
        self.assertIn("123456", candidates)
        self.assertIn("group:123456", candidates)
        self.assertIn("groupmessage:123456", candidates)

    def test_disabled_from_event_falls_back_to_origin(self):
        event = FakeEvent(origin="PrivateMessage:777", group_id=None)
        candidates = disabled_match_candidates(event)
        self.assertIn("privatemessage:777", candidates)
        self.assertNotIn("777", candidates)


class StableRulesTest(unittest.TestCase):
    def test_rules_content(self):
        rules = build_stable_rules()
        self.assertTrue(rules.startswith(STABLE_RULE_MARKER))
        # 五类约束的关键信息都在
        for keyword in ("客服式收尾", "事实", "清单", "不知道就直说", "不要把这些约束写进回复"):
            self.assertIn(keyword, rules)

    def test_rules_length_budget(self):
        """v0.6.0 预算：稳定规则不超过 550 字符（旧版 990）。"""
        self.assertLessEqual(len(build_stable_rules()), 550)

    def test_inject_blank_prompt(self):
        """仅空白 system_prompt 时不保留原空白，直接返回规则（回归：空白边界）。"""
        from quality_rules import inject_stable_rules

        self.assertEqual(inject_stable_rules("   "), build_stable_rules())


class BuildRuntimeHintTest(unittest.TestCase):
    """build_runtime_hint 边界：空状态/截断保 marker/超长词剔除。"""

    def test_empty_state_no_hint(self):
        from quality_rules import build_runtime_hint
        from runtime_state import SessionState

        self.assertEqual(build_runtime_hint(SessionState(), max_chars=80), "")

    def test_truncated_keeps_marker(self):
        from quality_rules import RUNTIME_HINT_MARKER, build_runtime_hint
        from runtime_state import SessionState

        state = SessionState(avoid_openers=["一个很长的重复开头词用于测试截断行为"], recent_openers=[])
        hint = build_runtime_hint(state, max_chars=80)
        self.assertLessEqual(len(hint), 80)
        self.assertTrue(hint.startswith(RUNTIME_HINT_MARKER), "截断不得破坏 marker 头部")

    def test_overlong_openers_excluded(self):
        from quality_rules import build_runtime_hint
        from runtime_state import SessionState

        state = SessionState(
            avoid_openers=["短词", "这是一个超过二十个字的超长重复开头条目用于测试"], recent_openers=[]
        )
        hint = build_runtime_hint(state, max_chars=600)
        self.assertIn("短词", hint)
        self.assertNotIn("这是一个超过二十个字", hint)


class HostVersionGateTest(unittest.TestCase):
    """宿主版本判别与 metadata 声明 (>=4.23,<5) 对齐（回归：缺 <5 上界）。"""

    def test_version_boundaries(self):
        from _fakes import is_supported_host

        self.assertFalse(is_supported_host("4.22.1"))
        self.assertTrue(is_supported_host("4.23.0"))
        self.assertTrue(is_supported_host("4.99.9"))
        self.assertFalse(is_supported_host("5.0.0"))
        self.assertFalse(is_supported_host("4"))
        self.assertFalse(is_supported_host("abc"))


class ClipTextTest(unittest.TestCase):
    """clip_text 边界直接覆盖（回归：唯一调用点 80-3000 clamp 下永不触发截断）。"""

    def test_boundaries(self):
        from quality_rules import clip_text

        self.assertEqual(clip_text("abc", 0), "")
        self.assertEqual(clip_text("abc", 1), ".")
        self.assertEqual(clip_text("abc", 2), "..")
        self.assertEqual(clip_text("abcd", 3), "...")
        self.assertEqual(clip_text("abc", 3), "abc", "长度恰好等于 max_chars 不截断")
        self.assertEqual(clip_text("abcde", 4), "a...")
        self.assertEqual(clip_text("abcde", 5), "abcde")
        # 截断前 rstrip 尾部空白
        self.assertEqual(clip_text("abcdef  ", 4), "a...")


class StoreEdgeCaseTest(unittest.TestCase):
    """store 边界路径（回归：备份清理失败路径、set_enabled no-op、avoid_openers 截断）。"""

    def _make_store(self, tmp: Path, window=8, custom=None, retention=14):
        return RuntimeStateStore(
            tmp / "state.json",
            retention_days=retention,
            recent_reply_window=window,
            custom_cliches=custom,
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_set_enabled_noop_skips_write(self):
        """set_enabled 状态无变化不触发写盘（回归：0.6.1 no-op 短路无覆盖）。"""
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td))
            calls = []
            real_save = store._save_unlocked

            async def spy_save():
                calls.append(1)
                await real_save()

            store._save_unlocked = spy_save
            self._run(store.set_enabled("s1", False))
            self.assertEqual(len(calls), 1, "首次变更应写盘")
            self._run(store.set_enabled("s1", False))
            self.assertEqual(len(calls), 1, "状态无变化不应写盘")
            self._run(store.set_enabled("s1", True))
            self.assertEqual(len(calls), 2, "状态变化应写盘")

    def test_avoid_openers_truncated_to_five(self):
        """旧文件含 6 个 avoid_openers 时加载只保留 5 个（回归：MAX_AVOID_OPENERS 截断无覆盖）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "disabled_sessions": [],
                        "sessions": {
                            "s1": {
                                "avoid_openers": ["a", "b", "c", "d", "e", "f"],
                                "recent_openers": [],
                                "last_response_at": time.time(),
                                "updated_at": time.time(),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = self._make_store(tmp)
            self.assertEqual(store.sessions["s1"].avoid_openers, ["a", "b", "c", "d", "e"])


class GroupIdEdgeTest(unittest.TestCase):
    """normalize_group_id / extract_group_id 深度路径表驱动（回归：bool/异常/message_obj 兜底无覆盖）。"""

    def test_normalize_group_id_table(self):
        from main import normalize_group_id

        self.assertEqual(normalize_group_id(None), "")
        self.assertEqual(normalize_group_id(True), "")
        self.assertEqual(normalize_group_id(False), "")
        self.assertEqual(normalize_group_id(123), "123")
        self.assertEqual(normalize_group_id(" 456 "), "456")
        self.assertEqual(normalize_group_id([]), "")
        self.assertEqual(normalize_group_id({}), "")
        self.assertEqual(normalize_group_id(1.5), "")

    def test_extract_group_id_dict_priority(self):
        from main import extract_group_id

        self.assertEqual(extract_group_id({"qq": "111"}), "111")
        self.assertEqual(extract_group_id({"id": 222}), "222")
        self.assertEqual(extract_group_id({"group_id": 333, "id": 444}), "333", "group_id 优先")
        self.assertEqual(extract_group_id({"other": "x"}), "")

    def test_group_id_from_event_message_obj_fallback(self):
        """get_group_id 缺失时经 message_obj 的 group_id 属性兜底。"""
        from main import group_id_from_event

        class _MsgObj:
            group_id = "456"

        class _Event:
            unified_msg_origin = "aiocqhttp:GroupMessage:456#def"
            message_obj = _MsgObj()

        self.assertEqual(group_id_from_event(_Event()), "456")

    def test_group_id_getter_exception_falls_back(self):
        """get_group_id 抛异常时不污染，回退到 session 解析路径。"""
        from main import group_id_from_event

        class _Event:
            unified_msg_origin = "aiocqhttp:GroupMessage:789#ghi"
            message_obj = None

            def get_group_id(self):
                raise RuntimeError("boom")

        self.assertEqual(group_id_from_event(_Event()), "789#ghi")


class OptionalFloatTest(unittest.TestCase):
    def test_optional_float_table(self):
        from runtime_state import _optional_float

        self.assertIsNone(_optional_float(None))
        self.assertEqual(_optional_float(1.5), 1.5)
        self.assertEqual(_optional_float("2.5"), 2.5)
        self.assertIsNone(_optional_float("abc"))
        self.assertIsNone(_optional_float([]))


if __name__ == "__main__":
    unittest.main()
