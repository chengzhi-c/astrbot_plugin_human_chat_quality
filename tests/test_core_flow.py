"""Host-independent Core behavior and configuration contracts."""

import asyncio
from dataclasses import fields
import json
import os
import re
import unittest
from unittest import mock

from pathlib import Path

from tests._support import (
    FakePart,
    FakeReq,
    V2_RULES_E4AA983,
    V5_RULES_B46BD0D,
    ensure_plugin_package,
    temporary_directory,
)

ensure_plugin_package()

from astrbot_plugin_human_chat_quality.core import AppConfig, HumanChatQualityCore, extract_response_text
from astrbot_plugin_human_chat_quality import core as core_module
from astrbot_plugin_human_chat_quality import quality_rules
from astrbot_plugin_human_chat_quality import runtime_state as runtime_state_module
from astrbot_plugin_human_chat_quality.quality_rules import (
    RUNTIME_HINT_MARKER,
    STABLE_RULE_MARKER,
    build_runtime_hint,
    build_stable_rules,
)
from astrbot_plugin_human_chat_quality.runtime_state import RuntimeStateStore, SessionState


class FakeEvent:
    def __init__(self, origin, text=""):
        self.unified_msg_origin = origin
        self.text = text


class FakeLLMResp:
    def __init__(self, text):
        self.completion_text = text
        self.result_chain = None


EXPECTED_MIN_RUNTIME_HINT_CHARS = 80
EXPECTED_MAX_RUNTIME_HINT_CHARS = 157


