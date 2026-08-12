"""main 模块流程测试：稳定规则注入、legacy 历史清扫、Core 全流程、配置解析。

依赖宿主 astrbot API（main 顶层导入）；宿主缺失或版本不兼容时整体跳过。
"""

import asyncio
import json
import os
import tempfile
import unittest

import sys
from pathlib import Path

# 插件目录本身即包根目录（astrbot_plugin_human_chat_quality），
# 需把仓库根目录的父目录加入 sys.path 才能以包方式导入（仓库可位于任意路径）
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

try:
    from astrbot_plugin_human_chat_quality.main import AppConfig, HumanChatQualityCore
    from astrbot_plugin_human_chat_quality.quality_rules import (
        RUNTIME_HINT_MARKER,
        STABLE_RULE_MARKER,
    )
    from astrbot_plugin_human_chat_quality.runtime_state import RuntimeStateStore

    HAVE_MAIN = True
except ImportError:  # pragma: no cover - 宿主缺失时跳过
    HAVE_MAIN = False


class FakePart:
    def __init__(self, text):
        self.text = text


class FakeEvent:
    def __init__(self, origin):
        self.unified_msg_origin = origin


class FakeLLMResp:
    def __init__(self, text):
        self.completion_text = text
        self.result_chain = None


class FakeReq:
    def __init__(self):
        self.system_prompt = "原人设：你是XX"
        self.contexts = []
        self.extra_user_content_parts = []


@unittest.skipUnless(HAVE_MAIN, "宿主 astrbot 不可导入，跳过 main 流程用例")
class TestConfigParse(unittest.TestCase):
    def test_bool_int_list_parse(self):
        self.assertTrue(AppConfig.from_config({"enabled": "true"}).enabled)
        self.assertFalse(AppConfig.from_config({"enabled": False}).enabled)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": 2}).recent_reply_window, 3)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": 999}).recent_reply_window, 50)
        cfg = AppConfig.from_config({"custom_cliches": ["  词  ", ""]})
        self.assertEqual(cfg.custom_cliches, ("词",))

    def test_defaults(self):
        cfg = AppConfig.from_config(None)
        self.assertEqual(cfg.max_runtime_hint_chars, 600)
        self.assertEqual(cfg.state_retention_days, 14)
        self.assertTrue(cfg.enabled and cfg.inject_stable_rules and cfg.inject_runtime_state)


@unittest.skipUnless(HAVE_MAIN, "宿主 astrbot 不可导入，跳过 main 流程用例")
class TestCoreFlow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = RuntimeStateStore(os.path.join(self.dir, "s.json"), 14, 8, ())
        self.core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=FakePart)
        self.ev = FakeEvent("aiocqhttp:GroupMessage:111")

    def test_stable_rules_v4_injected(self):
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": "在吗"}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
        self.assertEqual(req.system_prompt.count(STABLE_RULE_MARKER), 1)
        self.assertIn("natural-talk", req.system_prompt)

    def test_legacy_v2_block_removed_from_history(self):
        req = FakeReq()
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "原话"},
                    {"type": "text", "text": "[Human Chat Quality Rules v2]\n旧规则块"},
                ],
            }
        ]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        texts = [p.get("text", "") for ctx in req.contexts for p in ctx["content"]]
        self.assertEqual(texts, ["原话"])  # 旧块被清扫，用户原话保留
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

    def test_replace_in_history_no_accumulation(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": RUNTIME_HINT_MARKER + "\n旧"}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(len(req.extra_user_content_parts), 0)
        self.assertNotIn("旧", json.dumps(req.contexts, ensure_ascii=False))

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
        core_off = HumanChatQualityCore(
            AppConfig.from_config({"enabled": False}), self.store, text_part_factory=FakePart
        )
        self.assertIn("关闭", core_off.status_text(self.ev.unified_msg_origin, self.ev))


if __name__ == "__main__":
    unittest.main()
