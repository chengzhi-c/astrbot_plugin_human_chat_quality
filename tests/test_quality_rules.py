"""quality_rules 模块契约测试：规则注入幂等、legacy 剥离、marker 三态、temp part 契约。

无需宿主 astrbot 即可运行（quality_rules 对 logger 做了 ImportError 防护）。
"""

import unittest

import sys
from pathlib import Path

# 插件目录本身即包根目录（astrbot_plugin_human_chat_quality），
# 需把仓库根目录的父目录加入 sys.path 才能以包方式导入（仓库可位于任意路径）
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from astrbot_plugin_human_chat_quality.quality_rules import (
    LEGACY_STABLE_MARKERS,
    MARKER_ABSENT,
    MARKER_MODIFIED,
    MARKER_STR_BLOCKED,
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    append_temp_text_part,
    apply_context_marker,
    build_runtime_hint,
    build_stable_rules,
    clip_text,
    inject_stable_rules,
)

# natural-talk v2.1.0「作为 System Prompt」章节原文行（非扭曲校验基准）
SKILL_CORE_LINES = (
    "- 直接回答，零开场零收尾，最多留一句有效过渡",
    "- 不知道就说不知道，不编造",
    "- 像朋友聊天，不像客服或老师",
)
SKILL_BAN_LINES = (
    '- "作为AI" / "希望帮助你" / "好问题"（全文最多 1 次）',
    '- "让我来" / "首先其次最后" / "综上所述"（全文最多 1 次）',
    '- "值得注意" / "事实上" 等路标词（全文不超过 2 次）',
    "- 评判对方 / 替对方做心理判断",
    "- 破折号（全文不超过 2 次）",
)
SKILL_REQ_LINES = (
    "- 句子长短交替，不匀速",
    '- 能用"是/有"就不绕',
    "- 主动语态，真实主语",
    "- 具体表达，删除空泛词",
)


class FakePart:
    def __init__(self, text):
        self.text = text


class FakeReq:
    def __init__(self, system_prompt="原人设：你是XX"):
        self.system_prompt = system_prompt
        self.contexts = []
        self.extra_user_content_parts = []


class TestStableRules(unittest.TestCase):
    def test_marker_v4_and_legacy(self):
        self.assertIn("Rules v4]", STABLE_RULE_MARKER)
        legacy_text = "|".join(LEGACY_STABLE_MARKERS)
        self.assertIn("Rules]", legacy_text)
        self.assertIn("Rules v1]", legacy_text)
        self.assertIn("Rules v2]", legacy_text)
        self.assertIn("Rules v3]", legacy_text)
        # legacy 判定不得误伤当前 marker 自身（startswith 互斥）
        for legacy in LEGACY_STABLE_MARKERS:
            self.assertNotEqual(legacy, STABLE_RULE_MARKER)
            self.assertFalse(legacy.startswith(STABLE_RULE_MARKER))
            self.assertFalse(STABLE_RULE_MARKER.startswith(legacy))

    def test_build_stable_rules_contains_skill_verbatim(self):
        """非扭曲校验：skill「作为 System Prompt」章节行逐字存在于规则文本中。"""
        rules = build_stable_rules()
        self.assertIn(STABLE_RULE_MARKER, rules)
        for line in SKILL_CORE_LINES + SKILL_BAN_LINES + SKILL_REQ_LINES:
            self.assertIn(line, rules, f"skill 原文行缺失: {line}")
        # 来源标注与插件附加
        self.assertIn("natural-talk v2.1.0", rules)
        self.assertIn("插件附加（不改变上述原则）", rules)

    def test_inject_idempotent(self):
        r1 = inject_stable_rules("base")
        r2 = inject_stable_rules(r1)
        self.assertEqual(r1, r2)
        self.assertEqual(r1.count(STABLE_RULE_MARKER), 1)

    def test_inject_keeps_base(self):
        self.assertTrue(inject_stable_rules("base").startswith("base"))

    def test_inject_non_str_safe(self):
        self.assertEqual(inject_stable_rules(None), build_stable_rules())

    def test_legacy_blocks_stripped_from_system_prompt(self):
        """v2 规则块从 system_prompt 剥离，正文与当前版本保留。"""
        prompt = "人设A\n\n[Human Chat Quality Rules v2]\n旧规则内容\n\n人设尾巴"
        result = inject_stable_rules(prompt)
        self.assertNotIn("Rules v2]", result)
        self.assertNotIn("旧规则内容", result)
        self.assertIn("人设A", result)
        self.assertIn("人设尾巴", result)
        self.assertIn(STABLE_RULE_MARKER, result)
        self.assertEqual(result.count(STABLE_RULE_MARKER), 1)

    def test_legacy_v1_and_unversioned_stripped(self):
        prompt = "base\n\n[Human Chat Quality Rules v1]\na\n\n[Human Chat Quality Rules]\nb"
        result = inject_stable_rules(prompt)
        self.assertNotIn("Rules v1]", result)
        self.assertNotIn("Rules]", result)
        self.assertIn("base", result)

    def test_v2_v3_legacy_coexist_heals(self):
        """system_prompt 同时含 v2 与 v3 旧块时：全部剥离并注入当前版本，不重复追加。"""
        prompt = "base\n\n[Human Chat Quality Rules v2]\n旧块\n\n[Human Chat Quality Rules v3]\n更旧块"
        result = inject_stable_rules(prompt)
        self.assertNotIn("Rules v2]", result)
        self.assertNotIn("Rules v3]", result)
        self.assertNotIn("旧块", result)
        self.assertNotIn("更旧块", result)
        self.assertIn("base", result)
        self.assertEqual(result.count("Rules v4]"), 1)


