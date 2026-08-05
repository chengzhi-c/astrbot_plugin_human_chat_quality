"""astrbot_plugin_human_chat_quality 测试套件（无第三方依赖，unittest）。

运行：python -m unittest discover -s tests -v
覆盖：套路词/结构信号检测、opener 提取、声音校准、状态存取与损坏恢复。
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from voice_profile import (
    VOICE_MARKER,
    VoiceProfile,
    analyze_profile,
    build_voice_hint,
    extract_user_texts,
)


class DetectClichesTest(unittest.TestCase):
    """红绿灯：AI 腔样本必须命中，正常人话不得误报。"""

    RED_SAMPLES = [
        ("客服腔收尾", "希望这能帮到你，如果还有问题随时问我"),
        ("清单骨架", "总的来说，首先我们要看数据，其次看成本，最后看风险"),
        ("不仅更是+黑话", "这不仅是一个工具，更是一种赋能"),
        ("三连套话", "需要注意的是，这个问题众所周知，毋庸置疑是有价值的"),
        ("升华收尾", "总而言之，未来可期，让我们一起努力"),
        ("破折号连发", "深入探讨这个话题之前，作为AI，我想说这很有意义——但也要看到风险——不能盲目乐观——"),
        ("自问自答", "你可能会问：为什么这么贵？因为成本高"),
        ("三段式+闭环", "首先我们要明确目标，其次要分配资源，最后要复盘，这是一个闭环"),
        ("不是而是", "我不是在批评你，而是想帮你"),
        ("作为人工智能漏检回归", "作为人工智能，我想说这个问题很复杂"),
        ("此外然而连发", "此外，这个方案需要调整，然而我们时间不够了"),
        ("有趣的是开场", "有趣的是，这个数据跟预期完全相反"),
        ("不难发现套话", "不难发现，这个问题的主要矛盾在于成本"),
        ("事实上铺垫", "事实上，我之前也遇到过类似的情况"),
        ("然而连发", "这个方案不错，然而成本太高，然而时间也不够"),
        ("知识截止免责", "根据我的知识截止日期，我无法给出准确答案"),
        ("能力范围客服腔", "这个问题超出了我的能力范围，建议您咨询专业人士"),
    ]
    GREEN_SAMPLES = [
        "这个真的很好吃，我昨天刚试过，你可以去尝尝",
        "你说得对，我也觉得这样不太好",
        "这个我帮你看看，等下回复你",
        "嗯嗯，我刚试了下，可以",
        "别急，我查一下再告诉你",
        "让我看看这个文件",
        "没问题，我马上处理",
        "这个事我不好说，你自己拿主意吧",
        "这个计划可以，明天就执行",
        "我试过了，不行，换个思路吧",
        "另外我还有事，先不聊了，晚点再说",
        "不过说真的，那个店确实一般般",
        "我不知道啊，这个我真没研究过，别问我了",
        "这个我不太确定，得查一下再告诉你",
    ]

    def test_red_samples_hit(self):
        for name, text in self.RED_SAMPLES:
            with self.subTest(name=name):
                self.assertTrue(detect_cliches(text), f"{name} 未命中")

    def test_green_samples_clean(self):
        for text in self.GREEN_SAMPLES:
            with self.subTest(text=text[:12]):
                self.assertEqual(detect_cliches(text), [], f"误报: {text}")


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
    def test_repeated(self):
        self.assertEqual(repeated_items(["a", "b", "a", "c", "b"], limit=5), ["a", "b"])

    def test_limit(self):
        self.assertEqual(repeated_items(["a", "b", "a", "b", "c", "d"], limit=1), ["a"])


class VoiceProfileTest(unittest.TestCase):
    SHORT_STYLE = ["哈哈确实", "嗯嗯", "可以吧", "笑死我了", "这也太逗了", "好家伙"]
    LONG_STYLE = [
        "我觉得这个问题需要从多个角度来考虑，首先呢这个方案的可行性确实值得深入讨论一下，特别是实施周期和成本控制方面",
        "今天的工作进展比较顺利，完成了三个模块的联调，明天继续处理剩下的部分，预计周五之前可以全部收尾完成交付 😂",
        "其实我一直觉得这个设计思路挺好的，就是实现起来成本会比较高一些，需要权衡一下性价比再决定",
        "最近在追一部剧，感觉编剧的节奏把握得很好，每个角色的塑造都很立体，尤其是反派人物完全没有脸谱化处理",
        "😄 这个功能终于上线了，测试了两周终于没问题了，大家都辛苦了，后续有什么问题随时反馈给我就行 👍",
        "我觉得可以先把文档整理一下，然后下周安排评审，再根据反馈调整，这样流程上会顺畅很多，大家也轻松一些",
    ]

    def test_short_style(self):
        profile = analyze_profile(self.SHORT_STYLE)
        self.assertIsNotNone(profile)
        hint = build_voice_hint(profile)
        self.assertIn("消息偏短", hint)
        self.assertIn("爱用短句", hint)
        self.assertTrue(hint.startswith(VOICE_MARKER))

    def test_long_style(self):
        profile = analyze_profile(self.LONG_STYLE)
        self.assertIsNotNone(profile)
        hint = build_voice_hint(profile)
        self.assertIn("消息偏长", hint)
        self.assertIn("常带表情", hint)

    def test_extract_cleans_quote_and_at_prefix(self):
        """引用消息 (昵称): 内容 与 @提及 前缀不污染风格样本（回归）。"""
        contexts = [
            {"role": "user", "content": "(小明): 哈哈哈这个图笑死我了"},
            {"role": "user", "content": "（小红）：确实确实"},
            {"role": "user", "content": "@阿花 明天去吗"},
            {"role": "user", "content": "@bot: 好的好的"},
            {"role": "user", "content": "正常消息"},
        ]
        texts = extract_user_texts(contexts)
        self.assertEqual(
            texts,
            ["哈哈哈这个图笑死我了", "确实确实", "明天去吗", "好的好的", "正常消息"],
        )

    def test_extract_filters_injected(self):
        """注入文本与 assistant 消息不参与风格统计（回归）。"""
        contexts = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "正常消息一"},
            {"role": "user", "content": [{"type": "text", "text": "正常消息二"}]},
            {"role": "user", "content": f"{STABLE_RULE_MARKER}\n注入规则不算风格"},
            {"role": "assistant", "content": "机器回复不参与"},
        ]
        texts = extract_user_texts(contexts)
        self.assertEqual(texts, ["正常消息一", "正常消息二"])

    def test_insufficient_samples(self):
        self.assertIsNone(analyze_profile(["只有一条"]))
        self.assertIsNone(analyze_profile([]))

    def test_extract_limit(self):
        contexts = [{"role": "user", "content": f"消息{i}"} for i in range(80)]
        texts = extract_user_texts(contexts, limit=60)
        self.assertEqual(len(texts), 60)
        self.assertEqual(texts[0], "消息20")

    def test_flag_emoji_detected(self):
        """地区旗帜（🇨🇳）也计入表情统计（回归：1F1E6-1F1FF 漏检）。"""
        samples = [f"消息{i} 🇨🇳" for i in range(6)]
        profile = analyze_profile(samples)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.emoji_ratio, 1.0)
        self.assertIn("常带表情", build_voice_hint(profile))

    def test_single_char_opener_filtered(self):
        """单字符消息不构成"常以 X 开头"特征（回归：避免诱导复读单字）。"""
        samples = ["嗯", "哈", "哦", "好的", "嗯", "哈"]
        profile = analyze_profile(samples)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.openers, ["好的"])

    def test_hint_clip_boundary(self):
        """极小的 max_chars 不应产生负索引截断。"""
        profile = VoiceProfile(
            sample_count=6,
            avg_msg_len=10.0,
            short_ratio=0.8,
            emoji_ratio=0.0,
            tone_words=["吧"],
            openers=[],
        )
        self.assertEqual(build_voice_hint(profile, max_chars=0), "")
        self.assertEqual(build_voice_hint(profile, max_chars=2), "..")
        hint = build_voice_hint(profile, max_chars=100)
        self.assertLessEqual(len(hint), 100)
        self.assertTrue(hint.endswith("..."))

    def test_hint_excludes_openers(self):
        """与 runtime 避用开头重叠的 opener 不注入（回归：同轮矛盾指令）。"""
        profile = VoiceProfile(
            sample_count=6,
            avg_msg_len=10.0,
            short_ratio=0.8,
            emoji_ratio=0.0,
            tone_words=["吧"],
            openers=["好的", "确实"],
        )
        hint = build_voice_hint(profile, exclude_openers={"好的"})
        self.assertIn("确实", hint)
        self.assertNotIn("好的", hint)
        self.assertNotIn("常以好的", hint)

    def test_hint_excludes_opener_prefix_variant(self):
        """前缀变体也视为重叠（回归："好的" vs "好的，我来"）。"""
        profile = VoiceProfile(
            sample_count=6,
            avg_msg_len=10.0,
            short_ratio=0.8,
            emoji_ratio=0.0,
            tone_words=[],
            openers=["好的", "确实"],
        )
        hint = build_voice_hint(profile, exclude_openers={"好的，我来看看"})
        self.assertIn("确实", hint)
        self.assertNotIn("好的", hint)


class RuntimeStateStoreTest(unittest.TestCase):
    def _make_store(self, tmp: Path, window=8, custom=None):
        return RuntimeStateStore(
            tmp / "state.json",
            retention_days=14,
            recent_reply_window=window,
            custom_cliches=custom,
        )

    def _run(self, coro):
        return asyncio.run(coro)

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
            self.assertEqual(store.disabled_sessions, set())
            self.assertEqual(list(tmp.glob("state.corrupt.*.json")), [], "不应生成损坏备份")

    def test_load_bad_disabled_type_backups(self):
        """disabled_sessions 类型错误（dict）走损坏分支（回归：M2 类型校验）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_file = tmp / "state.json"
            state_file.write_text('{"disabled_sessions": {"a": 1}, "sessions": {}}', encoding="utf-8")
            store = self._make_store(tmp)
            self.assertEqual(store.sessions, {})
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

    def test_custom_cliches_merged(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(Path(td), custom=["我们群的黑话"])
            self.assertIn("我们群的黑话", store.cliches)
            hits = detect_cliches("这句话里有我们群的黑话", store.cliches)
            self.assertIn("我们群的黑话", hits)

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
        """写盘失败不应抛出（回归：旧版 raise 污染回复链路）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = self._make_store(tmp)
            store.state_path = tmp / "no_such_dir" / "state.json"
            store.state_path.parent.mkdir(parents=True)
            # 把父目录变成文件，使 write_text 失败
            store.state_path.parent.rmdir()
            (tmp / "no_such_dir").write_text("i am a file", encoding="utf-8")
            try:
                self._run(store.record_response("s1", "这是一条会触发保存失败的消息"))
            except Exception:
                self.fail("_save 失败不应向调用方抛异常")


class _FakeEvent:
    def __init__(self, origin="", group_id=None, message_obj=None):
        self.unified_msg_origin = origin
        self._group_id = group_id
        self.message_obj = message_obj

    def get_group_id(self):
        return self._group_id


class GroupMatchTest(unittest.TestCase):
    """群号提取与禁用匹配（main.py 纯函数）。"""

    def test_group_id_from_session(self):
        self.assertEqual(group_id_from_session_id("aiocqhttp:GroupMessage:123456#abc"), "123456#abc")
        self.assertEqual(group_id_from_session_id("PrivateMessage:123"), "")

    def test_disabled_from_event_splits_base(self):
        event = _FakeEvent(origin="aiocqhttp:GroupMessage:123456#abc", group_id="123456#abc")
        candidates = disabled_match_candidates(event)
        self.assertIn("123456", candidates)
        self.assertIn("group:123456", candidates)
        self.assertIn("GroupMessage:123456#abc", candidates)

    def test_disabled_from_session_splits_base(self):
        """session 路径与 event 路径行为一致（回归：# 拆分缺失）。"""
        candidates = disabled_match_candidates_from_session("aiocqhttp:GroupMessage:123456#abc")
        self.assertIn("123456", candidates)
        self.assertIn("group:123456", candidates)

    def test_disabled_from_event_falls_back_to_origin(self):
        event = _FakeEvent(origin="PrivateMessage:777", group_id=None)
        candidates = disabled_match_candidates(event)
        self.assertIn("PrivateMessage:777", candidates)
        self.assertNotIn("777", candidates)


class StableRulesTest(unittest.TestCase):
    def test_rules_content(self):
        rules = build_stable_rules()
        self.assertTrue(rules.startswith(STABLE_RULE_MARKER))
        self.assertIn("铁律", rules)
        self.assertIn("像个人", rules)


if __name__ == "__main__":
    unittest.main()
