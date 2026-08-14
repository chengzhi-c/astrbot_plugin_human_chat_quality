"""quality_rules 模块契约测试：规则注入幂等、legacy 剥离、marker 三态、temp part 契约。

无需宿主 astrbot 即可运行（quality_rules 对 logger 做了 ImportError 防护）。
"""

import unittest
from pathlib import Path

from tests._support import FakePart, FakeReq, V2_RULES_E4AA983, V5_RULES_B46BD0D, ensure_plugin_package

ensure_plugin_package()

from astrbot_plugin_human_chat_quality import quality_rules
from astrbot_plugin_human_chat_quality.quality_rules import (
    LEGACY_STABLE_MARKERS,
    RULES_VERSION,
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    append_temp_text_part,
    build_runtime_hint,
    build_stable_rules,
    rewrite_context_injections,
    rewrite_stable_rules,
)

V1_RULES_74BB884 = (
    "[Human Chat Quality Rules v1]\n"
    "聊天质量约束：\n"
    "- 避免模板腔：不要用“作为 AI”“首先/其次/最后”“总之”“希望这能帮到你”“需要注意的是”等讲义式开头或收尾。\n"
    "- 优先顺着当前语境回答，不主动写成报告、作文、公告或教学材料。\n"
    "- 能短就短；闲聊时自然接话，办事时直接给可执行信息，解释时再展开。\n"
    "- 保留事实准确性，不为了口语化牺牲关键信息、限制条件或安全边界。\n"
    "- 不强行卖萌、不强行情绪化、不复读用户原话，不把每轮对话都总结成段落。"
)
V1_RULES_8BAAE2B = (
    "[Human Chat Quality Rules v1]\n"
    "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
    "一、这是日常聊天，不是写报告。顺着对方的话自然接，别把闲聊答成讲义、作文或客服工单。\n"
    "   ❌“关于这个问题，首先…其次…最后总结一下” ✅ 直接说想说的，该短就一两句。\n"
    "二、别拔高、别升华。不给普通对话强行加意义、加金句、加结尾鼓励。\n"
    "   ❌“希望能帮到你～”“未来可期，一起加油！”“这不仅是…更是…” ✅ 话说完就停，不硬凑收尾。\n"
    "三、别谄媚开场。不用“好问题！”“你说得太对了！”“作为 AI…”这类套话起头，直接回应内容本身。\n"
    "四、别排比凑数、别否定平行。❌“有温度、有深度、有力度”“不是…而是…” ✅ 挑一个具体的说清楚就行。\n"
    "五、别复读对方原话，别每轮都总结。少用“需要注意的是”“值得一提的是”“让我们…”这类铺垫信号词。\n"
    "六、保持事实准确，口语化不等于牺牲关键信息、限制条件或安全边界。\n"
    "七、生成前自查一遍：有没有上面这些 AI 腔？有就地改成人会说的话，别提到这条自查。"
)
V2_RULES_9971F6D = (
    "[Human Chat Quality Rules v2]\n"
    "聊天质量约束（在现有人设语气之上生效，不改变人设的性格、称呼、情绪和口头禅）：\n"
    "这是日常聊天，不是写报告。顺着对方的话自然接，别把闲聊答成讲义、作文或客服工单。\n\n"
    "【铁律】以下任何一条出现都必须改掉：\n"
    '1. 客服式收尾："希望这能帮到你""如果还有问题随时问我"。\n'
    '2. "不是…而是…""不仅…更是…"排比句式。\n'
    '3. "首先…其次…最后""第一…第二…第三"式清单骨架。\n'
    '4. 升华/鼓励式结尾："未来可期""一起加油""让我们共同努力"。\n\n'
    "【词汇】这些词出现即换人话：\n"
    "- 值得注意的是/值得一提的是/需要注意的是 → 直接说内容\n"
    "- 总的来说/综上所述/总而言之 → 直接说结论\n"
    "- 众所周知/不可否认/毋庸置疑 → 直接陈述\n"
    "- 此外/与此同时/由此可见 → 删掉或换“另外/所以”\n"
    "- 深入探讨/深入分析/深度解析 → 聊聊/看看/说说\n"
    "- 赋能/闭环/抓手/沉淀/对齐/颗粒度 → 换大白话\n"
    "- 作为 AI/作为人工智能 → 别强调身份，直接说\n\n"
    "【结构】\n"
    '- 别排比凑数：三个以上相似短语并列（"有温度、有深度、有力度"）→ 挑一个说透\n'
    '- 别破折号连发：一条回复里出现两个以上"——"→ 换逗号句号或删掉\n'
    '- 别自问自答："你可能会问：为什么？" → 直接讲\n'
    "- 别过度条列：日常闲聊尽量不列 1.2.3.\n\n"
    "【沟通】\n"
    '- 别客服腔开场："好问题！""你说得太对了！"→ 直接回应内容\n'
    '- 别复读对方原话、别每轮总结、别铺垫信号词（"需要注意的是""有趣的是""事实上"）\n'
    '- 别免责声明腔："根据我的知识截止日期…"除非真的不确定，那就直接说不确定\n\n'
    "【像个人】\n"
    "- 句长要有变化，全是长句像念稿\n"
    "- 有观点，对事实做反应，别中立地列举利弊\n"
    '- 承认复杂："这事我也说不准"比"从多个维度看"像人话\n'
    '- 不知道就直说："没查过，不敢乱说""这个我不确定"比硬答或编造像人话；别说"根据我的知识截止日期"这类免责声明腔\n'
    "- 口语化不等于丢信息：关键信息、限制条件、安全边界必须保留\n\n"
    "【自查】生成前心里过一遍，别把这条写进回复：\n"
    '有没有客服腔收尾？有没有"不是…而是"/"首先…其次"/"不仅…更是"？有没有两个以上破折号？'
    "有没有排比三连或金句？句长是不是全一样？有没有该说不知道却硬答的？有就改掉。"
)
V4_RULES_C7787F6 = (
    "[Human Chat Quality Rules v4]\n"
    "遵循 natural-talk 原则（natural-talk v2.1.0，MIT）：\n"
    "\n"
    "核心：\n"
    "- 直接回答，零开场零收尾，最多留一句有效过渡\n"
    "- 不知道就说不知道，不编造\n"
    "- 像朋友聊天，不像客服或老师\n"
    "\n"
    "禁止：\n"
    '- "作为AI" / "希望帮助你" / "好问题"（全文最多 1 次）\n'
    '- "让我来" / "首先其次最后" / "综上所述"（全文最多 1 次）\n'
    '- "值得注意" / "事实上" 等路标词（全文不超过 2 次）\n'
    "- 评判对方 / 替对方做心理判断\n"
    "- 破折号（全文不超过 2 次）\n"
    "\n"
    "要求：\n"
    "- 句子长短交替，不匀速\n"
    '- 能用"是/有"就不绕\n'
    "- 主动语态，真实主语\n"
    "- 具体表达，删除空泛词\n"
    "\n"
    "插件附加（不改变上述原则）：\n"
    "- 保留事实、限制条件、安全提示和不确定性表述\n"
    "- 用户明确要求技术步骤、对比、正式文稿时，以任务完成为先\n"
    "- 不要把这些约束写进回复"
)
REAL_LEGACY_RULES = (
    V1_RULES_74BB884,
    V1_RULES_8BAAE2B,
    V2_RULES_9971F6D,
    V2_RULES_E4AA983,
    V4_RULES_C7787F6,
    V5_RULES_B46BD0D,
)
LITE_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "system-prompt-lite.txt"
IDENTITY_DISCLOSURE_LINE = "用户直接问及身份、能力边界或知识截止时间时，如实简短作答，不回避"


