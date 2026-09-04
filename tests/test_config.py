"""Configuration parsing and schema contract tests."""

import json
import re
import unittest
from dataclasses import fields
from pathlib import Path

from tests._support import ensure_plugin_package

ensure_plugin_package()

from astrbot_plugin_human_chat_quality.constants import MAX_RUNTIME_HINT_CHARS, MIN_RUNTIME_HINT_CHARS
from astrbot_plugin_human_chat_quality.core import AppConfig

EXPECTED_MIN_RUNTIME_HINT_CHARS = MIN_RUNTIME_HINT_CHARS
EXPECTED_MAX_RUNTIME_HINT_CHARS = MAX_RUNTIME_HINT_CHARS


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
        self.assertEqual(MIN_RUNTIME_HINT_CHARS, EXPECTED_MIN_RUNTIME_HINT_CHARS)
        self.assertEqual(MAX_RUNTIME_HINT_CHARS, EXPECTED_MAX_RUNTIME_HINT_CHARS)
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


if __name__ == "__main__":
    unittest.main()
