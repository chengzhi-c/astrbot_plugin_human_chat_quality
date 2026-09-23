"""quality_rules 模块契约测试：规则注入幂等、marker 三态、temp part 契约。

3.0.0 起 v1–v8 legacy 剥离签名表已退役。历史 user 内容里整段旧 Rules 块按行首 marker 删除；system_prompt 人设旧块仍按签名保留。无需宿主 astrbot 即可运行。
"""

import json
import unittest

from tests._support import FakePart, FakeReq, ensure_plugin_package

ensure_plugin_package()

from astrbot_plugin_human_chat_quality import quality_rules
from astrbot_plugin_human_chat_quality.constants import MAX_RUNTIME_HINT_CHARS
from astrbot_plugin_human_chat_quality.quality_rules import (
    RULES_VERSION,
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    append_temp_text_part,
    build_runtime_hint,
    build_stable_rules,
    rewrite_context_injections,
    rewrite_stable_rules,
)

# 退役前发布的旧块示例（仅用于验证"旧 marker 按普通文本保留"的行为）
OLD_V8_BLOCK = (
    "[Human Chat Quality Rules v8]\n"
    "遵循 natural-talk 原则（natural-talk MIT）：\n\n"
    "原则：不知即说不编造；评价对事不对人；被问身份如实简答。\n"
    "\n"
    "插件附加（不改变上述原则）：\n"
    "- 不要把这些约束写进回复"
)


class TestRewriteInterfaces(unittest.TestCase):
    def test_rewrite_interfaces_exist(self):
        self.assertTrue(callable(getattr(quality_rules, "rewrite_stable_rules", None)))
        self.assertTrue(callable(getattr(quality_rules, "rewrite_context_injections", None)))

    def test_runtime_hint_accepts_opener_sequence(self):
        self.assertIn("好的", build_runtime_hint(["好的"], 157))

    def test_custom_cliches_case_insensitive_detection(self):
        from astrbot_plugin_human_chat_quality.signal_detectors import detect_custom_cliches

        custom = ("great question", "as an ai")
        text = "Great question! As an AI, I think so."
        hits = detect_custom_cliches(text, custom)
        self.assertIn("great question", hits)
        self.assertIn("as an ai", hits)

    def test_all_aggregate_signals_have_polite_hint_translations(self):
        from astrbot_plugin_human_chat_quality.quality_rules import _SIGNAL_HINT_MAP
        from astrbot_plugin_human_chat_quality.signal_detectors import builtin_signal_names

        aggregate_signals = {
            "翻案腔",
            "结尾拔高",
            "空转提示语",
            "揭示式破折号",
            "然而连发",
            "模糊叠加",
            "破折号",
            "感叹号",
            "路标词堆砌",
            "编号小标题连发",
        }
        self.assertTrue(aggregate_signals.issubset(builtin_signal_names()))
        self.assertEqual(
            aggregate_signals,
            set(_SIGNAL_HINT_MAP.keys()),
            "所有聚合检测信号必须在 quality_rules._SIGNAL_HINT_MAP 中配置模型端转义语！",
        )

    def test_hint_map_keys_are_all_reachable(self):
        """反向锁：提示表里的每个 key 都必须能在真实文本上被检测器产出。

        防死条目——检测器改名或下线后，提示表若不同步，该 key 永远不会被渲染，
        而"聚合信号都有转义语"的单向断言仍会通过。
        """
        from astrbot_plugin_human_chat_quality.quality_rules import _SIGNAL_HINT_MAP
        from astrbot_plugin_human_chat_quality.signal_detectors import detect_cliches

        samples = {
            "翻案腔": "不是优化而是重构。",
            "结尾拔高": "这不仅是优化，更是对工程的追求。",
            "空转提示语": "核心是：提高代码质量。",
            "揭示式破折号": "他的答案是——那就是缓存。",
            "模糊叠加": "可能或许要等正式通知。",
            "编号小标题连发": "# 一、准备\n# 二、实施\n# 三、验收\n",
            "然而连发": "然而a然而b",
            "路标词堆砌": "事实上这样。实际上那样。换句话说都不行。",
            "破折号": "a——b——c",
            "感叹号": "太好了！太棒了！真厉害！冲啊！",
        }
        self.assertEqual(set(samples), set(_SIGNAL_HINT_MAP))
        for key, text in samples.items():
            with self.subTest(signal=key):
                self.assertIn(key, detect_cliches(text), f"提示表条目 {key!r} 在检测器中已不可达")

    def test_hint_map_never_tells_the_model_to_do_the_forbidden_thing(self):
        """方向锁：转义语不得反向要求模型去做被检测的行为。

        历史缺陷——感叹号超上限的转义语曾写成"多用感叹号"，与检测目标完全相反。
        """
        from astrbot_plugin_human_chat_quality.quality_rules import _SIGNAL_HINT_MAP

        for key, hint in _SIGNAL_HINT_MAP.items():
            with self.subTest(signal=key):
                self.assertNotIn("多用", hint)
                self.assertNotIn("多打", hint)

    def test_family_hints_are_imperative_prohibitions(self):
        """新增语义族的转义语必须是祈使否定，不能只是名词标签。

        历史缺陷——五类异质病灶共用一个标签时，除翻案腔外全部得到"先否定后肯定句式"
        这条与病灶无关的指令，动态提醒实际失效。
        """
        from astrbot_plugin_human_chat_quality.quality_rules import _SIGNAL_HINT_MAP

        for key in ("翻案腔", "结尾拔高", "空转提示语", "揭示式破折号"):
            with self.subTest(signal=key):
                self.assertTrue(_SIGNAL_HINT_MAP[key].startswith(("别", "删")), _SIGNAL_HINT_MAP[key])