class TestRewriteInterfaces(unittest.TestCase):
    def test_rewrite_interfaces_exist(self):
        self.assertTrue(callable(getattr(quality_rules, "rewrite_stable_rules", None)))
        self.assertTrue(callable(getattr(quality_rules, "rewrite_context_injections", None)))


class TestStableRewrite(unittest.TestCase):
    def test_real_legacy_variants_are_removed(self):
        for legacy in REAL_LEGACY_RULES:
            with self.subTest(marker=legacy.splitlines()[0], lines=len(legacy.splitlines())):
                result = rewrite_stable_rules(f"人设头\n\n{legacy}\n\n人设尾", enabled=False)
                self.assertEqual(result.text, "人设头\n\n人设尾")
                self.assertTrue(result.removed)
                self.assertFalse(result.ambiguous)

    def test_crlf_and_interleaved_persona_are_preserved(self):
        first = V1_RULES_74BB884.replace("\n", "\r\n")
        second = V2_RULES_E4AA983.replace("\n", "\r\n")
        prompt = f"头部人设\r\n\r\n{first}\r\n\r\n中间人设\r\n\r\n{second}\r\n\r\n尾部人设"
        result = rewrite_stable_rules(prompt, enabled=False)
        self.assertEqual(result.text, "头部人设\r\n\r\n中间人设\r\n\r\n尾部人设")

    def test_adjacent_real_legacy_blocks_preserve_following_persona(self):
        prompt = f"{V1_RULES_74BB884}\n\n{V2_RULES_E4AA983}\n\n尾部人设"
        result = rewrite_stable_rules(prompt, enabled=False)
        self.assertEqual(result.text, "尾部人设")

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

    def test_unknown_v3_block_preserved_but_does_not_block_injection(self):
        unknown = "[Human Chat Quality Rules v3]\n无法验证的正文"
        prompt = f"人设\n\n{unknown}\n\n尾巴"
        result = rewrite_stable_rules(prompt, enabled=True)
        # v3 块边界未知（无签名可核验），保留不剥离；但不再阻断当前规则注入
        self.assertIn(unknown, result.text)
        self.assertTrue(result.ambiguous)
        self.assertTrue(result.injected)
        self.assertEqual(result.text.count(STABLE_RULE_MARKER), 1)
        # 幂等：二次重写不重复注入
        again = rewrite_stable_rules(result.text, enabled=True)
        self.assertEqual(again.text, result.text)
        self.assertFalse(again.injected)

    def test_unknown_unversioned_rules_are_preserved(self):
        unknown = "[Human Chat Quality Rules]\n无法验证的正文"
        result = rewrite_stable_rules(unknown, enabled=False)
        self.assertEqual(result.text, unknown)
        self.assertTrue(result.ambiguous)
        # 无版本残留块不再阻断注入
        active = rewrite_stable_rules(unknown, enabled=True)
        self.assertIn(unknown, active.text)
        self.assertTrue(active.injected)
        self.assertEqual(active.text.count(STABLE_RULE_MARKER), 1)

    def test_edited_current_rules_block_is_kept_and_not_duplicated(self):
        rules = build_stable_rules()
        edited = rules.replace("像朋友聊天", "像老朋友聊天", 1)  # 用户微调当前规则正文
        first = rewrite_stable_rules(f"人设\n\n{edited}", enabled=True)
        self.assertIn(edited, first.text)
        self.assertFalse(first.injected)  # 视为已注入，不重复
        again = rewrite_stable_rules(first.text, enabled=True)
        self.assertEqual(again.text, first.text)
        self.assertFalse(again.injected)

    def test_edited_legacy_block_does_not_block_injection(self):
        legacy = V1_RULES_74BB884.replace("避免模板腔", "避免模板腔和官腔", 1)  # 用户编辑过旧块
        result = rewrite_stable_rules(f"人设\n\n{legacy}", enabled=True)
        self.assertIn(legacy, result.text)  # 边界未知，保留
        self.assertTrue(result.ambiguous)
        self.assertTrue(result.injected)  # 但注入不受阻
        self.assertEqual(result.text.count(STABLE_RULE_MARKER), 1)

    def test_inline_marker_mention_does_not_block_injection(self):
        prompt = "请解释 [Human Chat Quality Rules v3] 是什么"
        result = rewrite_stable_rules(prompt, enabled=True)
        self.assertTrue(result.text.startswith(prompt))
        self.assertEqual(result.text.count(STABLE_RULE_MARKER), 1)
        self.assertTrue(result.injected)