class TestApplyContextMarker(unittest.TestCase):
    def test_replace_single_block_self_heal(self):
        req = FakeReq()
        req.contexts = [
            {"role": "user", "content": [{"type": "text", "text": "你好"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": RUNTIME_HINT_MARKER + "\n旧提示"},
                    {"type": "text", "text": "原话"},
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": RUNTIME_HINT_MARKER + "\n多块残留"}]},
        ]
        res = apply_context_marker(req, RUNTIME_HINT_MARKER, "新提示")
        self.assertEqual(res, MARKER_MODIFIED)
        texts = [
            p.get("text", "")
            for ctx in req.contexts
            if ctx["role"] == "user"
            for p in ctx["content"]
        ]
        # 第一个命中被替换为纯提示文本（真实流程中提示以 marker 开头，此处验证块数与原话保留）
        self.assertEqual(texts.count("新提示"), 1)
        self.assertIn("原话", texts)
        # 多块自愈：第二个 marker 块被移除，无残留
        self.assertEqual(sum(1 for t in texts if RUNTIME_HINT_MARKER in t), 0)

    def test_remove_keeps_user_text(self):
        req = FakeReq()
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "原话"},
                    {"type": "text", "text": RUNTIME_HINT_MARKER + " x"},
                ],
            }
        ]
        res = apply_context_marker(req, RUNTIME_HINT_MARKER, None)
        self.assertEqual(res, MARKER_MODIFIED)
        texts = [p.get("text", "") for ctx in req.contexts for p in ctx["content"]]
        self.assertEqual(texts, ["原话"])

    def test_str_blocked_no_unsafe_cut(self):
        req = FakeReq()
        req.contexts = [{"role": "user", "content": RUNTIME_HINT_MARKER + "\n字符串形态残留"}]
        res = apply_context_marker(req, RUNTIME_HINT_MARKER, "新提示")
        self.assertEqual(res, MARKER_STR_BLOCKED)
        self.assertTrue(req.contexts[0]["content"].startswith(RUNTIME_HINT_MARKER))

    def test_absent_when_no_marker(self):
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, "hint"), MARKER_ABSENT)


class TestAppendTempPart(unittest.TestCase):
    def test_rejects_missing_marker_prefix(self):
        req = FakeReq()
        ok = append_temp_text_part(req, "没有marker的文本", FakePart, marker=RUNTIME_HINT_MARKER)
        self.assertFalse(ok)
        self.assertEqual(len(req.extra_user_content_parts), 0)

    def test_append_ok_and_dedup_same_request(self):
        req = FakeReq()
        ok = append_temp_text_part(req, RUNTIME_HINT_MARKER + "\nhint", FakePart, marker=RUNTIME_HINT_MARKER)
        self.assertTrue(ok)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        ok2 = append_temp_text_part(req, RUNTIME_HINT_MARKER + "\nhint2", FakePart, marker=RUNTIME_HINT_MARKER)
        self.assertFalse(ok2)
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_factory_none_degrades(self):
        req = FakeReq()
        self.assertFalse(
            append_temp_text_part(req, RUNTIME_HINT_MARKER + "\nh", None, marker=RUNTIME_HINT_MARKER)
        )


class TestRuntimeHint(unittest.TestCase):
    def test_empty_when_no_openers(self):
        from astrbot_plugin_human_chat_quality.runtime_state import SessionState

        self.assertEqual(build_runtime_hint(SessionState(), 600), "")

    def test_hint_starts_with_marker_and_clips(self):
        from astrbot_plugin_human_chat_quality.runtime_state import SessionState

        hint = build_runtime_hint(SessionState(avoid_openers=["好的", "没问题"]), 600)
        self.assertTrue(hint.startswith(RUNTIME_HINT_MARKER))
        self.assertIn("好的", hint)
        self.assertIn("没问题", hint)
        short = build_runtime_hint(SessionState(avoid_openers=["好的"] * 5), 60)
        self.assertLessEqual(len(short), 60)
        self.assertTrue(short.endswith("..."))
        # 裁剪只能从尾部切，marker 位于头部必须保留（append 契约依赖 marker 前缀）
        self.assertTrue(short.startswith(RUNTIME_HINT_MARKER))

    def test_clip_text_edges(self):
        self.assertEqual(clip_text("abc", 0), "")
        self.assertEqual(clip_text("abc", 2), "..")
        self.assertEqual(clip_text("短", 10), "短")
        self.assertTrue(clip_text("很长" * 10, 10).endswith("..."))


if __name__ == "__main__":
    unittest.main()
