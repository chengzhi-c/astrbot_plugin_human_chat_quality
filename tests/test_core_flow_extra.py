"""Host-independent Core cleanup, counting, and response-recording contracts."""

import asyncio
import os
import unittest
from unittest import mock

from tests._support import (
    FakeEvent,
    FakeLLMResp,
    FakePart,
    FakeReq,
    ensure_plugin_package,
    temporary_directory,
)

ensure_plugin_package()

from astrbot_plugin_human_chat_quality import core as core_module
from astrbot_plugin_human_chat_quality import quality_rules
from astrbot_plugin_human_chat_quality.constants import MAX_AVOID_ITEMS, MAX_RUNTIME_HINT_CHARS
from astrbot_plugin_human_chat_quality.core import AppConfig, HumanChatQualityCore
from astrbot_plugin_human_chat_quality.quality_rules import (
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    build_runtime_hint,
    build_stable_rules,
)
from astrbot_plugin_human_chat_quality.runtime_state import RuntimeStateStore, SessionState


class TestCoreFlowExtra(unittest.TestCase):
    """Core cleanup, counting, and response-recording contracts."""

    def setUp(self):
        self.dir = temporary_directory(self)
        self.store = RuntimeStateStore(os.path.join(self.dir, "s.json"), 14, 8, ())
        self.core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=FakePart)
        self.ev = FakeEvent("aiocqhttp:GroupMessage:111")

    def test_long_rendered_signal_does_not_silence_runtime_hints(self):
        """回归锁：含最长模型端指令的信号不得让动态提醒永久静默。

        历史缺陷——「空转提示语」的指令 22 字 > MAX_AVOID_ITEM_LEN(20)，
        其注入块被 _is_complete_runtime 判 ambiguous，成为历史里永不清理的孤儿块。
        """
        origin = self.ev.unified_msg_origin
        self.store.sessions[origin] = SessionState(avoid_openers=["空转提示语"])

        first = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, first))
        self.assertEqual(len(first.extra_user_content_parts), 1, "首轮应注入动态提醒")
        injected = first.extra_user_content_parts[0].text

        # 宿主把注入块写回历史后回流（真实调用链：extra parts → 同一条 user message → 落库）
        history = {"role": "user", "content": [{"type": "text", "text": "你好"}, {"type": "text", "text": injected}]}
        second = FakeReq()
        second.contexts = [history]
        asyncio.run(self.core.on_llm_request(self.ev, second))
        self.assertEqual(len(second.extra_user_content_parts), 1, "历史回流后仍应继续注入动态提醒")

    def test_orphan_block_does_not_block_new_injection(self):
        """孤儿块（无法核验的自家旧块）只影响清理、不影响注入。

        回归目标——老用户历史里可能已有旧版本写入、当前判定为 ambiguous 的提示块
        （如 3.10.0 的 extract_opener 会产出含顿号的项）。旧实现把这类块当成"已有提醒"，
        此后每轮都拒绝注入，而该块按设计永不被清理 → 该会话动态提醒永久静默。
        """
        origin = self.ev.unified_msg_origin
        self.store.sessions[origin] = SessionState(avoid_openers=["作为AI"])
        orphan_block = quality_rules.render_runtime_hint(["你好呀、然后再说"] * 3)
        self.assertEqual(quality_rules._runtime_kind(orphan_block), "ambiguous", "前置条件：构造出孤儿块")

        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": orphan_block}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(len(req.extra_user_content_parts), 1, "孤儿块存在时仍须注入本轮提醒")
        # 孤儿块本身仍按设计保留（不误删用户可能手写的文本）
        self.assertTrue(req.contexts[0]["content"], "孤儿块不应被清理")

    def test_overlong_custom_cliche_filtered_end_to_end(self):
        # 超长自定义词在 Store 构造期被过滤，Core 全流程不入 avoid_openers
        store = RuntimeStateStore(os.path.join(self.dir, "s2.json"), 14, 8, ("x" * 21,))
        core = HumanChatQualityCore(AppConfig.from_config(None), store, text_part_factory=FakePart)
        asyncio.run(core.on_llm_response(self.ev, FakeLLMResp("这是" + "x" * 21 + "的回复")))
        self.assertEqual(store.get(self.ev.unified_msg_origin).avoid_openers, [])

    def test_session_off_stops_inject_and_record(self):
        asyncio.run(self.core.set_session_enabled(self.ev.unified_msg_origin, False))
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        self.assertEqual(self.store.get(self.ev.unified_msg_origin).recent_openers, [])

    def _request_with_owned_blocks(self):
        req = FakeReq()
        req.system_prompt = f"原人设\n\n{build_stable_rules()}"
        runtime = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": runtime}]}]
        return req

    def test_global_off_cleans_owned_history_without_counting_injection(self):
        core = HumanChatQualityCore(AppConfig.from_config({"enabled": False}), self.store, text_part_factory=FakePart)
        req = self._request_with_owned_blocks()
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.system_prompt, "原人设")
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(core.stats.total_injections, 0)

    def test_static_disabled_session_cleans_owned_history(self):
        core = HumanChatQualityCore(
            AppConfig.from_config({"disabled_sessions": ["111"]}), self.store, text_part_factory=FakePart
        )
        req = self._request_with_owned_blocks()
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.system_prompt, "原人设")
        self.assertEqual(req.contexts[0]["content"], [])

    def test_session_off_cleans_owned_history(self):
        asyncio.run(self.core.set_session_enabled(self.ev.unified_msg_origin, False))
        req = self._request_with_owned_blocks()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(req.system_prompt, "原人设")
        self.assertEqual(req.contexts[0]["content"], [])

    def test_no_origin_cleans_owned_history_without_state_or_injection(self):
        req = self._request_with_owned_blocks()
        asyncio.run(self.core.on_llm_request(FakeEvent(""), req))
        self.assertEqual(req.system_prompt, "原人设")
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.core.stats.total_injections, 0)

    def test_runtime_config_off_removes_runtime_but_keeps_stable_rules(self):
        core = HumanChatQualityCore(
            AppConfig.from_config({"inject_runtime_state": False}), self.store, text_part_factory=FakePart
        )
        req = self._request_with_owned_blocks()
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_stable_config_off_removes_stable_rules_but_runtime_stays_active(self):
        asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答一"))
        asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答二"))
        asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答三"))
        core = HumanChatQualityCore(
            AppConfig.from_config({"inject_stable_rules": False}), self.store, text_part_factory=FakePart
        )
        req = self._request_with_owned_blocks()
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.system_prompt, "原人设")
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn(RUNTIME_HINT_MARKER, req.extra_user_content_parts[0].text)

    def test_missing_text_part_factory_still_cleans_history_without_fake_part(self):
        asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答一"))
        asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答二"))
        asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答三"))
        core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=None)
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": "在吗"}]}]
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.contexts[0]["content"], [{"type": "text", "text": "在吗"}])
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_missing_text_part_factory_removes_existing_runtime_hint(self):
        for _ in range(3):
            asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答"))
        core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=None)
        old_hint = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": old_hint}]}]

        asyncio.run(core.on_llm_request(self.ev, req))

        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(req.extra_user_content_parts, [])

    def test_total_injections_only_real_injections(self):
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(self.core.stats.total_injections, 1)
        # 幂等轮：同一 req 已含规则、无 hint → 不注入不计
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(self.core.stats.total_injections, 1)
        # 仅移除历史旧块 → 不计注入
        req2 = FakeReq()
        req2.system_prompt = req.system_prompt
        req2.contexts = [{"role": "user", "content": [{"type": "text", "text": RUNTIME_HINT_MARKER + "\n旧"}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req2))
        self.assertEqual(self.core.stats.total_injections, 1)

    def test_response_signals_are_detected_once(self):
        core_detect = core_module.detect_cliches
        with mock.patch.object(core_module, "detect_cliches", wraps=core_detect) as core_mock:
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好问题，回答")))

        # 检测只在 core 发生一次；store 只做合并，不再有任何检测调用
        self.assertEqual(core_mock.call_count, 1)

    def test_stats_count_new_avoid_items_only(self):
        """避用项统计只计新增：同词重复命中不再累计（防停留多轮重复膨胀）。"""
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("我完全理解你的感受。")))
        self.assertEqual(self.core.stats.avoid_openers_seen, 1)
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("我完全理解你的感受。再来一次")))
        self.assertEqual(self.core.stats.avoid_openers_seen, 1)

    def test_cleanup_stats_count_all_removed_blocks(self):
        runtime = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq(system_prompt=f"{build_stable_rules()}\n\n{build_stable_rules()}")
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": runtime},
                    {"type": "text", "text": runtime},
                ],
            }
        ]

        asyncio.run(self.core.on_llm_request(FakeEvent(""), req))

        self.assertEqual(self.core.stats.legacy_blocks_removed, 2)
        self.assertEqual(self.core.stats.stale_hints_removed, 2)

    def test_runtime_hint_missed_counter_removed(self):
        """P1 删链路后：QualityStats 不再有 missed 字段（防回归再引入）。"""
        self.assertFalse(hasattr(self.core.stats, "runtime_hint_missed"))

    def test_runtime_hint_budget_fits_only_prefix_items(self):
        first = "第一项第一项第一项第一项第一项"
        second = "第二项第二项第二项第二项第二项"
        self.store.sessions[self.ev.unified_msg_origin] = SessionState(avoid_openers=[first, second])
        core = HumanChatQualityCore(
            AppConfig.from_config({"max_runtime_hint_chars": 80}), self.store, text_part_factory=FakePart
        )
        req = FakeReq()

        asyncio.run(core.on_llm_request(self.ev, req))

        self.assertIn(first, req.extra_user_content_parts[0].text)
        self.assertNotIn(second, req.extra_user_content_parts[0].text)

    def test_formal_writing_with_emotion_topic_skips_request_and_response(self):
        event = FakeEvent(self.ev.unified_msg_origin, "写一篇关于抑郁的论文")
        req = FakeReq()

        asyncio.run(self.core.on_llm_request(event, req))
        asyncio.run(self.core.on_llm_response(event, FakeLLMResp("好的，论文草稿")))

        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertNotIn(event.unified_msg_origin, self.store.sessions)

    def test_discussing_a_formal_topic_keeps_chat_quality_active(self):
        event = FakeEvent(self.ev.unified_msg_origin, "分析一下关于抑郁论文的观点")
        req = FakeReq()

        asyncio.run(self.core.on_llm_request(event, req))

        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_casual_notice_to_a_friend_stays_active(self):
        event = FakeEvent(self.ev.unified_msg_origin, "写个通知给我朋友今晚聚餐")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(event, req))
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_short_formal_requests_yield(self):
        prompts = [
            "帮我写个通知",
            "写一份通知",
            "拟一份会议纪要",
            # 量词全覆盖（旧实现只认「写个/写一份」，其余漏让位）
            "写份通知",
            "写一封通知",
            "写一篇通知",
            "拟个通知",
            "拟一份通知",
            "拟一篇通知",
            "起草通知",
            "拟定通知",
            "撰写通知",
            "写个周报",
            "写份周报",
            "写个日报",
            "写份日报",
            "写个公告",
            "拟个公告",
            "起草公告",
            "写个汇报",
            "写份汇报",
            "帮我写一份汇报",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                event = FakeEvent(self.ev.unified_msg_origin, prompt)
                req = FakeReq()
                asyncio.run(self.core.on_llm_request(event, req))
                self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_technical_and_private_cases_do_not_yield(self):
        """让位扩展的护栏：技术系统与私下叮嘱不得被正式规则接管。"""
        prompts = [
            "帮我写一个合同管理系统的表结构",
            "写一个论文查重算法的Python实现",
            "写一段公文流转系统的审批流代码",
            "写个合同系统的数据库设计",
            "写个通知推送的代码",
            "写个通知队列的表结构",
            "写个通知服务的脚本",
            "写个通知查询接口",
            "写个通知数据表结构",
            "写个公告组件的接口",
            "写个公告组件的样式",
            "写个周报汇总的服务",
            "写个通知系统",
            "写个通知给我朋友今晚聚餐",
            "写个通知发到同学群里",
            "写个公告告诉家人今晚不用等我",
            "拟一份方案",
            "通知一下大家",
            "这个通知是什么意思",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                event = FakeEvent(self.ev.unified_msg_origin, prompt)
                req = FakeReq()
                asyncio.run(self.core.on_llm_request(event, req))
                self.assertIn(STABLE_RULE_MARKER, req.system_prompt, f"技术/私域场景被误让位: {prompt}")

    def test_formal_artifacts_with_explanatory_verbs_still_yield(self):
        """反向护栏：正式产物的表述里带传达类动词时仍须让位，不得被技术排除吞掉。"""
        prompts = [
            "写个公告说明服务下线",
            "帮我写份通知说明服务变更",
            "写份通知说明系统升级",
            "写个公告说明服务恢复时间",
            "拟一份通知告知服务暂停",
            "写一份通知说明代码规范",
            "写个公告介绍新功能",
            "写个通知解释一下延迟原因",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                event = FakeEvent(self.ev.unified_msg_origin, prompt)
                req = FakeReq()
                asyncio.run(self.core.on_llm_request(event, req))
                self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt, f"正式文稿被误判为技术件: {prompt}")

    def test_edit_a_bit_marketing_copy_stays_active(self):
        """「改一下」不进动作表，避免误伤粘性口令「再改一下」。"""
        event = FakeEvent(self.ev.unified_msg_origin, "帮我改一下营销文案")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(event, req))
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_bare_sticky_phrase_does_not_yield_without_writing_context(self):
        event = FakeEvent(self.ev.unified_msg_origin, "再改一下")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(event, req))
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_revise_official_document_yields(self):
        event = FakeEvent(self.ev.unified_msg_origin, "帮我改这篇公文")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(event, req))
        asyncio.run(self.core.on_llm_response(event, FakeLLMResp("好的，公文草稿")))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertNotIn(event.unified_msg_origin, self.store.sessions)
        self.assertIn("正式写作场景让位", self.core.status_text(event.unified_msg_origin, event))

    def test_user_story_stays_active_until_a_real_genre_appears(self):
        for prompt in ("写个用户故事", "帮我写一条 user story"):
            with self.subTest(prompt=prompt):
                event = FakeEvent(self.ev.unified_msg_origin, prompt)
                req = FakeReq()
                asyncio.run(self.core.on_llm_request(event, req))
                self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
        novel = FakeReq()
        asyncio.run(self.core.on_llm_request(FakeEvent(self.ev.unified_msg_origin, "写个用户故事，再写个小说"), novel))
        self.assertNotIn(STABLE_RULE_MARKER, novel.system_prompt)

    def test_reply_does_not_rewrite_yield_kind(self):
        origin = self.ev.unified_msg_origin
        ask = FakeEvent(origin, "帮我写个通知")
        asyncio.run(self.core.on_llm_request(ask, FakeReq()))
        asyncio.run(self.core.on_llm_response(FakeEvent(origin, "写个故事"), FakeLLMResp("好的，我来写个故事")))
        follow = FakeEvent(origin, "继续")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(follow, req))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertIn("正式写作场景让位", self.core.status_text(origin, follow))
        self.assertNotIn("创作场景让位", self.core.status_text(origin, follow))

    def test_roleplay_request_yields_without_injecting_fiction_rules(self):
        event = FakeEvent(self.ev.unified_msg_origin, "写一段角色扮演")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(event, req))
        asyncio.run(self.core.on_llm_response(event, FakeLLMResp("好的，我来扮演")))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertNotIn("fiction", req.system_prompt.lower())
        self.assertNotIn(event.unified_msg_origin, self.store.sessions)
        self.assertIn("创作场景让位", self.core.status_text(event.unified_msg_origin, event))

    def test_mentioning_a_novel_without_writing_intent_stays_active(self):
        event = FakeEvent(self.ev.unified_msg_origin, "今天看了本小说")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(event, req))
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_sticky_yield_keeps_followup_continue_from_injecting(self):
        origin = self.ev.unified_msg_origin
        asyncio.run(self.core.on_llm_request(FakeEvent(origin, "帮我起草正式通知"), FakeReq()))
        follow = FakeEvent(origin, "继续")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(follow, req))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertIn("正式写作场景让位", self.core.status_text(origin, follow))

        write_again = FakeReq()
        asyncio.run(self.core.on_llm_request(FakeEvent(origin, "继续写通知"), write_again))
        self.assertNotIn(STABLE_RULE_MARKER, write_again.system_prompt)

        chat = FakeReq()
        asyncio.run(self.core.on_llm_request(FakeEvent(origin, "那吃饭呢"), chat))
        self.assertIn(STABLE_RULE_MARKER, chat.system_prompt)

    def test_sticky_yield_expires_and_ignores_casual_then(self):
        origin = self.ev.unified_msg_origin
        now = {"value": 0.0}
        time_stub = mock.Mock()
        time_stub.monotonic.side_effect = lambda: now["value"]
        with (
            mock.patch.object(core_module, "YIELD_STICKY_TTL_SECONDS", 10, create=True),
            mock.patch.object(core_module, "time", time_stub),
        ):
            asyncio.run(self.core.on_llm_request(FakeEvent(origin, "帮我起草正式通知"), FakeReq()))
            now["value"] = 11.0
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(FakeEvent(origin, "继续"), req))
            self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

        casual = FakeReq()
        asyncio.run(self.core.on_llm_request(FakeEvent(origin, "帮我起草正式通知"), FakeReq()))
        asyncio.run(self.core.on_llm_request(FakeEvent(origin, "然后呢"), casual))
        self.assertIn(STABLE_RULE_MARKER, casual.system_prompt)

    def test_runtime_hint_prefers_reliability_signals_when_budget_is_tight(self):
        appearance = "第一项第一项第一项第一项第一项"
        harmful = "第二项第二项第二项第二项第二项"
        self.store.sessions[self.ev.unified_msg_origin] = SessionState(avoid_openers=[appearance, harmful])
        core = HumanChatQualityCore(
            AppConfig.from_config({"max_runtime_hint_chars": 80}), self.store, text_part_factory=FakePart
        )
        req = FakeReq()
        with mock.patch.object(core_module, "signal_priority", side_effect=lambda name: 1 if name == harmful else 2):
            asyncio.run(core.on_llm_request(self.ev, req))
        hint = req.extra_user_content_parts[0].text
        self.assertIn(harmful, hint)
        self.assertNotIn(appearance, hint)

    def test_runtime_hint_renders_aggregate_signal_by_canonical_name(self):
        self.store.sessions[self.ev.unified_msg_origin] = SessionState(avoid_openers=["破折号"])
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertIn("别用破折号", req.extra_user_content_parts[0].text)

    def test_reliability_signals_survive_admission_truncation(self):
        """阶位保名额：同轮命中超过 MAX_AVOID_ITEMS 时，档 1 不得被档 2 挤出。

        回归目标——旧实现按 detect_cliches 的分层返回顺序截断，末位的档 1 信号会被丢弃，
        而注入侧排序发生在截断之后，救不回来。
        """
        reply = (
            "好问题，让我来梳理。说白了，我直接说。作为AI，我需要说明边界。"
            "你说得太对了。有研究表明这样更快。专家指出另一个方向。"
            "综合来看，希望能帮到你。"
        )
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp(reply)))
        admitted = self.store.get(self.ev.unified_msg_origin).avoid_openers

        self.assertEqual(len(admitted), MAX_AVOID_ITEMS)
        for signal in ("作为AI", "你说得太对了", "有研究表明", "专家指出"):
            with self.subTest(signal=signal):
                self.assertIn(signal, admitted, f"档 1 信号 {signal} 被挤出名额")
        dropped = [s for s in ("希望能帮到你", "好问题") if s not in admitted]
        self.assertTrue(dropped, "本用例需要一个档 2 信号被挤出以证明排序生效")

    def test_detected_signals_win_slots_against_repeated_openers(self):
        """重复开头不得挤占当轮检测信号的名额。

        回归目标——旧实现把 repeated 排在 detected 之前合并，窗口一满（如 window=50 攒出 5 个重复开头），
        当轮命中的档 1 可靠性信号会被整体挤出 avoid_openers，下一轮提示里只剩口头语。
        """
        store = RuntimeStateStore(os.path.join(self.dir, "b50.json"), 14, 50, ())
        core = HumanChatQualityCore(AppConfig.from_config(None), store, text_part_factory=FakePart)
        origin = "aiocqhttp:GroupMessage:222"

        # 攒满 5 个重复开头（各 3 次，达到 OPENER_REPEAT_THRESHOLD）
        for index in range(15):
            opener = ["确实", "当然", "哈哈", "对的", "没错"][index % 5]
            asyncio.run(core.on_llm_response(FakeEvent(origin), FakeLLMResp(f"{opener}，回答{index}。")))
        self.assertEqual(len(store.get(origin).avoid_openers), MAX_AVOID_ITEMS, "前置条件：名额已被重复开头占满")

        asyncio.run(core.on_llm_response(FakeEvent(origin), FakeLLMResp("作为AI，我不确定边界在哪。")))
        admitted = store.get(origin).avoid_openers
        self.assertIn("作为AI", admitted, "档 1 可靠性信号被重复开头挤出名额")

    def test_each_signal_family_gets_its_own_hint(self):
        """提示指向病灶：各类异质病灶得到各自的修改指令，不能共用一条。"""
        samples = {
            "不是优化而是重构。": "别用“不是A而是B”式对比",
            "这不仅是优化，更是对工程的追求。": "别在结尾升大命题",
            "核心是：提高代码质量。": "删掉“核心是：”这类空转提示语",
            "他的答案是——那就是缓存。": "别用破折号制造揭晓",
        }
        for index, (text, expected) in enumerate(samples.items()):
            with self.subTest(text=text):
                store = RuntimeStateStore(os.path.join(self.dir, f"hint-{index}.json"), 14, 8, ())
                core = HumanChatQualityCore(AppConfig.from_config(None), store, text_part_factory=FakePart)
                asyncio.run(core.on_llm_response(self.ev, FakeLLMResp(text)))
                req = FakeReq()
                asyncio.run(core.on_llm_request(self.ev, req))
                self.assertEqual(len(req.extra_user_content_parts), 1)
                self.assertIn(expected, req.extra_user_content_parts[0].text)

    def test_yield_sessions_are_bounded_by_cap(self):
        with mock.patch.object(core_module, "PENDING_SESSION_CAP", 4, create=True):
            for index in range(10):
                self.core._yield_reason(f"yield-{index}", FakeEvent("x", "请起草一份正式通知"), update=True)
            self.assertLessEqual(len(self.core._pending_yield), 4)

    def test_no_origin_skips_everything(self):
        ev = FakeEvent("")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(ev, req))
        self.assertEqual(req.system_prompt, "原人设：你是XX")
        asyncio.run(self.core.on_llm_response(ev, FakeLLMResp("好的，回答")))
        self.assertEqual(self.store.sessions, {})

    def test_unknown_stable_string_is_removed_from_history(self):
        req = FakeReq()
        req.contexts = [{"role": "user", "content": "[Human Chat Quality Rules v2]\n旧规则块"}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(req.contexts[0]["content"], "")

    def test_debug_log_reports_ambiguous_owned_markers_kept(self):
        core = HumanChatQualityCore(AppConfig.from_config({"debug_log": True}), self.store, text_part_factory=FakePart)
        req = FakeReq()
        req.system_prompt = "[Human Chat Quality Rules v3]\n未知规则"
        req.contexts = [{"role": "user", "content": RUNTIME_HINT_MARKER + "\n未知提示"}]
        with self.assertLogs("astrbot_plugin_human_chat_quality.core", level="DEBUG") as logs:
            asyncio.run(core.on_llm_request(self.ev, req))
        self.assertTrue(any("ambiguous" in line for line in logs.output))

    def test_command_result_and_status_expose_pending_persistence(self):
        async def run():
            with mock.patch.object(self.store, "_write_snapshot_sync", side_effect=OSError("disk full")):
                result = await self.core.set_session_enabled(self.ev.unified_msg_origin, False)
            self.assertFalse(result)
            self.assertFalse(self.store.is_enabled(self.ev.unified_msg_origin))
            self.assertIn("待重试", self.core.status_text(self.ev.unified_msg_origin, self.ev))

        asyncio.run(run())


class TestEventTextProbing(unittest.TestCase):
    """宿主对象形状探测链与粘性口令长度上限（此前无覆盖）。"""

    def test_event_text_prefers_callable_message_str(self):
        class Ev:
            unified_msg_origin = "x"

            def get_message_str(self):
                return "  有内容  "

        from astrbot_plugin_human_chat_quality.scene_guard import event_text

        self.assertEqual(event_text(Ev()), "有内容")

    def test_event_text_skips_raising_attribute(self):
        class Ev:
            unified_msg_origin = "x"

            @property
            def message_str(self):
                raise RuntimeError("host shape drift")

            message = "后备文本"

        from astrbot_plugin_human_chat_quality.scene_guard import event_text

        self.assertEqual(event_text(Ev()), "后备文本")

    def test_sticky_followup_rejects_overlong_input(self):
        from astrbot_plugin_human_chat_quality.scene_guard import is_sticky_followup

        self.assertFalse(is_sticky_followup("继续" + "啊" * 30))
        self.assertTrue(is_sticky_followup("继续"))


if __name__ == "__main__":
    unittest.main()
