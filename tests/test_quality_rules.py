"""quality_rules 模块契约测试：规则注入幂等、marker 三态、temp part 契约。

3.0.0 起 v1–v8 legacy 剥离签名表已退役（ARCHITECTURE.md D7 终态）：
旧 marker 块按普通文本保留，不再剥离。无需宿主 astrbot 即可运行。
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

    def test_runtime_factory_contract_does_not_require_removed_content_protocol(self):
        from astrbot_plugin_human_chat_quality import protocols

        self.assertTrue(hasattr(protocols, "TextPartFactoryProtocol"))
        self.assertFalse(hasattr(protocols, "ContentPartProtocol"))

    def test_quality_rules_does_not_depend_on_runtime_state(self):
        self.assertFalse(hasattr(quality_rules, "SessionState"))

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
            "然而连发",
            "结构性表演",
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
        # 3.0.0：旧规则块按普通文本保留，不再剥离
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
        self.assertEqual(RULES_VERSION, 11)
        # legacy 机器已退役：模块不再导出 legacy 剥离设施
        self.assertFalse(hasattr(quality_rules, "LEGACY_STABLE_MARKERS"))
        self.assertFalse(hasattr(quality_rules, "_LEGACY_STABLE_SIGNATURES"))
        self.assertFalse(hasattr(quality_rules, "_LEGACY_RUNTIME_PREFIX"))
        self.assertFalse(hasattr(quality_rules, "_is_legacy_truncated_runtime"))

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
        self.assertIn("铁律：先否定后肯定（不是/与其/看似/很久…久到）删否定留肯定，直接说肯定面；角色引号内除外", rules)
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


if __name__ == "__main__":
    unittest.main()
