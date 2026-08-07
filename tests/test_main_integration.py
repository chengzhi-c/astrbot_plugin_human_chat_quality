"""main.py 注入链路集成测试（mock req/event，不依赖真实 AstrBot 环境）。

运行：python -m unittest discover -s tests -v
覆盖：配置边界、_extract_response_text、on_llm_request 注入流程与幂等、
runtime hint、disabled 匹配、空 origin 隔离、temp part 工具。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _fakes import FakeEvent
from helpers import (
    FakeReq as _FakeReq,
    FakeResp as _FakeResp,
    HumanChatQualityCore,
    RUNTIME_HINT_MARKER,
    RuntimeStateStore,
    STABLE_RULE_MARKER,
    chain as _chain,
    fake_part_factory as _fake_part_factory,
    get_main,
    get_quality_rules,
    load_plugin_package,
)

load_plugin_package()
main = get_main()
quality_rules = get_quality_rules()
main_module = main

_extract_response_text = main._extract_response_text
AppConfig = main.AppConfig
append_temp_text_part = quality_rules.append_temp_text_part
make_text_part = quality_rules.make_text_part


class AppConfigTest(unittest.TestCase):
    """AppConfig.from_config 解析边界（原 ConfigHelperTest 迁移，断言同一批边界）。"""

    def test_none_and_empty_defaults(self):
        for raw in (None, {}):
            cfg = AppConfig.from_config(raw)
            self.assertTrue(cfg.enabled)
            self.assertTrue(cfg.inject_stable_rules)
            self.assertTrue(cfg.inject_runtime_state)
            self.assertFalse(cfg.debug_log)
            self.assertEqual(cfg.max_runtime_hint_chars, 600)
            self.assertEqual(cfg.state_retention_days, 14)
            self.assertEqual(cfg.recent_reply_window, 8)
            self.assertEqual(cfg.custom_cliches, ())
            self.assertEqual(cfg.disabled_sessions, frozenset())

    def test_bool_string_forms(self):
        self.assertTrue(AppConfig.from_config({"enabled": "yes"}).enabled)
        self.assertTrue(AppConfig.from_config({"enabled": "on"}).enabled)
        self.assertFalse(AppConfig.from_config({"enabled": "0"}).enabled)
        self.assertFalse(AppConfig.from_config({"enabled": False}).enabled)
        self.assertTrue(AppConfig.from_config({"enabled": True}).enabled)

    def test_int_clamped(self):
        # recent_reply_window：默认 8，夹取 3-50（下限与重复阈值同构）
        self.assertEqual(AppConfig.from_config({"recent_reply_window": "abc"}).recent_reply_window, 8)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": 999}).recent_reply_window, 50)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": -3}).recent_reply_window, 3)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": "30"}).recent_reply_window, 30)
        # None 降级（int(None) 抛 TypeError 路径）
        self.assertEqual(AppConfig.from_config({"recent_reply_window": None}).recent_reply_window, 8)
        # max_runtime_hint_chars：默认 600，夹取 80-3000
        self.assertEqual(AppConfig.from_config({"max_runtime_hint_chars": 1}).max_runtime_hint_chars, 80)
        self.assertEqual(AppConfig.from_config({"max_runtime_hint_chars": 9999}).max_runtime_hint_chars, 3000)

    def test_list_forms(self):
        self.assertEqual(AppConfig.from_config({"custom_cliches": "a\nb\n"}).custom_cliches, ("a", "b"))
        self.assertEqual(AppConfig.from_config({"custom_cliches": [" a ", "", "b"]}).custom_cliches, ("a", "b"))
        self.assertEqual(AppConfig.from_config({}).custom_cliches, ())
        # disabled_sessions：全小写 frozenset
        cfg = AppConfig.from_config({"disabled_sessions": ["GroupMessage:123#A", "456"]})
        self.assertEqual(cfg.disabled_sessions, frozenset({"groupmessage:123#a", "456"}))


class ExtractResponseTextTest(unittest.TestCase):
    def test_completion_text_priority(self):
        resp = _FakeResp(completion_text="  完整回复  ", chain=_chain("旧回复"))
        self.assertEqual(_extract_response_text(resp), "完整回复")

    def test_result_chain_fallback(self):
        resp = _FakeResp(completion_text="", chain=_chain("甲", "乙"))
        self.assertEqual(_extract_response_text(resp), "甲 乙")

    def test_chain_skips_non_assistant_parts(self):
        """兜底链跳过明确非 assistant 的 part（回归：吸收平台消息里的用户输入）。"""

        class _Item:
            def __init__(self, text, role=None):
                self.text = text
                self.role = role

        class _Chain:
            def __init__(self, items):
                self.chain = items

        resp = _FakeResp(completion_text="", chain=_Chain([_Item("用户原话", role="user"), _Item("模型回答")]))
        self.assertEqual(_extract_response_text(resp), "模型回答")
        # 无 role 属性的 part（TextPart 形态）照常取文本
        resp2 = _FakeResp(completion_text="", chain=_Chain([_Item("甲"), _Item("乙")]))
        self.assertEqual(_extract_response_text(resp2), "甲 乙")

    def test_content_field_fallback(self):
        """无 text 属性时取 content 字段；结构化 content 不进入记录（回归：类型防御）。"""

        class _Item:
            content = "备用文本"

        class _DictItem:
            content = {"type": "text"}

        class _Chain:
            def __init__(self, items):
                self.chain = items

        resp = _FakeResp(completion_text="", chain=_Chain([_Item()]))
        self.assertEqual(_extract_response_text(resp), "备用文本")
        resp2 = _FakeResp(completion_text="", chain=_Chain([_DictItem()]))
        self.assertEqual(_extract_response_text(resp2), "", "结构化 content 应被跳过")

    def test_empty(self):
        self.assertEqual(_extract_response_text(_FakeResp()), "")
        self.assertEqual(_extract_response_text(_FakeResp(completion_text="   ")), "")


class AppendTempPartTest(unittest.TestCase):
    def test_creates_missing_list(self):
        req = _FakeReq()
        self.assertTrue(append_temp_text_part(req, "内容", factory=_fake_part_factory))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(req.extra_user_content_parts[0].text, "内容")

    def test_marker_idempotent(self):
        """marker 幂等依赖契约：注入文本本身以 marker 开头（与真实调用一致）。"""
        req = _FakeReq()
        req.extra_user_content_parts = []
        self.assertTrue(append_temp_text_part(req, "M1\n第一条", factory=_fake_part_factory, marker="M1"))
        self.assertFalse(append_temp_text_part(req, "M1\n第二条", factory=_fake_part_factory, marker="M1"))
        self.assertEqual(len(req.extra_user_content_parts), 1)

    def test_marker_in_req_semantics(self):
        """_marker_in_req 只查 system_prompt 与本请求 parts（回归：守卫职责边界）。"""
        # system_prompt 含 marker → append 跳过
        req = _FakeReq(system_prompt=f"前缀 {RUNTIME_HINT_MARKER}")
        req.extra_user_content_parts = []
        self.assertFalse(
            append_temp_text_part(
                req, f"{RUNTIME_HINT_MARKER}\n新", factory=_fake_part_factory, marker=RUNTIME_HINT_MARKER
            )
        )
        # 不同 marker 互不干扰：system 含 A marker 不影响注入 B marker
        req3 = _FakeReq(system_prompt=f"前缀 {STABLE_RULE_MARKER}")
        req3.extra_user_content_parts = []
        self.assertTrue(
            append_temp_text_part(
                req3, f"{RUNTIME_HINT_MARKER}\n新", factory=_fake_part_factory, marker=RUNTIME_HINT_MARKER
            )
        )

    def test_core_factory_skips_probe(self):
        """显式 factory 时构造 Core 不触发 TextPart 探测（回归：去全局化，factory 优先）。"""
        store = RuntimeStateStore(Path(tempfile.mkdtemp()) / "state.json", retention_days=14, recent_reply_window=8)
        with mock.patch.object(main_module, "_probe_text_part_cls") as probe:
            core = HumanChatQualityCore({"enabled": True}, store, text_part_factory=_fake_part_factory)
            probe.assert_not_called()
        self.assertIs(core.text_part_factory, _fake_part_factory)

    def test_core_probes_when_no_factory(self):
        """无 factory 时构造 Core 触发探测；探测失败 → None → 动态注入降级。"""
        store = RuntimeStateStore(Path(tempfile.mkdtemp()) / "state.json", retention_days=14, recent_reply_window=8)
        with mock.patch.object(main_module, "_probe_text_part_cls", return_value=None) as probe:
            core = HumanChatQualityCore({"enabled": True}, store)
            probe.assert_called_once()
        self.assertIsNone(core.text_part_factory)

    def test_append_guard_scoped_to_request(self):
        """守卫只查 system/parts；历史 contexts 含 marker 不阻止追加（回归：P3 收窄后的契约，
        历史级幂等由 on_llm_request → apply_context_marker 负责）。"""
        req = _FakeReq(contexts=[{"role": "user", "content": f"{RUNTIME_HINT_MARKER}\n历史旧块"}])
        self.assertTrue(
            append_temp_text_part(
                req, f"{RUNTIME_HINT_MARKER}\n新块", factory=_fake_part_factory, marker=RUNTIME_HINT_MARKER
            )
        )

    def test_non_list_rejected(self):
        req = _FakeReq()
        req.extra_user_content_parts = {"not": "list"}
        self.assertFalse(append_temp_text_part(req, "内容", factory=_fake_part_factory))

    def test_empty_text_rejected(self):
        req = _FakeReq()
        req.extra_user_content_parts = []
        self.assertFalse(append_temp_text_part(req, "   ", factory=_fake_part_factory))

    def test_make_text_part_failure_returns_none(self):
        """无 factory 或构造失败返回 None（回归：不再回退到注定失败的 _FallbackTextPart）。"""
        self.assertIsNone(make_text_part("x"))
        # factory 显式返回 None 时上层应识别为失败
        self.assertIsNone(make_text_part("x", factory=lambda text: None))
        # factory 抛异常时同样视为失败（回归：去全局化后 make_text_part 的异常兜底）
        self.assertIsNone(make_text_part("x", factory=lambda text: (_ for _ in ()).throw(RuntimeError("boom"))))

    def test_make_text_part_factory(self):
        """v0.6.0：只做构造，不再做 part 级临时标记（4.23 保存链路只看消息级 _no_save）。"""
        part = make_text_part("x", factory=_fake_part_factory)
        self.assertIsNotNone(part)
        self.assertEqual(part.text, "x")

    def test_apply_context_marker_dedupes_multiple_blocks(self):
        """异常多块状态：只保留一个最新块，其余旧块丢弃（自愈防累积）。"""
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "历史一"},
                        {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧块A"},
                    ],
                },
                {"role": "user", "content": [{"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧块B"}]},
            ]
        )
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, "新块"), "modified")
        texts = [p["text"] for ctx in req.contexts for p in ctx["content"]]
        self.assertEqual(texts.count("新块"), 1, "历史应收敛为至多一个最新块")
        self.assertNotIn(f"{RUNTIME_HINT_MARKER}\n旧块A", texts)
        self.assertNotIn(f"{RUNTIME_HINT_MARKER}\n旧块B", texts)
        self.assertEqual(req.contexts[0]["content"][0]["text"], "历史一")

    def test_apply_context_marker_tuple_content_skipped(self):
        """纯 tuple content 不触碰，返回 absent（回归：非 list 非 str 形态保守跳过）。"""
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(contexts=[{"role": "user", "content": ("strange", "tuple")}])
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, "new hint"), "absent")
        self.assertIsInstance(req.contexts[0]["content"], tuple, "tuple 形态不得改写")

    def test_apply_context_marker_tuple_and_list_mixed(self):
        """tuple 跳过 + list 处理 → modified，tuple 保持（回归：混合形态单趟）。"""
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(
            contexts=[
                {"role": "user", "content": ("strange", "tuple")},
                {"role": "user", "content": [{"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧块"}]},
            ]
        )
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, "新块"), "modified")
        self.assertIsInstance(req.contexts[0]["content"], tuple, "tuple 形态不得改写")
        self.assertEqual(req.contexts[1]["content"][0]["text"], "新块")

    def test_apply_context_marker_modified_beats_str_blocked(self):
        """混合场景：list 可替换 + str 含 marker → 返回 modified（操作优先于阻塞，回归：优先级语义）。"""
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用户原话"},
                        {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧块"},
                    ],
                },
                {"role": "user", "content": f"x\n{RUNTIME_HINT_MARKER}"},
            ]
        )
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, "新块"), "modified")
        self.assertEqual(req.contexts[0]["content"][1]["text"], "新块")
        self.assertEqual(req.contexts[1]["content"], f"x\n{RUNTIME_HINT_MARKER}", "str 形态不得切割")

    def test_apply_context_marker_replace_and_absent(self):
        """原位替换 / absent / 非 user / str_blocked（回归：M1）。"""
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用户原话"},
                        {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词A"},
                    ],
                }
            ]
        )
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, "新块B"), "modified")
        parts = req.contexts[0]["content"]
        self.assertEqual(parts[0]["text"], "用户原话")
        self.assertEqual(parts[1]["text"], "新块B")

        req2 = _FakeReq(
            contexts=[{"role": "assistant", "content": [{"type": "text", "text": f"x\n{RUNTIME_HINT_MARKER}"}]}]
        )
        self.assertEqual(apply_context_marker(req2, RUNTIME_HINT_MARKER, "y"), "absent")

        req3 = _FakeReq(contexts=[{"role": "user", "content": [{"type": "text", "text": "正常历史"}]}])
        self.assertEqual(apply_context_marker(req3, RUNTIME_HINT_MARKER, "y"), "absent")

        raw = f"x\n{RUNTIME_HINT_MARKER}\n旧块"
        req4 = _FakeReq(contexts=[{"role": "user", "content": raw}])
        self.assertEqual(apply_context_marker(req4, RUNTIME_HINT_MARKER, "y"), "str_blocked")
        self.assertEqual(req4.contexts[0]["content"], raw)


class ApplyContextMarkerRemoveTest(unittest.TestCase):
    """apply_context_marker(None)：list 安全删除、str 保守不动。"""

    def test_removes_marker_part_keeps_user_text(self):
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "用户原话"},
                        {"type": "image", "url": "http://x/y.png"},
                        {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词"},
                    ],
                }
            ]
        )
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, None), "modified")
        content = req.contexts[0]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["text"], "用户原话")
        self.assertEqual(content[1], {"type": "image", "url": "http://x/y.png"})

    def test_str_content_not_touched(self):
        apply_context_marker = quality_rules.apply_context_marker

        raw = f"用户原话\n{RUNTIME_HINT_MARKER}\n旧块"
        req = _FakeReq(contexts=[{"role": "user", "content": raw}])
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, None), "str_blocked")
        self.assertEqual(req.contexts[0]["content"], raw)

    def test_no_marker_and_non_user(self):
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq(contexts=[{"role": "user", "content": [{"type": "text", "text": "干净消息"}]}])
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, None), "absent")
        req2 = _FakeReq(
            contexts=[{"role": "assistant", "content": [{"type": "text", "text": f"{RUNTIME_HINT_MARKER}\nx"}]}]
        )
        self.assertEqual(apply_context_marker(req2, RUNTIME_HINT_MARKER, None), "absent")
        self.assertEqual(len(req2.contexts[0]["content"]), 1)

    def test_missing_contexts(self):
        apply_context_marker = quality_rules.apply_context_marker

        req = _FakeReq()
        req.contexts = None
        self.assertEqual(apply_context_marker(req, RUNTIME_HINT_MARKER, None), "absent")


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

    def test_context_marker_skipped_when_both_injections_disabled(self):
        """双关配置下不触碰 contexts（回归：无条件白扫 O(历史)）。"""

        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False, "inject_runtime_state": False})
            req = _FakeReq(contexts=[{"role": "user", "content": [{"type": "text", "text": "x"}]}])
            with mock.patch.object(main_module, "apply_context_marker", wraps=main_module.apply_context_marker) as spy:
                self._run(core.on_llm_request(FakeEvent(), req))
                spy.assert_not_called()

    def test_context_marker_once_when_runtime_replaces(self):
        """runtime 有 hint 且历史有块：contexts 单趟 replace（回归：scan+replace 双扫）。"""

        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = FakeEvent()
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            req = _FakeReq(
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户消息"},
                            {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词"},
                        ],
                    }
                ]
            )
            with mock.patch.object(main_module, "apply_context_marker", wraps=main_module.apply_context_marker) as spy:
                self._run(core.on_llm_request(event, req))
                spy.assert_called_once()
                self.assertEqual(spy.call_args.args[1], RUNTIME_HINT_MARKER)
                self.assertTrue(spy.call_args.args[2], "应携带非空 hint 做 replace")
            self.assertIsNone(req.extra_user_content_parts)
            self.assertIn("希望对你有帮助", req.contexts[0]["content"][1]["text"])

    def test_factory_failure_no_injection(self):
        """TextPart 构造失败（factory 返回 None）时本轮不注入 hint（回归：区分
        "选择不注入"与"注入失败"，避免虚绿灯）。"""
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeStateStore(Path(td) / "state.json", retention_days=14, recent_reply_window=8)
            core = HumanChatQualityCore(
                {"enabled": True, "inject_stable_rules": False}, store, text_part_factory=lambda text: None
            )
            event = FakeEvent()
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            self.assertTrue(core.store.get(event.unified_msg_origin).avoid_openers, "前置：已产生提醒状态")
            req = _FakeReq()
            self._run(core.on_llm_request(event, req))
            # hint 非空且 marker 不在请求中，唯一不注入原因是 factory 失败
            self.assertIsNone(req.extra_user_content_parts, "factory 失败：不应注入")

    def test_stable_migration_checked_once_per_session(self):
        """stable 迁移清理按会话只执行一次，不同会话独立检查（回归：每轮全扫）。"""

        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            req = _FakeReq(
                contexts=[{"role": "user", "content": [{"type": "text", "text": f"{STABLE_RULE_MARKER}\n旧规则块"}]}]
            )
            with mock.patch.object(main_module, "apply_context_marker", wraps=main_module.apply_context_marker) as spy:
                self._run(core.on_llm_request(FakeEvent(), req))
                self._run(core.on_llm_request(FakeEvent(), req))
                # 同会话：仅首轮 stable 迁移 remove；后续两轮若无 runtime 状态则不再因 stable 调 apply
                # 本用例无 avoid_openers，runtime 仍会每轮 apply(remove 语义) 一次 → 共 1 stable + 2 runtime
                stable_calls = [c for c in spy.call_args_list if c.args[1] == STABLE_RULE_MARKER]
                self.assertEqual(len(stable_calls), 1, "stable 迁移每会话只一次")
                # 不同会话独立检查
                other = FakeEvent(origin="GroupMessage:456#def")
                req2 = _FakeReq()
                self._run(core.on_llm_request(other, req2))
                stable_calls = [c for c in spy.call_args_list if c.args[1] == STABLE_RULE_MARKER]
                self.assertEqual(len(stable_calls), 2)

    def test_status_text_active(self):
        """active 会话 status 输出包含启用状态、重复开头与累计注入（回归：status_text 零覆盖）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            event = FakeEvent()
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            text = core.status_text(event.unified_msg_origin, event)
            self.assertIn("当前会话：启用", text)
            self.assertIn("希望对你有帮助", text)
            self.assertIn("累计注入", text)

    def test_status_text_inactive_hides_state(self):
        """非 active 会话 status 不展示历史重复开头（回归：误导组合）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": False})
            text = core.status_text(FakeEvent().unified_msg_origin, FakeEvent())
            self.assertIn("关闭", text)
            self.assertNotIn("重复开头", text)

    def test_stable_rules_injected_and_idempotent(self):
        """v0.6.0：稳定规则幂等写 system_prompt，不再走 extra（不入历史）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            req = _FakeReq(system_prompt="你是某人设。")
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
            self.assertIn("你是某人设。", req.system_prompt)
            self.assertIsNone(req.extra_user_content_parts)
            after_first = req.system_prompt
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertEqual(req.system_prompt, after_first, "幂等：同请求不应重复注入")

    def test_stable_rules_migration_removes_history_block(self):
        """v0.6.0 迁移：落入历史的旧规则块被移除，规则改由 system 提供（随本次保存入库）。
        str 形态不做不安全切割，保守保留。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            # list 形态（4.23+ 实际形态）
            req = _FakeReq(
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户消息"},
                            {"type": "text", "text": f"{STABLE_RULE_MARKER}\n旧规则块"},
                        ],
                    }
                ]
            )
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertIn(STABLE_RULE_MARKER, req.system_prompt)
            texts = [p["text"] for p in req.contexts[0]["content"]]
            self.assertFalse(any(STABLE_RULE_MARKER in t for t in texts))
            self.assertIn("用户消息", texts)
            self.assertIsNone(req.extra_user_content_parts)
            # str 形态：不切割，但规则仍进 system
            req2 = _FakeReq(contexts=[{"role": "user", "content": f"用户消息\n{STABLE_RULE_MARKER}"}])
            self._run(core.on_llm_request(FakeEvent(), req2))
            self.assertIn(STABLE_RULE_MARKER, req2.system_prompt)
            self.assertIn(STABLE_RULE_MARKER, req2.contexts[0]["content"])

    def test_stable_migration_retries_after_str_blocked(self):
        """红灯：首轮 str_blocked 不标记完成；次轮 list 形态仍须清掉旧规则块。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            event = FakeEvent()
            raw = f"用户消息\n{STABLE_RULE_MARKER}\n旧规则"
            req1 = _FakeReq(contexts=[{"role": "user", "content": raw}])
            self._run(core.on_llm_request(event, req1))
            self.assertEqual(req1.contexts[0]["content"], raw)
            self.assertNotIn(event.unified_msg_origin, core._stable_migration_checked)

            req2 = _FakeReq(
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户消息"},
                            {"type": "text", "text": f"{STABLE_RULE_MARKER}\n旧规则"},
                        ],
                    }
                ]
            )
            self._run(core.on_llm_request(event, req2))
            texts = [p["text"] for p in req2.contexts[0]["content"]]
            self.assertFalse(any(STABLE_RULE_MARKER in t for t in texts))
            self.assertIn(event.unified_msg_origin, core._stable_migration_checked)

    def test_stable_block_kept_when_rules_disabled(self):
        """inject_stable_rules=false 时不动历史旧块（保守行为，与 runtime 对称）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            req = _FakeReq(
                contexts=[{"role": "user", "content": [{"type": "text", "text": f"{STABLE_RULE_MARKER}\n旧"}]}]
            )
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertEqual(req.system_prompt, "")
            self.assertIn(STABLE_RULE_MARKER, req.contexts[0]["content"][0]["text"])

    def test_runtime_marker_in_contexts_skips_runtime(self):
        """无状态时 str 历史含 marker：不追加（弱场景，见下条红灯）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            req = _FakeReq(contexts=[{"role": "user", "content": f"x\n{RUNTIME_HINT_MARKER}"}])
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertIsNone(req.extra_user_content_parts)

    def test_str_blocked_with_hint_does_not_append(self):
        """红灯：已有 avoid 状态 + str 形态历史含 marker 时不得 append（str_blocked）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = FakeEvent()
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            self.assertTrue(core.store.get(event.unified_msg_origin).avoid_openers)
            raw = f"用户消息\n{RUNTIME_HINT_MARKER}\n旧块"
            req = _FakeReq(contexts=[{"role": "user", "content": raw}])
            self._run(core.on_llm_request(event, req))
            self.assertIsNone(req.extra_user_content_parts, "str_blocked 不得再 append temp part")
            self.assertEqual(req.contexts[0]["content"], raw, "str 形态不得切割改写")

    def test_runtime_hint_after_cliche_reply(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = FakeEvent()
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            req = _FakeReq()
            self._run(core.on_llm_request(event, req))
            self.assertTrue(any(RUNTIME_HINT_MARKER in t for t in self._part_texts(req)))
            self.assertTrue(any("希望对你有帮助" in t for t in self._part_texts(req)))

    def test_runtime_hint_replaces_old_block(self):
        """4.23+：历史已有旧 runtime 块时原位替换而非追加（回归：M1 动态提示冻结）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = FakeEvent()
            # 第一轮：制造重复开头
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            # 第二轮：contexts 里已有旧块（模拟 4.23+ 首轮入库后的历史）
            req = _FakeReq(
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户消息"},
                            {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词"},
                        ],
                    }
                ]
            )
            self._run(core.on_llm_request(event, req))
            # 不追加新 part，旧块被原位替换为新 hint
            self.assertIsNone(req.extra_user_content_parts)
            parts = req.contexts[0]["content"]
            self.assertTrue(parts[1]["text"].startswith(RUNTIME_HINT_MARKER))
            self.assertIn("希望对你有帮助", parts[1]["text"])
            self.assertNotIn("旧提示词", parts[1]["text"])

    def test_stale_runtime_marker_removed_when_hint_empty(self):
        """回归（v0.5.6 红灯）：runtime hint 从"有"变"无"后，历史里的旧动态块
        必须被同步移除，模型不再看到已失效的重复开头约束。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False})
            event = FakeEvent()
            # 先命中末尾模板，产生提醒状态
            self._run(core.on_llm_response(event, _FakeResp(completion_text="这个我先记下了，希望对你有帮助")))
            self.assertTrue(core.store.get(event.unified_msg_origin).avoid_openers)
            # 一条干净回复后提醒状态清空
            self._run(core.on_llm_response(event, _FakeResp(completion_text="嗯，这个思路可以")))
            self.assertEqual(core.store.get(event.unified_msg_origin).avoid_openers, [])
            # 历史中仍留着旧 runtime 块（4.23+ 首轮入库形态）
            req = _FakeReq(
                contexts=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用户原话"},
                            {"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧提示词"},
                        ],
                    }
                ]
            )
            self._run(core.on_llm_request(event, req))
            texts = [p["text"] for p in req.contexts[0]["content"]]
            self.assertFalse(
                any(RUNTIME_HINT_MARKER in t for t in texts),
                "hint 已为空，历史旧动态块应被移除",
            )
            self.assertIn("用户原话", texts)
            self.assertIsNone(req.extra_user_content_parts)

    def test_stale_removal_only_when_runtime_enabled(self):
        """runtime 注入关闭时不动历史（保守行为，v0.5.6 范围）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "inject_stable_rules": False, "inject_runtime_state": False})
            req = _FakeReq(
                contexts=[{"role": "user", "content": [{"type": "text", "text": f"{RUNTIME_HINT_MARKER}\n旧"}]}]
            )
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertIn(RUNTIME_HINT_MARKER, req.contexts[0]["content"][0]["text"])

    def test_disabled_session_skips(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True, "disabled_sessions": ["123"]})
            req = _FakeReq()
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertIsNone(req.extra_user_content_parts)

    def test_empty_origin_skips(self):
        """无来源事件不参与状态管理（回归：不再挤进 unknown 会话）。"""
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": True})
            req = _FakeReq()
            self._run(core.on_llm_request(FakeEvent(origin="", group_id=None), req))
            self.assertIsNone(req.extra_user_content_parts)
            self._run(core.on_llm_response(FakeEvent(origin="", group_id=None), _FakeResp(completion_text="总之测试")))
            self.assertEqual(core.store.sessions, {})

    def test_all_disabled_skips(self):
        with tempfile.TemporaryDirectory() as td:
            core = self._core(Path(td), {"enabled": False})
            req = _FakeReq()
            self._run(core.on_llm_request(FakeEvent(), req))
            self.assertIsNone(req.extra_user_content_parts)


