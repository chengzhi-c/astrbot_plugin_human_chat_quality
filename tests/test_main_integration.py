"""main.py 注入链路集成测试（mock req/event，不依赖真实 AstrBot 环境）。

运行：python -m unittest discover -s tests -v
覆盖：配置边界、_extract_response_text、on_llm_request 注入流程与幂等、
runtime hint、voice 注入、disabled 匹配、空 origin 隔离、temp part 工具。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import (
    HumanChatQualityCore,
    _extract_response_text,
    config_bool,
    config_int,
    config_list,
)
from quality_rules import (
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    append_temp_text_part,
    make_text_part,
)
from runtime_state import RuntimeStateStore
from voice_profile import VOICE_MARKER

VOICE_SAMPLES = [
    "哈哈确实",
    "嗯嗯",
    "可以吧",
    "笑死我了",
    "这也太逗了",
    "好家伙",
]


class _FakeEvent:
    def __init__(self, origin="GroupMessage:123#abc", group_id="123#abc"):
        self.unified_msg_origin = origin
        self._group_id = group_id
        self.message_obj = None

    def get_group_id(self):
        return self._group_id


class _FakeReq:
    def __init__(self, contexts=None, system_prompt="", prompt="hi"):
        self.contexts = contexts or []
        self.system_prompt = system_prompt
        self.prompt = prompt
        self.extra_user_content_parts = None


class _FakeResp:
    def __init__(self, completion_text=None, chain=None):
        self.completion_text = completion_text
        self.result_chain = chain


def _chain(*texts):
    class _Item:
        def __init__(self, text):
            self.text = text

    class _Chain:
        def __init__(self, items):
            self.chain = items

    return _Chain([_Item(t) for t in texts])


class ConfigHelperTest(unittest.TestCase):
    def test_config_bool(self):
        self.assertTrue(config_bool(None, "k", True))
        self.assertTrue(config_bool({}, "k", True))
        self.assertTrue(config_bool({"k": "yes"}, "k", False))
        self.assertFalse(config_bool({"k": "0"}, "k", True))
        self.assertFalse(config_bool({"k": False}, "k", True))

    def test_config_int_clamped(self):
        self.assertEqual(config_int({"k": "abc"}, "k", 8, 1, 50), 8)
        self.assertEqual(config_int({"k": 999}, "k", 8, 1, 50), 50)
        self.assertEqual(config_int({"k": -3}, "k", 8, 1, 50), 1)
        self.assertEqual(config_int({"k": "30"}, "k", 8, 1, 50), 30)

    def test_config_list(self):
        self.assertEqual(config_list({"k": "a\nb\n"}, "k"), ["a", "b"])
        self.assertEqual(config_list({"k": [" a ", "", "b"]}, "k"), ["a", "b"])
        self.assertEqual(config_list({}, "k"), [])


class ExtractResponseTextTest(unittest.TestCase):
    def test_completion_text_priority(self):
        resp = _FakeResp(completion_text="  完整回复  ", chain=_chain("旧回复"))
        self.assertEqual(_extract_response_text(resp), "完整回复")

    def test_result_chain_fallback(self):
        resp = _FakeResp(completion_text="", chain=_chain("甲", "乙"))
        self.assertEqual(_extract_response_text(resp), "甲 乙")

    def test_empty(self):
        self.assertEqual(_extract_response_text(_FakeResp()), "")
        self.assertEqual(_extract_response_text(_FakeResp(completion_text="   ")), "")


def _text_part_available() -> bool:
    try:
        from astrbot.core.agent.message import TextPart  # noqa: F401

        return True
    except Exception:
        return False


class AppendTempPartTest(unittest.TestCase):
    def test_creates_missing_list(self):
        req = _FakeReq()
        self.assertTrue(append_temp_text_part(req, "内容", factory=_fake_part_factory))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(req.extra_user_content_parts[0].text, "内容")

    def test_marker_idempotent(self):
        """marker 幂等依赖约定：注入文本本身以 marker 开头（与真实调用一致）。"""
        req = _FakeReq()
        req.extra_user_content_parts = []
        self.assertTrue(append_temp_text_part(req, "M1\n第一条", factory=_fake_part_factory, marker="M1"))
        self.assertFalse(append_temp_text_part(req, "第二条", factory=_fake_part_factory, marker="M1"))
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_non_list_rejected(self):
        req = _FakeReq()
        req.extra_user_content_parts = {"not": "list"}
        self.assertFalse(append_temp_text_part(req, "内容", factory=_fake_part_factory))

    def test_empty_text_rejected(self):
        req = _FakeReq()
        req.extra_user_content_parts = []
        self.assertFalse(append_temp_text_part(req, "   ", factory=_fake_part_factory))

    def test_make_text_part_failure_returns_none(self):
        """构造失败返回 None（回归：不再回退到注定失败的 _FallbackTextPart）。
        同时隔离 TextPart 三态缓存的全局状态，避免污染其他用例。"""
        import builtins
        from unittest import mock

        import quality_rules

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("astrbot"):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        with (
            mock.patch.object(quality_rules, "_TEXTPART_CLS", None),
            mock.patch.object(quality_rules, "_TEXTPART_PROBED", False),
            mock.patch("builtins.__import__", side_effect=fake_import),
        ):
            self.assertIsNone(make_text_part("x"))
        # factory 显式返回 None 时上层应识别为失败
        self.assertIsNone(make_text_part("x", factory=lambda _t: None))

    def test_make_text_part_dual_mode(self):
        """兼容两代 AstrBot：有 mark_as_temp（4.16）与无（4.23+）都产出 _no_save part。"""
        from quality_rules import make_text_part

        class OldStyle:
            """4.16 形态：mark_as_temp 返回 self。"""

            def __init__(self, text):
                self.text = text
                self._no_save = False

            def mark_as_temp(self):
                self._no_save = True
                return self

        class NewStyle:
            """4.23 形态：无 mark_as_temp，只能 setattr（新版本由 contexts 扫描兜底）。"""

            def __init__(self, text):
                self.text = text

        old_part = make_text_part("x", factory=OldStyle)
        self.assertIsNotNone(old_part)
        self.assertTrue(old_part._no_save)
        new_part = make_text_part("x", factory=NewStyle)
        self.assertIsNotNone(new_part)
        self.assertTrue(getattr(new_part, "_no_save", False))

    def test_setattr_pydantic_model_silent(self):
        """模拟 4.23.3 真实形态：pydantic BaseModel 上 setattr _no_save 不抛且 dump 不带出。"""
        from pydantic import BaseModel
        from quality_rules import make_text_part

        class P(BaseModel):
            text: str

        part = make_text_part("x", factory=lambda t: P(text=t))
        self.assertIsNotNone(part)
        self.assertEqual(part.model_dump(), {"text": "x"})

    @unittest.skipUnless(_text_part_available(), "requires real astrbot environment")
    def test_real_textpart_contract(self):
        """真实 TextPart 契约：model_dump 只含 type/text（私有标记不序列化），
        mark_as_temp 在 4.16 存在、4.23+ 移除，make_text_part 两种形态都不崩。"""
        from quality_rules import make_text_part

        part = make_text_part("契约测试")
        self.assertIsNotNone(part)
        self.assertEqual(part.model_dump(), {"type": "text", "text": "契约测试"})

    def test_request_has_marker_contract(self):
        """request_has_marker 对三种来源的 marker 都能识别（回归：H1 修复）。"""
        from quality_rules import request_has_marker

        req_system = _FakeReq(system_prompt=f"前缀\n{STABLE_RULE_MARKER}")
        self.assertTrue(request_has_marker(req_system, STABLE_RULE_MARKER))
        req_parts = _FakeReq()
        req_parts.extra_user_content_parts = [_fake_part_factory(f"{STABLE_RULE_MARKER}\n规则")]
        self.assertTrue(request_has_marker(req_parts, STABLE_RULE_MARKER))
        req_ctx = _FakeReq(contexts=[{"role": "user", "content": f"{STABLE_RULE_MARKER}\n历史"}])
        self.assertTrue(request_has_marker(req_ctx, STABLE_RULE_MARKER))
        req_clean = _FakeReq(contexts=[{"role": "user", "content": "正常历史"}])
        self.assertFalse(request_has_marker(req_clean, STABLE_RULE_MARKER))

    def test_request_has_marker_ignores_non_user_roles(self):
        """assistant/system 消息含 marker（模型复述/手打）不误停注入（回归：M3）。"""
        from quality_rules import request_has_marker

        req = _FakeReq(
            contexts=[
                {"role": "assistant", "content": f"我复述一下：{STABLE_RULE_MARKER}"},
                {"role": "system", "content": f"系统也有 {STABLE_RULE_MARKER}"},
            ]
        )
        self.assertFalse(request_has_marker(req, STABLE_RULE_MARKER))

    def test_replace_marker_in_contexts(self):
        """原位替换：list 形态替换成功、str 形态视为已存在、找不到返回 False（回归：M1）。"""
        from quality_rules import replace_marker_in_contexts

        # list 形态（4.23+ 实际形态）：替换且不丢其他 part
        req = _FakeReq(
            contexts=[
                {"role": "user", "content": [
                    {"type": "text", "text": "用户原话"},
                    {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧避用词A"},
                ]}
            ]
        )
        self.assertTrue(replace_marker_in_contexts(req, RUNTIME_HINT_MARKER, "新块B"))
        parts = req.contexts[0]["content"]
        self.assertEqual(parts[0]["text"], "用户原话")
        self.assertEqual(parts[1]["text"], "新块B")
        # 非 user 消息不替换
        req2 = _FakeReq(contexts=[{"role": "assistant", "content": f"x\n{RUNTIME_HINT_MARKER}"}])
        self.assertFalse(replace_marker_in_contexts(req2, RUNTIME_HINT_MARKER, "y"))
        # 找不到（首轮）返回 False
        req3 = _FakeReq(contexts=[{"role": "user", "content": "正常历史"}])
        self.assertFalse(replace_marker_in_contexts(req3, RUNTIME_HINT_MARKER, "y"))
        # str 形态含 marker：视为已存在，不替换（防御，实际链路为 list 形态）
        req4 = _FakeReq(contexts=[{"role": "user", "content": f"x\n{RUNTIME_HINT_MARKER}\n旧块"}])
        self.assertTrue(replace_marker_in_contexts(req4, RUNTIME_HINT_MARKER, "y"))
        self.assertEqual(req4.contexts[0]["content"], f"x\n{RUNTIME_HINT_MARKER}\n旧块")


def _fake_part_factory(text: str):
    class _P:
        def __init__(self, text: str):
            self.text = text

        def mark_as_temp(self):
            return self

    return _P(text)


class CoreInjectionTest(unittest.TestCase):
    def _core(self, tmp: Path, config: dict):
        store = RuntimeStateStore(
            tmp / "state.json",
            retention_days=14,
            recent_reply_window=8,
        )
        return HumanChatQualityCore(config, store, text_part_factory=_fake_part_factory)

    def _run(self, coro):
        return asyncio.run(coro)

    def _part_texts(self, req) -> list[str]:
        return [getattr(p, "text", "") for p in (req.extra_user_content_parts or [])]

    def test_stable_rules_injected_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "prompt_injection_mode": "cache_friendly"})
            req = _FakeReq()
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertTrue(any(STABLE_RULE_MARKER in t for t in self._part_texts(req)))
            count = len(req.extra_user_content_parts)
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertEqual(len(req.extra_user_content_parts), count, "幂等：同请求不应重复注入")

    def test_contexts_marker_skips_injection(self):
        """回归（H1）：AstrBot >=4.23 注入块会进历史，contexts 已有 marker 时不再注入，
        避免规则块逐轮累积。覆盖字符串与多模态 content 两种形态。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            for content in (
                f"用户消息\n{STABLE_RULE_MARKER}\n规则内容",
                [{"type": "text", "text": f"用户消息\n{STABLE_RULE_MARKER}"}],
            ):
                req = _FakeReq(contexts=[{"role": "user", "content": content}])
                self._run(core.on_llm_request(_FakeEvent(), req))
                self.assertIsNone(req.extra_user_content_parts, "历史已有 marker 不应重复注入")

    def test_runtime_marker_in_contexts_skips_runtime(self):
        """runtime/voice 注入块入历史后，各自 marker 也应阻止重复注入。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            req = _FakeReq(contexts=[{"role": "user", "content": f"x\n{RUNTIME_HINT_MARKER}"}])
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertIsNone(req.extra_user_content_parts)

    def test_runtime_hint_after_cliche_reply(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = _FakeEvent()
            self._run(core.on_llm_response(event, _FakeResp(completion_text="总的来说，这个方案不错")))
            req = _FakeReq()
            self._run(core.on_llm_request(event, req))
            self.assertTrue(any(RUNTIME_HINT_MARKER in t for t in self._part_texts(req)))
            self.assertTrue(any("总的来说" in t for t in self._part_texts(req)))

    def test_voice_enabled_and_skipped_after_history(self):
        """voice 注入生效后，历史出现 VOICE_MARKER 则后续轮不再注入。"""
        with tempfile.TemporaryDirectory() as td:
            config = {"enabled": True, "inject_stable_rules": False, "inject_runtime_state": False, "voice_match": True}
            core = self._core(Path(td), config)
            req = _FakeReq(contexts=[{"role": "user", "content": s} for s in VOICE_SAMPLES])
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertTrue(any(VOICE_MARKER in t for t in self._part_texts(req)))
            # 模拟 4.23+：本轮注入进了历史
            req2 = _FakeReq(
                contexts=[{"role": "user", "content": s} for s in VOICE_SAMPLES]
                + [{"role": "user", "content": f"x\n{VOICE_MARKER}\n提示"}]
            )
            self._run(core.on_llm_request(_FakeEvent(), req2))
            self.assertIsNone(req2.extra_user_content_parts)

    def test_runtime_hint_replaces_old_block(self):
        """4.23+：历史已有旧 runtime 块时原位替换而非追加（回归：M1 动态提示冻结）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = _FakeEvent()
            # 第一轮：制造避用词
            self._run(core.on_llm_response(event, _FakeResp(completion_text="总的来说，这个方案不错")))
            # 第二轮：contexts 里已有旧块（模拟 4.23+ 首轮入库后的历史）
            req = _FakeReq(
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户消息"},
                            {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧避用词"},
                        ],
                    }
                ]
            )
            self._run(core.on_llm_request(event, req))
            # 不追加新 part，旧块被原位替换为新 hint
            self.assertIsNone(req.extra_user_content_parts)
            parts = req.contexts[0]["content"]
            self.assertTrue(parts[1]["text"].startswith(RUNTIME_HINT_MARKER))
            self.assertIn("总的来说", parts[1]["text"])
            self.assertNotIn("旧避用词", parts[1]["text"])

    def test_voice_injected_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            config = {"enabled": True, "inject_stable_rules": False, "inject_runtime_state": False, "voice_match": True}
            core = self._core(Path(td), config)
            req = _FakeReq(contexts=[{"role": "user", "content": s} for s in VOICE_SAMPLES])
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertTrue(any(VOICE_MARKER in t for t in self._part_texts(req)))

    def test_disabled_session_skips(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "disabled_sessions": ["123"]})
            req = _FakeReq()
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertIsNone(req.extra_user_content_parts)

    def test_empty_origin_skips(self):
        """无来源事件不参与状态管理（回归：不再挤进 unknown 会话）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            req = _FakeReq()
            self._run(core.on_llm_request(_FakeEvent(origin="", group_id=None), req))
            self.assertIsNone(req.extra_user_content_parts)
            self._run(core.on_llm_response(_FakeEvent(origin="", group_id=None), _FakeResp(completion_text="总之测试")))
            self.assertEqual(core.store.sessions, {})

    def test_all_disabled_skips(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": False})
            req = _FakeReq()
            self._run(core.on_llm_request(_FakeEvent(), req))
            self.assertIsNone(req.extra_user_content_parts)


if __name__ == "__main__":
    unittest.main()
