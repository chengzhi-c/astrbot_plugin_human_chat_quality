"""Host-independent Core behavior and configuration contracts."""

import asyncio
import json
import os
import unittest

from tests._support import (
    V2_RULES_E4AA983,
    V5_RULES_B46BD0D,
    FakePart,
    FakeReq,
    ensure_plugin_package,
    temporary_directory,
)

ensure_plugin_package()

from astrbot_plugin_human_chat_quality.constants import MAX_RUNTIME_HINT_CHARS
from astrbot_plugin_human_chat_quality.core import AppConfig, HumanChatQualityCore
from astrbot_plugin_human_chat_quality.quality_rules import (
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    build_runtime_hint,
)
from astrbot_plugin_human_chat_quality.runtime_state import RuntimeStateStore


class FakeEvent:
    def __init__(self, origin, text=""):
        self.unified_msg_origin = origin
        self.text = text


class FakeLLMResp:
    def __init__(self, text):
        self.completion_text = text
        self.result_chain = None


class TestCoreFlow(unittest.TestCase):
    def setUp(self):
        self.dir = temporary_directory(self)
        self.store = RuntimeStateStore(os.path.join(self.dir, "s.json"), 14, 8, ())
        self.core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=FakePart)
        self.ev = FakeEvent("aiocqhttp:GroupMessage:111")

    def test_legacy_block_is_preserved_as_ordinary_text(self):
        """3.0.0 破坏性变更：旧规则块不再被剥离，按普通文本保留。"""
        req = FakeReq()
        req.system_prompt = f"原人设：你是XX\n\n{V5_RULES_B46BD0D}"
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertIn("[Human Chat Quality Rules v5]", req.system_prompt)
        self.assertEqual(req.system_prompt.count(STABLE_RULE_MARKER), 1)
        self.assertTrue(req.system_prompt.startswith("原人设：你是XX"))

    def test_current_stable_rules_injected(self):
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": "在吗"}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertEqual(req.system_prompt.count(STABLE_RULE_MARKER), 1)
        self.assertIn("natural-talk", req.system_prompt)

    def test_legacy_v2_block_preserved_in_history(self):
        """3.0.0：旧规则块在历史中按普通文本保留，不再清扫。"""
        req = FakeReq()
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "原话"},
                    {"type": "text", "text": V2_RULES_E4AA983},
                ],
            }
        ]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        texts = [p.get("text", "") for ctx in req.contexts for p in ctx["content"]]
        self.assertEqual(texts, ["原话", V2_RULES_E4AA983])  # 用户原话与旧块都保留
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)  # 稳定规则正常注入

    def test_no_hint_first_round_then_hint_after_three_repeats(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn(RUNTIME_HINT_MARKER, req.extra_user_content_parts[0].text)
        self.assertIn("好的", req.extra_user_content_parts[0].text)

    def test_stale_extra_part_is_replaced_by_factory_product_not_dict(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        old_hint = build_runtime_hint(["旧开头"], MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.extra_user_content_parts = [FakePart(old_hint)]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIsInstance(part, FakePart)
        self.assertFalse(isinstance(part, dict))
        self.assertIn("好的", part.text)
        self.assertNotIn("旧开头", part.text)

    def test_replace_in_history_no_accumulation(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        req = FakeReq()
        old_hint = build_runtime_hint(
            self.store.get(self.ev.unified_msg_origin).avoid_openers, MAX_RUNTIME_HINT_CHARS
        ).replace("好的", "旧开头")
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": old_hint}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn("好的", req.extra_user_content_parts[0].text)
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertNotIn("旧开头", json.dumps(req.contexts, ensure_ascii=False))

    def test_global_off_no_inject(self):
        core_off = HumanChatQualityCore(
            AppConfig.from_config({"enabled": False}), self.store, text_part_factory=FakePart
        )
        req = FakeReq()
        asyncio.run(core_off.on_llm_request(self.ev, req))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_blacklist_hit_and_miss(self):
        core = HumanChatQualityCore(
            AppConfig.from_config({"disabled_sessions": ["222"]}), self.store, text_part_factory=FakePart
        )
        req_hit = FakeReq()
        asyncio.run(core.on_llm_request(FakeEvent("aiocqhttp:GroupMessage:222"), req_hit))
        self.assertNotIn(STABLE_RULE_MARKER, req_hit.system_prompt)
        req_miss = FakeReq()
        asyncio.run(core.on_llm_request(FakeEvent("aiocqhttp:GroupMessage:333"), req_miss))
        self.assertIn(STABLE_RULE_MARKER, req_miss.system_prompt)

    def test_status_text_active_and_inactive(self):
        text_active = self.core.status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("启用", text_active)
        self.assertIn("下一轮避用：无（尚未形成重复或套话信号）", text_active)
        self.assertNotIn("下一轮请求会带上动态提醒", text_active)
        core_off = HumanChatQualityCore(
            AppConfig.from_config({"enabled": False}), self.store, text_part_factory=FakePart
        )
        self.assertIn("关闭", core_off.status_text(self.ev.unified_msg_origin, self.ev))

    def test_status_text_names_next_round_hint(self):
        for _ in range(3):
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        text = self.core.status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("下一轮避用：好的", text)
        self.assertIn("下一轮请求会带上动态提醒", text)
        core_no_hint = HumanChatQualityCore(
            AppConfig.from_config({"inject_runtime_state": False}), self.store, text_part_factory=FakePart
        )
        quiet = core_no_hint.status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("下一轮避用：好的", quiet)
        self.assertNotIn("下一轮请求会带上动态提醒", quiet)

    def test_status_distinguishes_global_static_and_session_disable(self):
        global_off = HumanChatQualityCore(
            AppConfig.from_config({"enabled": False}), self.store, text_part_factory=FakePart
        ).status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("全局配置：关闭", global_off)

        static_off = HumanChatQualityCore(
            AppConfig.from_config({"disabled_sessions": ["111"]}), self.store, text_part_factory=FakePart
        ).status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("配置静态禁用", static_off)

        asyncio.run(self.core.set_session_enabled(self.ev.unified_msg_origin, False))
        session_off = self.core.status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("/humanq off", session_off)

    def test_status_reports_runtime_capability_and_invalid_config_summary(self):
        store = RuntimeStateStore(self.dir + "-status.json", 14, 8, ["", "词", "词", "x" * 21])
        core = HumanChatQualityCore(AppConfig.from_config(None), store, text_part_factory=None)
        text = core.status_text(self.ev.unified_msg_origin, self.ev)
        self.assertIn("宿主临时文本部件不可用", text)
        self.assertIn("配置忽略：3 项", text)
        self.assertIn("空值 1 项、重复 1 项、过长 1 项", text)
        self.assertNotIn("x" * 21, text)


if __name__ == "__main__":
    unittest.main()
