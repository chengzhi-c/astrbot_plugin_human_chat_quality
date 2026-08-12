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
    from astrbot_plugin_human_chat_quality.main import (
        AppConfig,
        HumanChatQualityCore,
        HumanChatQualityPlugin,
        _extract_response_text,
        group_id_from_event,
        is_session_disabled,
        match_keys,
    )
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

    def test_all_int_clamps(self):
        self.assertEqual(AppConfig.from_config({"max_runtime_hint_chars": 5}).max_runtime_hint_chars, 80)
        self.assertEqual(AppConfig.from_config({"max_runtime_hint_chars": 99999}).max_runtime_hint_chars, 3000)
        self.assertEqual(AppConfig.from_config({"state_retention_days": 0}).state_retention_days, 1)
        self.assertEqual(AppConfig.from_config({"state_retention_days": 9999}).state_retention_days, 365)

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


@unittest.skipUnless(HAVE_MAIN, "宿主 astrbot 不可导入，跳过 main 流程用例")
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


@unittest.skipUnless(HAVE_MAIN, "宿主 astrbot 不可导入，跳过 main 流程用例")
class TestResponseTextExtraction(unittest.TestCase):
    """C11：回复文本提取的 result_chain 兜底路径。"""

    def test_completion_text_used_first(self):
        self.assertEqual(_extract_response_text(FakeLLMResp("正文")), "正文")

    def test_chain_fallback_with_role_filter(self):
        class Part:
            def __init__(self, role, text):
                self.role = role
                self.text = text

        class Chain:
            def __init__(self, parts):
                self.chain = parts

        resp = FakeLLMResp("")
        resp.result_chain = Chain([Part("assistant", "模型输出"), Part("user", "用户原话")])
        self.assertEqual(_extract_response_text(resp), "模型输出")

    def test_chain_content_field(self):
        class ContentPart:
            def __init__(self, content):
                self.content = content

        class Chain:
            def __init__(self, parts):
                self.chain = parts

        resp = FakeLLMResp("")
        resp.result_chain = Chain([ContentPart("正文内容")])
        self.assertEqual(_extract_response_text(resp), "正文内容")

    def test_empty_all(self):
        self.assertEqual(_extract_response_text(FakeLLMResp("")), "")


@unittest.skipUnless(HAVE_MAIN, "宿主 astrbot 不可导入，跳过 main 流程用例")
class TestCoreFlowExtra(unittest.TestCase):
    """C3/C6/C11/C12：Core 层补充契约（含插件层异常隔离）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = RuntimeStateStore(os.path.join(self.dir, "s.json"), 14, 8, ())
        self.core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=FakePart)
        self.ev = FakeEvent("aiocqhttp:GroupMessage:111")

    def test_overlong_custom_cliche_filtered_end_to_end(self):
        # 超长自定义词在 Store 构造期被过滤，Core 全流程不入 avoid_openers
        store = RuntimeStateStore(os.path.join(self.dir, "s2.json"), 14, 8, ("x" * 21,))
        core = HumanChatQualityCore(AppConfig.from_config(None), store, text_part_factory=FakePart)
        asyncio.run(core.on_llm_response(self.ev, FakeLLMResp("这是" + "x" * 21 + "的回复")))
        self.assertEqual(store.get(self.ev.unified_msg_origin).avoid_openers, [])

    def test_runtime_off_stops_inject_and_record(self):
        asyncio.run(self.core.set_session_enabled(self.ev.unified_msg_origin, False))
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertNotIn(STABLE_RULE_MARKER, req.system_prompt)
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        self.assertEqual(self.store.get(self.ev.unified_msg_origin).recent_openers, [])

    def test_injection_count_only_real_injections(self):
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(self.core.injection_count, 1)
        # 幂等轮：同一 req 已含规则、无 hint → 不注入不计
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(self.core.injection_count, 1)
        # 仅移除历史旧块 → 不计注入
        req2 = FakeReq()
        req2.system_prompt = req.system_prompt
        req2.contexts = [{"role": "user", "content": [{"type": "text", "text": RUNTIME_HINT_MARKER + "\n旧"}]}]
        asyncio.run(self.core.on_llm_request(self.ev, req2))
        self.assertEqual(self.core.injection_count, 1)

    def test_no_origin_skips_everything(self):
        ev = FakeEvent("")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(ev, req))
        self.assertEqual(req.system_prompt, "原人设：你是XX")
        asyncio.run(self.core.on_llm_response(ev, FakeLLMResp("好的，回答")))
        self.assertEqual(self.store.sessions, {})

    def test_plugin_layer_swallows_core_errors(self):
        async def fail(event, req):
            raise RuntimeError("boom")

        stub = type("StubCore", (), {"on_llm_request": staticmethod(fail)})()
        stub_plugin = type("StubPlugin", (), {"core": stub})()
        # 装饰器原样返回函数，unbound 调用等价于宿主直接调插件方法
        asyncio.run(HumanChatQualityPlugin.on_llm_request(stub_plugin, object(), object()))


if __name__ == "__main__":
    unittest.main()
