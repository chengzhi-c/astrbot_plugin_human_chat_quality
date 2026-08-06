"""真实 AstrBot 宿主契约测试（仅装有兼容 astrbot 时运行，顺序无关）。

策略：不做任何 sys.modules 清理/重导入——astrbot 4.23+ 的 SQLModel 表定义
进程内不可二次导入，purge 后重导入会抛 InvalidRequestError。
判别真实宿主：astrbot 包元数据版本在支持范围内，且导入后的模块带 __file__
（tests/_fakes 注入的假模块没有）。_fakes 已实现"真实宿主优先"，故真实
可用时整套件本就走真实 API，本文件补充真实对象端到端契约用例。
宿主不可用或版本不兼容时整类跳过；fakes 环境行为不受影响。

真实/假差异备忘：
- ProviderRequest.extra_user_content_parts 真实默认 []（fakes 为 None），
  断言用 len()==0 而非 is None。
- TextPart 4.23+ 无 mark_as_temp；model_dump 仅 {type, text}。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _fakes import FakeEvent, is_supported_host


_REAL = False
_REAL_VERSION = None
try:
    import importlib.metadata as _metadata

    _REAL_VERSION = _metadata.version("astrbot")
    if is_supported_host(_REAL_VERSION):
        import astrbot

        _REAL = bool(getattr(astrbot, "__file__", None))
except Exception:
    _REAL = False


@unittest.skipUnless(_REAL and is_supported_host(_REAL_VERSION or ""), "requires astrbot >=4.23,<5")
class RealHostContractTest(unittest.TestCase):
    """真实宿主下的端到端契约（真实 ProviderRequest / TextPart / LLMResponse）。"""

    def _core(self, td: str, config: dict | None = None):
        from main import HumanChatQualityCore
        from runtime_state import RuntimeStateStore

        cfg = {"enabled": True, "debug_log": False}
        if config:
            cfg.update(config)
        store = RuntimeStateStore(Path(td) / "state.json", retention_days=14, recent_reply_window=8)
        return HumanChatQualityCore(cfg, store)

    def test_real_textpart_contract(self):
        from astrbot.core.agent.message import TextPart

        from quality_rules import make_text_part

        part = make_text_part("契约测试")
        self.assertIsNotNone(part)
        self.assertIsInstance(part, TextPart)
        self.assertEqual(part.model_dump(), {"type": "text", "text": "契约测试"})

    def test_real_provider_injection_idempotent(self):
        """v0.6.0：稳定规则写 system_prompt（不入 extra/历史），同请求幂等。"""
        from astrbot.api.provider import ProviderRequest

        from quality_rules import STABLE_RULE_MARKER

        with tempfile.TemporaryDirectory() as td:
            core = self._core(td)
            req = ProviderRequest("你好")
            asyncio.run(core.on_llm_request(FakeEvent(), req))
            self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
            self.assertEqual(len(req.extra_user_content_parts), 0, "稳定规则不再走 extra")
            after_first = req.system_prompt
            asyncio.run(core.on_llm_request(FakeEvent(), req))
            self.assertEqual(req.system_prompt, after_first, "同请求幂等")

    def test_history_stable_block_migrated(self):
        """v0.6.0 迁移：历史 list 形态旧规则块被移除，规则改由 system 提供。"""
        from astrbot.api.provider import ProviderRequest

        from quality_rules import STABLE_RULE_MARKER

        with tempfile.TemporaryDirectory() as td:
            core = self._core(td)
            req = ProviderRequest(
                "你好",
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户原话"},
                            {"type": "text", "text": f"{STABLE_RULE_MARKER}\n旧规则"},
                        ],
                    }
                ],
            )
            asyncio.run(core.on_llm_request(FakeEvent(), req))
            self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
            texts = [p["text"] for p in req.contexts[0]["content"]]
            self.assertFalse(any(STABLE_RULE_MARKER in t for t in texts), "旧规则块应被移除")
            self.assertIn("用户原话", texts)

    def test_history_marker_skip_and_dynamic_replace(self):
        from astrbot.api.provider import LLMResponse, ProviderRequest

        from quality_rules import RUNTIME_HINT_MARKER

        with tempfile.TemporaryDirectory() as td:
            core_r = self._core(td, {"inject_stable_rules": False})
            asyncio.run(
                core_r.on_llm_response(
                    FakeEvent(), LLMResponse(role="assistant", completion_text="这个我先记下了，希望对你有帮助")
                )
            )
            req3 = ProviderRequest(
                "再聊",
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户原话"},
                            {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词"},
                        ],
                    }
                ],
            )
            asyncio.run(core_r.on_llm_request(FakeEvent(), req3))
            self.assertEqual(len(req3.extra_user_content_parts), 0, "原位替换不追加")
            marked = [
                part
                for ctx in req3.contexts
                if isinstance(ctx.get("content"), list)
                for part in ctx["content"]
                if isinstance(part, dict) and isinstance(part.get("text"), str) and RUNTIME_HINT_MARKER in part["text"]
            ]
            self.assertEqual(len(marked), 1, "历史应只有一个 runtime 块")
            self.assertIn("希望对你有帮助", marked[0]["text"])

    def test_stale_runtime_block_removed_real_request(self):
        """hint 清空后，真实 ProviderRequest 历史中的旧动态块被移除，用户原话保留。"""
        from astrbot.api.provider import LLMResponse, ProviderRequest

        from quality_rules import RUNTIME_HINT_MARKER

        with tempfile.TemporaryDirectory() as td:
            core = self._core(td, {"inject_stable_rules": False})
            event = FakeEvent()
            asyncio.run(
                core.on_llm_response(
                    event, LLMResponse(role="assistant", completion_text="这个我先记下了，希望对你有帮助")
                )
            )
            asyncio.run(core.on_llm_response(event, LLMResponse(role="assistant", completion_text="嗯，这个思路可以")))
            req = ProviderRequest(
                "再聊",
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户原话"},
                            {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词"},
                        ],
                    }
                ],
            )
            asyncio.run(core.on_llm_request(event, req))
            self.assertEqual(len(req.extra_user_content_parts), 0, "无 hint 不应追加")
            texts = [p["text"] for p in req.contexts[0]["content"]]
            self.assertFalse(any(RUNTIME_HINT_MARKER in t for t in texts), "旧动态块应被移除")
            self.assertIn("用户原话", texts)

    def test_response_recording(self):
        from astrbot.api.provider import LLMResponse

        with tempfile.TemporaryDirectory() as td:
            core = self._core(td)
            event = FakeEvent()
            asyncio.run(
                core.on_llm_response(event, LLMResponse(role="assistant", completion_text="好的，希望对你有帮助"))
            )
            state = core.store.get(event.unified_msg_origin)
            self.assertIn("希望对你有帮助", state.avoid_openers)

    def test_real_factory_failure_no_injection(self):
        """真实 ProviderRequest：TextPart 构造失败时不注入且不残留空列表
        （回归：fakes 的 None 哨兵在真实 [] 环境下不可用，须真实宿主等价用例）。"""
        from unittest import mock

        from astrbot.api.provider import LLMResponse, ProviderRequest

        with tempfile.TemporaryDirectory() as td:
            core = self._core(td, {"inject_stable_rules": False})
            event = FakeEvent()
            asyncio.run(
                core.on_llm_response(
                    event, LLMResponse(role="assistant", completion_text="这个我先记下了，希望对你有帮助")
                )
            )
            self.assertTrue(core.store.get(event.unified_msg_origin).avoid_openers, "前置：已产生提醒状态")
            with mock.patch("quality_rules.make_text_part", return_value=None):
                req = ProviderRequest("再聊")
                asyncio.run(core.on_llm_request(event, req))
                self.assertEqual(len(req.extra_user_content_parts), 0, "构造失败不应注入")

    def test_real_fakeevent_shares_contract(self):
        """真实宿主用例与 fakes 共用 FakeEvent（回归：重复定义漂移）。"""
        event = FakeEvent(origin="aiocqhttp:GroupMessage:123#abc", group_id="123#abc")
        self.assertEqual(event.unified_msg_origin, "aiocqhttp:GroupMessage:123#abc")
        self.assertEqual(event.get_group_id(), "123#abc")


if __name__ == "__main__":
    unittest.main()