class TestStableRewrite(unittest.TestCase):
    def test_legacy_block_is_preserved_as_ordinary_text(self):
        """3.0.0 破坏性变更：旧规则块不再被剥离，按普通文本保留。"""
        result = rewrite_stable_rules(f"人设头\n\n{OLD_V8_BLOCK}\n\n人设尾", enabled=False)
        self.assertIn(OLD_V8_BLOCK, result.text)
        self.assertIn("人设头", result.text)
        self.assertIn("人设尾", result.text)
        self.assertEqual(result.removed, 0)

    def test_current_rules_removed_when_disabled(self):
        result = rewrite_stable_rules(f"人设\n\n{build_stable_rules()}\n\n尾巴", enabled=False)
        self.assertEqual(result.text, "人设\n\n尾巴")
        self.assertTrue(result.removed)
        self.assertFalse(result.injected)

    def test_duplicate_current_rules_keep_first_in_place(self):
        rules = build_stable_rules()
        prompt = f"头\n\n{rules}\n\n中\n\n{rules}\n\n尾"
        result = rewrite_stable_rules(prompt, enabled=True)
        self.assertEqual(result.text, f"头\n\n{rules}\n\n中\n\n尾")
        self.assertFalse(result.injected)
        self.assertTrue(result.removed)

    def test_stable_removal_reports_the_number_of_removed_blocks(self):
        rules = build_stable_rules()
        result = rewrite_stable_rules(f"{rules}\n\n{rules}\n\n{rules}", enabled=False)
        self.assertEqual(result.removed, 3)

    def test_edited_current_rules_block_is_kept_and_not_duplicated(self):
        rules = build_stable_rules()
        original = "不知即说，不编造"
        replacement = "不知就直说，不编造"
        self.assertIn(original, rules)
        edited = rules.replace(original, replacement, 1)
        self.assertNotEqual(edited, rules)
        first = rewrite_stable_rules(f"人设\n\n{edited}", enabled=True)
        self.assertIn(edited, first.text)
        self.assertIn(replacement, first.text)
        self.assertEqual(first.text.count(STABLE_RULE_MARKER), 1)
        self.assertFalse(first.injected)
        self.assertTrue(first.ambiguous)
        again = rewrite_stable_rules(first.text, enabled=True)
        self.assertEqual(again.text, first.text)
        self.assertFalse(again.injected)

    def test_inline_marker_mention_does_not_block_injection(self):
        prompt = "请解释 [Human Chat Quality Rules v3] 是什么"
        result = rewrite_stable_rules(prompt, enabled=True)
        self.assertTrue(result.text.startswith(prompt))
        self.assertEqual(result.text.count(STABLE_RULE_MARKER), 1)
        self.assertTrue(result.injected)

    def test_crlf_persona_is_preserved(self):
        prompt = "头部人设\r\n\r\n中间人设\r\n\r\n尾部人设"
        result = rewrite_stable_rules(prompt, enabled=False)
        self.assertEqual(result.text, prompt)