class TestConfigParse(unittest.TestCase):
    def test_bool_int_list_parse(self):
        self.assertTrue(AppConfig.from_config({"enabled": "true"}).enabled)
        self.assertFalse(AppConfig.from_config({"enabled": False}).enabled)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": 2}).recent_reply_window, 3)
        self.assertEqual(AppConfig.from_config({"recent_reply_window": 999}).recent_reply_window, 50)
        cfg = AppConfig.from_config({"custom_cliches": ["  词  ", ""]})
        self.assertEqual(cfg.custom_cliches, ("词", ""))

    def test_all_int_clamps(self):
        self.assertEqual(
            AppConfig.from_config({"max_runtime_hint_chars": 5}).max_runtime_hint_chars,
            EXPECTED_MIN_RUNTIME_HINT_CHARS,
        )
        self.assertEqual(
            AppConfig.from_config({"max_runtime_hint_chars": 99999}).max_runtime_hint_chars,
            EXPECTED_MAX_RUNTIME_HINT_CHARS,
        )
        self.assertEqual(AppConfig.from_config({"state_retention_days": 0}).state_retention_days, 1)
        self.assertEqual(AppConfig.from_config({"state_retention_days": 9999}).state_retention_days, 365)

    def test_defaults(self):
        cfg = AppConfig.from_config(None)
        self.assertEqual(cfg.max_runtime_hint_chars, EXPECTED_MAX_RUNTIME_HINT_CHARS)
        self.assertEqual(cfg.state_retention_days, 14)
        self.assertTrue(cfg.enabled and cfg.inject_stable_rules and cfg.inject_runtime_state)

    def test_schema_matches_config_contract(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(getattr(quality_rules, "MIN_RUNTIME_HINT_CHARS", None), EXPECTED_MIN_RUNTIME_HINT_CHARS)
        self.assertEqual(getattr(quality_rules, "MAX_RUNTIME_HINT_CHARS", None), EXPECTED_MAX_RUNTIME_HINT_CHARS)
        config_fields = {field.name for field in fields(AppConfig)}
        self.assertEqual(set(schema), config_fields)

        allowed_keys = {"description", "type", "default", "hint", "condition", "slider"}
        for name, field_schema in schema.items():
            with self.subTest(field=name):
                self.assertLessEqual(set(field_schema), allowed_keys)
                for condition_key in field_schema.get("condition", {}):
                    self.assertIn(condition_key, schema)
                    self.assertEqual(schema[condition_key]["type"], "bool")

        defaults = AppConfig()
        for name, field_schema in schema.items():
            expected = getattr(defaults, name)
            if isinstance(expected, (tuple, frozenset)):
                expected = []
            self.assertEqual(field_schema["default"], expected, name)

    def test_readme_config_table_matches_schema(self):
        """README 配置表与 schema 逐字段一致（防文档漂移复发）。"""
        repo_root = Path(__file__).resolve().parents[1]
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        schema = json.loads((repo_root / "_conf_schema.json").read_text(encoding="utf-8"))

        row_re = re.compile(r"^\| `([a-z_]+)` \| ([^|]+?) \|")
        rows: dict[str, str] = {}
        for line in readme.splitlines():
            match = row_re.match(line.strip())
            if match:
                rows[match.group(1)] = line.strip()

        for key, field_schema in schema.items():
            with self.subTest(field=key):
                self.assertIn(key, rows, f"README 配置表缺少 {key}")
                row = rows[key]
                default_cell = row_re.match(row).group(2).strip()
                default = field_schema["default"]
                if isinstance(default, bool):
                    self.assertEqual(default_cell, "true" if default else "false")
                elif isinstance(default, int):
                    self.assertEqual(default_cell, str(default))
                elif isinstance(default, list):
                    expected = "空" if not default else "、".join(str(item) for item in default)
                    self.assertEqual(default_cell, expected)
                elif isinstance(default, str):
                    self.assertEqual(default_cell, default)
                else:
                    self.fail(f"{key} 的 default 类型未覆盖: {type(default).__name__}")
                slider = field_schema.get("slider")
                if slider:
                    range_match = re.search(r"(\d+)\s*–\s*(\d+)", row)
                    self.assertIsNotNone(range_match, f"{key} README 行缺少范围描述")
                    self.assertEqual(
                        (int(range_match.group(1)), int(range_match.group(2))),
                        (slider["min"], slider["max"]),
                    )

    def test_schema_conditions_and_numeric_controls_match_runtime_semantics(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["inject_stable_rules"]["condition"], {"enabled": True})
        self.assertEqual(schema["inject_runtime_state"]["condition"], {"enabled": True})
        self.assertEqual(
            schema["max_runtime_hint_chars"]["condition"],
            {"enabled": True, "inject_runtime_state": True},
        )
        self.assertEqual(
            schema["max_runtime_hint_chars"]["slider"],
            {"min": EXPECTED_MIN_RUNTIME_HINT_CHARS, "max": EXPECTED_MAX_RUNTIME_HINT_CHARS, "step": 1},
        )
        self.assertEqual(schema["recent_reply_window"]["slider"], {"min": 3, "max": 50, "step": 1})
        for name in ("recent_reply_window", "custom_cliches", "state_retention_days", "disabled_sessions", "debug_log"):
            self.assertEqual(schema[name]["condition"], {"enabled": True})


class TestCoreFlow(unittest.TestCase):
    def setUp(self):
        self.dir = temporary_directory(self)
        self.store = RuntimeStateStore(os.path.join(self.dir, "s.json"), 14, 8, ())
        self.core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=FakePart)
        self.ev = FakeEvent("aiocqhttp:GroupMessage:111")

    def test_published_v5_block_is_replaced_with_current_rules(self):
        req = FakeReq()
        req.system_prompt = f"原人设：你是XX\n\n{V5_RULES_B46BD0D}"
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertNotIn("[Human Chat Quality Rules v5]", req.system_prompt)
        self.assertEqual(req.system_prompt.count(STABLE_RULE_MARKER), 1)
        self.assertIn("用户直接问及身份、能力边界或知识截止时间时，如实简短作答，不回避", req.system_prompt)

    def test_current_stable_rules_injected(self):
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
                    {"type": "text", "text": V2_RULES_E4AA983},
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

    def test_stale_extra_part_is_replaced_by_factory_product_not_dict(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        old_hint = build_runtime_hint(SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS)
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
            self.store.get(self.ev.unified_msg_origin), quality_rules.MAX_RUNTIME_HINT_CHARS
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
        self.assertNotIn("x" * 21, text)


class TestQualityStats(unittest.TestCase):
    def test_top_cliches_sort_ties_stably(self):
        from astrbot_plugin_human_chat_quality.core import QualityStats

        stats = QualityStats(cliche_hits={"zeta": 2, "alpha": 2, "middle": 1})
        self.assertEqual(stats.top_cliches(3), [("alpha", 2), ("zeta", 2), ("middle", 1)])


class TestResponseTextExtraction(unittest.TestCase):
    """C11：回复文本提取的 result_chain 兜底路径。"""

    def test_completion_text_used_first(self):
        self.assertEqual(extract_response_text(FakeLLMResp("正文")), "正文")

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
        self.assertEqual(extract_response_text(resp), "模型输出")

    def test_chain_content_field(self):
        class ContentPart:
            def __init__(self, content):
                self.content = content

        class Chain:
            def __init__(self, parts):
                self.chain = parts

        resp = FakeLLMResp("")
        resp.result_chain = Chain([ContentPart("正文内容")])
        self.assertEqual(extract_response_text(resp), "正文内容")

    def test_empty_all(self):
        self.assertEqual(extract_response_text(FakeLLMResp("")), "")


class TestCoreFlowExtra(unittest.TestCase):
    """Core cleanup, counting, and response-recording contracts."""

    def setUp(self):
        self.dir = temporary_directory(self)
        self.store = RuntimeStateStore(os.path.join(self.dir, "s.json"), 14, 8, ())
        self.core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=FakePart)
        self.ev = FakeEvent("aiocqhttp:GroupMessage:111")

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
        runtime = build_runtime_hint(SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS)
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": runtime}]}]
        return req

    def test_global_off_cleans_owned_history_without_counting_injection(self):
        core = HumanChatQualityCore(AppConfig.from_config({"enabled": False}), self.store, text_part_factory=FakePart)
        req = self._request_with_owned_blocks()
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.system_prompt, "原人设")
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(core.injection_count, 0)

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
        self.assertEqual(self.core.injection_count, 0)

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
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": V2_RULES_E4AA983}]}]
        asyncio.run(core.on_llm_request(self.ev, req))
        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertIn(STABLE_RULE_MARKER, req.system_prompt)

    def test_missing_text_part_factory_removes_existing_runtime_hint(self):
        for _ in range(3):
            asyncio.run(self.store.record_response(self.ev.unified_msg_origin, "好的，回答"))
        core = HumanChatQualityCore(AppConfig.from_config(None), self.store, text_part_factory=None)
        old_hint = build_runtime_hint(SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS)
        req = FakeReq()
        req.contexts = [{"role": "user", "content": [{"type": "text", "text": old_hint}]}]

        asyncio.run(core.on_llm_request(self.ev, req))

        self.assertEqual(req.contexts[0]["content"], [])
        self.assertEqual(req.extra_user_content_parts, [])

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

    def test_response_signals_are_detected_once(self):
        core_detect = core_module.detect_cliches
        store_detect = runtime_state_module.detect_cliches
        with (
            mock.patch.object(core_module, "detect_cliches", wraps=core_detect) as core_mock,
            mock.patch.object(runtime_state_module, "detect_cliches", wraps=store_detect) as store_mock,
        ):
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好问题，回答")))

        self.assertEqual(core_mock.call_count + store_mock.call_count, 1)

    def test_cleanup_stats_count_all_removed_blocks(self):
        runtime = build_runtime_hint(SessionState(avoid_openers=["旧开头"]), quality_rules.MAX_RUNTIME_HINT_CHARS)
        req = FakeReq(system_prompt=f"{build_stable_rules()}\n\n{build_stable_rules()}")
        req.contexts = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": V2_RULES_E4AA983},
                    {"type": "text", "text": runtime},
                    {"type": "text", "text": runtime},
                ],
            }
        ]

        asyncio.run(self.core.on_llm_request(FakeEvent(""), req))

        self.assertEqual(self.core.stats.legacy_blocks_removed, 3)
        self.assertEqual(self.core.stats.stale_hints_removed, 2)

    def test_runtime_hint_missed_counts_repetition_after_hint(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        # 第四轮请求注入提醒（avoid_openers=["好的"]），回复仍用同一开头 → 计一次忽略
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，还在重复")))
        self.assertEqual(self.core.stats.runtime_hint_missed, 1)
        # 下一轮换了开头，不再计数
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(self.ev, req))
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("换了个自然开头")))
        self.assertEqual(self.core.stats.runtime_hint_missed, 1)

    def test_runtime_hint_missed_not_counted_without_hint(self):
        for _ in range(3):
            req = FakeReq()
            asyncio.run(self.core.on_llm_request(self.ev, req))
            asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("好的，回答")))
        # 无提醒注入的响应（如另一会话）：不计数
        asyncio.run(self.core.on_llm_response(FakeEvent("aiocqhttp:GroupMessage:999"), FakeLLMResp("好的，重复")))
        self.assertEqual(self.core.stats.runtime_hint_missed, 0)

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

    def test_runtime_hint_missed_only_checks_items_that_were_injected(self):
        first = "第一项第一项第一项第一项第一项"
        second = "第二项第二项第二项第二项第二项"
        self.store.sessions[self.ev.unified_msg_origin] = SessionState(avoid_openers=[first, second])
        core = HumanChatQualityCore(
            AppConfig.from_config({"max_runtime_hint_chars": 80}), self.store, text_part_factory=FakePart
        )
        req = FakeReq()

        asyncio.run(core.on_llm_request(self.ev, req))
        asyncio.run(core.on_llm_response(self.ev, FakeLLMResp(second)))

        self.assertIn(first, req.extra_user_content_parts[0].text)
        self.assertNotIn(second, req.extra_user_content_parts[0].text)
        self.assertEqual(core.stats.runtime_hint_missed, 0)

    def test_runtime_hint_tracking_is_fifo_per_session(self):
        self.store.sessions[self.ev.unified_msg_origin] = SessionState(avoid_openers=["第一项"])
        asyncio.run(self.core.on_llm_request(self.ev, FakeReq()))
        self.store.sessions[self.ev.unified_msg_origin] = SessionState(avoid_openers=["第二项"])
        asyncio.run(self.core.on_llm_request(self.ev, FakeReq()))

        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("第二项")))
        self.assertEqual(self.core.stats.runtime_hint_missed, 0)
        asyncio.run(self.core.on_llm_response(self.ev, FakeLLMResp("第二项")))
        self.assertEqual(self.core.stats.runtime_hint_missed, 1)

    def test_no_origin_skips_everything(self):
        ev = FakeEvent("")
        req = FakeReq()
        asyncio.run(self.core.on_llm_request(ev, req))
        self.assertEqual(req.system_prompt, "原人设：你是XX")
        asyncio.run(self.core.on_llm_response(ev, FakeLLMResp("好的，回答")))
        self.assertEqual(self.store.sessions, {})

    def test_unknown_stable_string_is_preserved_across_requests(self):
        req = FakeReq()
        req.contexts = [{"role": "user", "content": "[Human Chat Quality Rules v2]\n旧规则块"}]
        asyncio.run(self.core.on_llm_request(self.ev, req))
        asyncio.run(self.core.on_llm_request(self.ev, req))
        self.assertEqual(req.contexts[0]["content"], "[Human Chat Quality Rules v2]\n旧规则块")

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

        from unittest import mock

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