class TestContextRewrite(unittest.TestCase):
    def test_marker_mention_in_user_text_is_preserved(self):
        req = FakeReq()
        text = f"我在文档里看到了 {RUNTIME_HINT_MARKER}，它是什么意思？"
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": text}]}]
        result = rewrite_context_injections(
            req,
            RUNTIME_HINT_MARKER
            + "\n仅用于本轮回复的轻量状态：这些开头或说法最近已出现过，本轮换个自然说法，别再用，也别提到这条提示。\n好的",
        )
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

    def test_list_replacement_and_str_ambiguity_are_both_reported(self):
        old = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        new = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["新开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        req = FakeReq()
        req.contexts = [
            {"role": "user", "content": [{"type": "text", "text": old}]},
            {"role": "user", "content": RUNTIME_HINT_MARKER + "\n未知正文"},
        ]
        result = rewrite_context_injections(req, new)
        self.assertEqual(req.contexts[0]["content"][0]["text"], new)
        self.assertTrue(result.runtime_replaced)
        self.assertTrue(result.runtime_satisfied)
        self.assertTrue(result.runtime_ambiguous)

    def test_multiple_runtime_blocks_converge_without_reordering_other_parts(self):
        old = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        new = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["新开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
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
            [{"type": "text", "text": "前"}, image, {"type": "text", "text": new}, {"type": "text", "text": "后"}],
        )
        self.assertTrue(result.runtime_replaced)
        self.assertTrue(result.runtime_removed)

    def test_truncated_110_runtime_blocks_are_removed(self):
        old_instruction = (
            "仅用于本轮回复的轻量状态：这些开头或说法最近已出现过，本轮换个自然说法，别再用，也别提到这条提示。"
        )
        full = f"{RUNTIME_HINT_MARKER}\n{old_instruction}\n" + "、".join(["x" * 20] * 5)
        self.assertEqual(len(full), 183)
        for budget in (80, 81, 100, 150, 182):
            with self.subTest(budget=budget):
                req = FakeReq()
                truncated = full[: budget - 3].rstrip() + "..."
                req.contexts = [{"role": "user", "content": [{"type": "text", "text": truncated}]}]
                result = rewrite_context_injections(req, None)
                self.assertEqual(req.contexts[0]["content"], [])
                self.assertTrue(result.runtime_removed)

    def test_matching_runtime_block_is_satisfied_without_rewrite(self):
        target = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["好的"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        part = {"type": "text", "text": target}
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [part]}]
        result = rewrite_context_injections(req, target)
        self.assertIs(req.contexts[0]["content"][0], part)
        self.assertTrue(result.runtime_satisfied)
        self.assertFalse(result.runtime_replaced)
        self.assertFalse(result.runtime_removed)

    def test_known_stable_block_is_removed_from_history(self):
        req = FakeReq()
        req.contexts = [
            {"role": "user", "content": [{"type": "text", "text": V2_RULES_E4AA983}, {"type": "text", "text": "原话"}]}
        ]
        result = rewrite_context_injections(req, None)
        self.assertEqual(req.contexts[0]["content"], [{"type": "text", "text": "原话"}])
        self.assertTrue(result.stable_removed)

    def test_extra_stale_owned_part_is_removed_without_dict(self):
        old = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        new = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["新开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        req = FakeReq()
        req.extra_user_content_parts = [FakePart(old)]
        result = rewrite_context_injections(req, new)
        self.assertTrue(all(not isinstance(part, dict) for part in req.extra_user_content_parts))
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertTrue(result.runtime_removed)
        self.assertFalse(result.runtime_satisfied)
        self.assertFalse(result.runtime_replaced)

    def test_extra_matching_owned_part_is_kept_as_same_object(self):
        target = build_runtime_hint(
            quality_rules.SessionState(avoid_openers=["好的"]), quality_rules.MAX_RUNTIME_HINT_CHARS
        )
        part = FakePart(target)
        req = FakeReq()
        req.extra_user_content_parts = [part]
        result = rewrite_context_injections(req, target)
        self.assertIs(req.extra_user_content_parts[0], part)
        self.assertTrue(result.runtime_satisfied)
        self.assertFalse(result.runtime_replaced)
        self.assertFalse(result.runtime_removed)


class TestStableRules(unittest.TestCase):
    def test_marker_current_and_legacy(self):
        self.assertIn(f"Rules v{RULES_VERSION}]", STABLE_RULE_MARKER)
        legacy_text = "|".join(LEGACY_STABLE_MARKERS)
        self.assertIn("Rules]", legacy_text)
        for i in range(1, RULES_VERSION):
            self.assertIn(f"Rules v{i}]", legacy_text)
        # 推导集合不含当前版本（升级 v4→v5 时本断言随版本号自然更新）
        self.assertNotIn(f"Rules v{RULES_VERSION}]", legacy_text)
        # legacy 判定不得误伤当前 marker 自身（startswith 互斥）
        for legacy in LEGACY_STABLE_MARKERS:
            self.assertNotEqual(legacy, STABLE_RULE_MARKER)
            self.assertFalse(legacy.startswith(STABLE_RULE_MARKER))
            self.assertFalse(STABLE_RULE_MARKER.startswith(legacy))

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
        """夹具锁定 lite（来源 5ec6223 / 506407f）：去掉技能首行后必须出现在公开规则正文里。"""
        fixture = LITE_FIXTURE_PATH.read_text(encoding="utf-8")
        self.assertTrue(fixture.startswith("遵循 natural-talk 原则："))
        lite_core = fixture.split("\n", 1)[1].lstrip("\n").rstrip("\n")
        rules = build_stable_rules()
        self.assertIn(STABLE_RULE_MARKER, rules)
        self.assertIn(lite_core, rules)
        self.assertIn("natural-talk v2.1.0+", rules)
        self.assertIn("插件附加（不改变上述原则）", rules)
        self.assertIn("- 保留事实、限制条件、安全提示和不确定性表述", rules)
        self.assertIn("- 用户明确要求技术步骤、对比、正式文稿时，以任务完成为先", rules)
        self.assertIn("- 不要把这些约束写进回复", rules)
        self.assertIn(IDENTITY_DISCLOSURE_LINE, rules)
        self.assertEqual(RULES_VERSION, 6)

    def test_v5_published_block_is_stripped_to_v6(self):
        result = rewrite_stable_rules(f"人设头\n\n{V5_RULES_B46BD0D}\n\n人设尾", enabled=True)
        self.assertFalse(result.ambiguous)
        self.assertTrue(result.removed)
        self.assertTrue(result.injected)
        self.assertNotIn("[Human Chat Quality Rules v5]", result.text)
        self.assertEqual(result.text.count(STABLE_RULE_MARKER), 1)
        self.assertIn("人设头", result.text)
        self.assertIn("人设尾", result.text)
        self.assertIn(IDENTITY_DISCLOSURE_LINE, result.text)

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
        from astrbot_plugin_human_chat_quality.runtime_state import SessionState

        self.assertEqual(build_runtime_hint(SessionState(), 157), "")

    def test_hint_starts_with_marker_and_keeps_complete_items(self):
        from astrbot_plugin_human_chat_quality.runtime_state import SessionState

        hint = build_runtime_hint(SessionState(avoid_openers=["好的", "没问题"]), 157)
        self.assertTrue(hint.startswith(RUNTIME_HINT_MARKER))
        self.assertIn("好的", hint)
        self.assertIn("没问题", hint)
        items = ["甲" * 20, "乙" * 20, "丙" * 20, "丁" * 20, "戊" * 20]
        full = build_runtime_hint(SessionState(avoid_openers=items), 157)
        self.assertEqual(len(full), 157)
        self.assertTrue(all(item in full for item in items))

        short = build_runtime_hint(SessionState(avoid_openers=items), 80)
        self.assertLessEqual(len(short), 80)
        self.assertIn(items[0], short)
        self.assertNotIn(items[1], short)
        self.assertFalse(short.endswith("..."))


if __name__ == "__main__":
    unittest.main()