class PluginAssemblyTest(unittest.TestCase):
    """插件类装配：装饰器链、数据目录、store 初始化（fakes/真实宿主均适用）。"""

    def _instantiate(self, td: str):
        HumanChatQualityPlugin = main.HumanChatQualityPlugin
        StarTools = main.StarTools

        original = StarTools.get_data_dir
        StarTools.get_data_dir = staticmethod(lambda *_a, **_k: td)
        try:
            return HumanChatQualityPlugin(context=None, config={"enabled": True})
        finally:
            StarTools.get_data_dir = original

    def test_plugin_instantiation(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = self._instantiate(td)
            self.assertIsNotNone(plugin.core)
            self.assertEqual(plugin.store.state_path, Path(td) / "runtime_state.json")
            self.assertEqual(len(plugin.store.sessions), 0)
            for name in (
                "humanq_status",
                "humanq_on",
                "humanq_off",
                "humanq_reset",
                "humanq_rules",
            ):
                self.assertTrue(callable(getattr(plugin, name)), f"命令方法缺失: {name}")

    def test_plugin_terminate(self):
        with tempfile.TemporaryDirectory() as td:
            plugin = self._instantiate(td)
            asyncio.run(plugin.terminate())  # 不应抛异常


class MetadataVersionTest(unittest.TestCase):
    """_read_metadata_version 参数化后的降级分支覆盖。"""

    @staticmethod
    def _write_meta(td: str, content: str) -> str:
        path = Path(td) / "meta.yaml"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_nonexistent_path_returns_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(main._read_metadata_version(Path(td) / "nonexistent.yaml"), "0.0.0")

    def test_file_without_version_field_returns_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_meta(td, "name: test\ndesc: no version\n")
            self.assertEqual(main._read_metadata_version(path), "0.0.0")

    def test_version_with_quotes_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_meta(td, 'version: "1.2.3"\n')
            self.assertEqual(main._read_metadata_version(path), "1.2.3")

    def test_default_path_matches_plugin_dir(self):
        """无参调用指向插件目录 metadata.yaml（回归：默认路径契约，逐位等价）。"""
        self.assertEqual(main._read_metadata_version(), main.PLUGIN_VERSION)


if __name__ == "__main__":
    unittest.main()