class TestContextRewrite(unittest.TestCase):
    def test_history_runtime_is_removed_instead_of_replaced(self):
        old = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        new = build_runtime_hint(["新开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": old}]}]

        result = rewrite_context_injections(req, new)

        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(result.runtime_removed, 1)
        self.assertFalse(result.runtime_satisfied)

    def test_context_removal_counts_every_owned_block(self):
        runtime = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OLD_V8_BLOCK},
                    {"type": "text", "text": runtime},
                    {"type": "text", "text": runtime},
                ],
            }
        ]

        result = rewrite_context_injections(req, None)

        self.assertEqual(result.runtime_removed, 2)
        self.assertEqual(result.stable_removed, 1)
        self.assertEqual(
            [part.get("text", "") for part in req.contexts[0]["content"]],
            [],
        )

    def test_inline_rules_marker_in_user_text_is_preserved(self):
        req = FakeReq()
        text = "请解释 [Human Chat Quality Rules v3] 是什么"
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        result = rewrite_context_injections(req, None)
        self.assertEqual(req.contexts[0]["content"][0]["text"], text)
        self.assertEqual(result.stable_removed, 0)

    def test_marker_mention_in_user_text_is_preserved(self):
        req = FakeReq()
        text = f"我在文档里看到了 {RUNTIME_HINT_MARKER}，它是什么意思？"
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        result = rewrite_context_injections(req, build_runtime_hint(["好的"], MAX_RUNTIME_HINT_CHARS))
        self.assertEqual(req.contexts[0]["content"][0]["text"], text)
        self.assertFalse(result.runtime_ambiguous)
        self.assertFalse(result.runtime_satisfied)

    def test_similar_runtime_block_is_preserved_as_ambiguous(self):
        req = FakeReq()
        text = RUNTIME_HINT_MARKER + "\n这是用户自己的相似格式"
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        result = rewrite_context_injections(req, None)
        self.assertEqual(req.contexts[0]["content"], [{"type": "text", "text": text}])
        self.assertTrue(result.runtime_ambiguous)
        self.assertFalse(result.runtime_removed)

    def test_history_removal_and_str_ambiguity_are_both_reported(self):
        old = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        new = build_runtime_hint(["新开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.contexts = [
            {"role": "user", "content": [{"type": "text", "text": old}]},
            {"role": "user", "content": RUNTIME_HINT_MARKER + "\n未知正文"},
        ]
        result = rewrite_context_injections(req, new)
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(result.runtime_removed, 1)
        self.assertFalse(result.runtime_satisfied)
        self.assertTrue(result.runtime_ambiguous)

    def test_multiple_runtime_blocks_converge_without_reordering_other_parts(self):
        old = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        new = build_runtime_hint(["新开头"], MAX_RUNTIME_HINT_CHARS)
        image = {"type": "image", "url": "keep"}
        req = FakeReq()
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "前"},
                    image,
                    {"type": "text", "text": old},
                    {"type": "text", "text": "后"},
                    {"type": "text", "text": old},
                ],
            }
        ]
        result = rewrite_context_injections(req, new)
        self.assertEqual(
            req.contexts[0]["content"],
            [{"type": "text", "text": "前"}, image, {"type": "text", "text": "后"}],
        )
        self.assertEqual(result.runtime_removed, 2)
        self.assertFalse(result.runtime_satisfied)

    def test_matching_runtime_block_is_removed_from_history(self):
        target = build_runtime_hint(["好的"], MAX_RUNTIME_HINT_CHARS)
        part = {"type": "text", "text": target}
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [part]}]
        result = rewrite_context_injections(req, target)
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertFalse(result.runtime_satisfied)
        self.assertEqual(result.runtime_removed, 1)

    def test_extra_stale_owned_part_is_removed_without_dict(self):
        old = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        new = build_runtime_hint(["新开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.extra_user_content_parts = [FakePart(old)]
        result = rewrite_context_injections(req, new)
        self.assertTrue(all(not isinstance(part, dict) for part in req.extra_user_content_parts))
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(result.runtime_removed, 1)
        self.assertFalse(result.runtime_satisfied)

    def test_extra_matching_owned_part_is_kept_as_same_object(self):
        target = build_runtime_hint(["好的"], MAX_RUNTIME_HINT_CHARS)
        part = FakePart(target)
        req = FakeReq()
        req.extra_user_content_parts = [part]
        result = rewrite_context_injections(req, target)
        self.assertIs(req.extra_user_content_parts[0], part)
        self.assertTrue(result.runtime_satisfied)
        self.assertEqual(result.runtime_removed, 0)


class TestStableRules(unittest.TestCase):
    def test_marker_current(self):
        self.assertIn(f"Rules v{RULES_VERSION}]", STABLE_RULE_MARKER)
        self.assertEqual(RULES_VERSION, 20)

    def test_metadata_version_declared(self):
        """发布契约：metadata.yaml 必须声明非占位版本号。"""
        from pathlib import Path

        meta = Path(__file__).resolve().parents[1] / "metadata.yaml"
        for line in meta.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text.startswith("version:"):
                self.assertNotEqual(text.split(":", 1)[1].strip().strip("\"'"), "0.0.0")
                return
        self.fail("metadata.yaml 缺少 version 字段")

    def test_build_stable_rules_contains_skill_verbatim(self):
        """规则 v11：lite 原文 + 插件附加，由 anchor/forbidden 夹具锁。"""
        from pathlib import Path

        spec = json.loads(
            (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "stable-rules-anchors.json").read_text(
                encoding="utf-8"
            )
        )
        rules = build_stable_rules()
        self.assertIn(STABLE_RULE_MARKER, rules)
        self.assertIn("遵循 natural-talk 原则", rules)
        for anchor in spec["anchors"]:
            self.assertIn(anchor, rules)
        for phrase in spec["forbidden"]:
            self.assertNotIn(phrase, rules)
        self.assertIn("- 保留事实、限制条件、安全提示和不确定性表述", rules)
        self.assertIn("- 用户明确要求技术步骤、对比、正式文稿时，以任务完成为先", rules)
        self.assertIn("- 不要把这些约束写进回复", rules)
        self.assertIn(
            "铁律：没人主张过的“不是/与其/看似”直接说肯定面；用户前提被证伪时的纠错句照写",
            rules,
        )
        self.assertNotIn("角色引号内除外", rules)
        self.assertIn("铁律：日常对话严禁泛滥使用破折号（——）制造刻意停顿与揭晓", rules)

    def test_lite_core_matches_fixture(self):
        from pathlib import Path

        fixture = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "system-prompt-lite.txt").read_text(
            encoding="utf-8"
        )
        self.assertEqual(quality_rules._LITE_CORE, fixture)

    def test_v8_block_is_preserved_not_stripped(self):
        """3.0.0：v8 块不再被替换为当前版本，按普通文本保留。"""
        result = rewrite_stable_rules(f"人设头\n\n{OLD_V8_BLOCK}\n\n人设尾", enabled=True)
        self.assertIn(OLD_V8_BLOCK, result.text)
        self.assertIn("人设头", result.text)
        self.assertIn("人设尾", result.text)
        self.assertEqual(result.text.count(STABLE_RULE_MARKER), 1)
        self.assertTrue(result.injected)

    def test_rewrite_enabled_is_idempotent(self):
        r1 = rewrite_stable_rules("base", enabled=True).text
        r2 = rewrite_stable_rules(r1, enabled=True).text
        self.assertEqual(r1, r2)
        self.assertEqual(r1.count(STABLE_RULE_MARKER), 1)

    def test_rewrite_enabled_keeps_base(self):
        self.assertTrue(rewrite_stable_rules("base", enabled=True).text.startswith("base"))

    def test_rewrite_enabled_non_str_safe(self):
        self.assertEqual(rewrite_stable_rules(None, enabled=True).text, build_stable_rules())


class TestAppendTempPart(unittest.TestCase):
    def test_rejects_missing_marker_prefix(self):
        req = FakeReq()
        ok = append_temp_text_part(req, "没有marker的文本", FakePart, marker=RUNTIME_HINT_MARKER)
        self.assertFalse(ok)
        self.assertEqual(len(req.extra_user_content_parts), 0)

    def test_append_only_constructs_parts(self):
        req = FakeReq()
        ok = append_temp_text_part(req, RUNTIME_HINT_MARKER + "\nhint", FakePart, marker=RUNTIME_HINT_MARKER)
        self.assertTrue(ok)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        ok2 = append_temp_text_part(req, RUNTIME_HINT_MARKER + "\nhint2", FakePart, marker=RUNTIME_HINT_MARKER)
        self.assertTrue(ok2)
        self.assertEqual(len(req.extra_user_content_parts), 2)

    def test_factory_none_degrades(self):
        req = FakeReq()
        self.assertFalse(append_temp_text_part(req, RUNTIME_HINT_MARKER + "\nh", None, marker=RUNTIME_HINT_MARKER))


class TestRuntimeHint(unittest.TestCase):
    def test_empty_when_no_openers(self):
        self.assertEqual(build_runtime_hint([], 157), "")

    def test_hint_starts_with_marker_and_keeps_complete_items(self):
        hint = build_runtime_hint(["好的", "没问题"], 157)
        self.assertTrue(hint.startswith(RUNTIME_HINT_MARKER))
        self.assertIn("好的", hint)
        self.assertIn("没问题", hint)
        items = ["甲" * 20, "乙" * 20, "丙" * 20, "丁" * 20, "戊" * 20]
        full = build_runtime_hint(items, 157)
        self.assertEqual(len(full), 157)
        self.assertTrue(all(item in full for item in items))

        short = build_runtime_hint(items, 80)
        self.assertLessEqual(len(short), 80)
        self.assertIn(items[0], short)
        self.assertNotIn(items[1], short)
        self.assertFalse(short.endswith("..."))

    def test_aggregate_hint_forbids_the_signal_it_names(self):
        hint = build_runtime_hint(["感叹号"], 157)
        self.assertIn("别堆感叹号", hint)
        self.assertNotIn("多用感叹号", hint)


class TestOwnershipEdges(unittest.TestCase):
    """所有权判定的边界与注入失败降级（此前全无覆盖）。"""

    def test_overlong_runtime_item_set_is_ambiguous_not_owned(self):
        items = "、".join("甲乙丙" * 8 for _ in range(quality_rules.MAX_AVOID_ITEMS + 1))
        text = f"{RUNTIME_HINT_MARKER}\n{quality_rules._RUNTIME_INSTRUCTION}\n{items}"
        self.assertEqual(quality_rules._runtime_kind(text), "ambiguous")

    def test_runtime_item_with_newline_is_ambiguous(self):
        text = f"{RUNTIME_HINT_MARKER}\n{quality_rules._RUNTIME_INSTRUCTION}\n开头\n结尾"
        self.assertEqual(quality_rules._runtime_kind(text), "ambiguous")

    def test_append_temp_text_part_rejects_marker_mismatch(self):
        req = FakeReq()
        self.assertFalse(append_temp_text_part(req, "没有 marker 的文本", FakePart, marker=RUNTIME_HINT_MARKER))
        self.assertEqual(req.extra_user_content_parts, [])

    def test_append_temp_text_part_degrades_when_factory_raises(self):
        def broken_factory(*, text):
            raise RuntimeError("provider rejects")

        req = FakeReq()
        self.assertFalse(append_temp_text_part(req, f"{RUNTIME_HINT_MARKER}\n提示", broken_factory))
        self.assertEqual(req.extra_user_content_parts, [])

    def test_append_temp_text_part_rejects_non_list_parts(self):
        req = FakeReq()
        req.extra_user_content_parts = "不是列表"
        self.assertFalse(append_temp_text_part(req, f"{RUNTIME_HINT_MARKER}\n提示", FakePart))
        self.assertEqual(req.extra_user_content_parts, "不是列表")

    def test_make_text_part_without_factory_returns_none(self):
        self.assertIsNone(quality_rules.make_text_part("文本", None))


if __name__ == "__main__":
    unittest.main()
