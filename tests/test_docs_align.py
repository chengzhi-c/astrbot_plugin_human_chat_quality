"""文档/元数据与代码事实源对齐护栏。

防止 README「会拦」例句漂移成绿样、版本多源不一致。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from helpers import get_main, get_runtime_state, load_plugin_package

load_plugin_package()
main = get_main()
runtime_state = get_runtime_state()

ROOT = Path(__file__).resolve().parent.parent

# README「会拦/会提醒」区块里允许出现的结构标签（非 DEFAULT_ENDINGS 字符串）
_ALLOWED_STRUCTURE_LABELS = frozenset({"破折号连发", "然而连发", "——", "然而"})

# 明确禁止再写回 README 作为默认拦截例句的绿样/已移除信号
_FORBIDDEN_README_EXAMPLES = frozenset(
    {
        "祝您愉快",
        "这是一个很好的问题",
        "总的来说",
        "综上所述",
    }
)


def _readme_text() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _metadata_version() -> str:
    """与生产同一读法：复用 main._read_metadata_version。"""
    return main._read_metadata_version()


def _backtick_phrases(section: str) -> list[str]:
    return re.findall(r"`([^`]+)`", section)


class ReadmeAlignTest(unittest.TestCase):
    def test_blocked_examples_are_real_signals(self):
        """README 拦截例句必须落在 DEFAULT_ENDINGS 或结构标签，且不得出现已移除绿样。"""
        DEFAULT_ENDINGS = runtime_state.DEFAULT_ENDINGS

        text = _readme_text()
        m = re.search(r"## 它会提醒模型避开这些\n(.*?)(\n## |\Z)", text, re.S)
        self.assertIsNotNone(m, "README 缺少「它会提醒模型避开这些」章节")
        section = m.group(1)
        phrases = _backtick_phrases(section)
        self.assertTrue(phrases, "拦截章节应有反引号例句")

        endings = set(DEFAULT_ENDINGS)
        for phrase in phrases:
            if phrase in _ALLOWED_STRUCTURE_LABELS:
                continue
            if (
                phrase in {"custom_cliches", "DEFAULT_ENDINGS"}
                or phrase.isdigit()
                or phrase.startswith("/")
                or phrase.startswith("——")
            ):
                continue
            self.assertNotIn(phrase, _FORBIDDEN_README_EXAMPLES, f"README 不得再列举已移除信号: {phrase}")
            self.assertIn(
                phrase,
                endings,
                f"README 例句 {phrase!r} 不在 DEFAULT_ENDINGS，也不是结构标签",
            )

    def test_readme_states_history_replace_semantics(self):
        text = _readme_text()
        self.assertIn("原位替换", text)
        self.assertNotIn("也不会写进聊天历史", text)

    def test_readme_does_not_pin_version_literal(self):
        """版本以 metadata/CHANGELOG 为准，README 不写死 vX.Y.Z。"""
        text = _readme_text()
        self.assertIsNone(re.search(r"当前版本\s*\*\*v?\d+\.\d+", text))
        self.assertIn("CHANGELOG.md", text)
        self.assertIn("metadata.yaml", text)


class VersionSingleSourceTest(unittest.TestCase):
    def test_plugin_version_matches_metadata(self):
        self.assertEqual(main.PLUGIN_VERSION, _metadata_version())

    def test_metadata_desc_has_no_version_narrative(self):
        meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        for line in meta.splitlines():
            if line.strip().startswith("desc:"):
                self.assertNotRegex(line, r"v\d+\.\d+")
                return
        self.fail("metadata.yaml missing desc")


class DefaultEndingsSurfaceTest(unittest.TestCase):
    def test_no_split_ending_aliases(self):
        self.assertFalse(hasattr(runtime_state, "SERVICE_ENDINGS"))
        self.assertFalse(hasattr(runtime_state, "CHEER_ENDINGS"))
        self.assertGreaterEqual(len(runtime_state.DEFAULT_ENDINGS), 10)


class RegisterRemovalGuardTest(unittest.TestCase):
    def test_no_register_decorator_and_star_contract(self):
        """回归护栏：宿主 Star.__init_subclass__ 自动注册子类，@register 已弃用。

        真实注册契约是"继承 Star"（宿主 base.py __init_subclass__），
        而非"类名以 plugin 结尾"（_get_classes 仅旧路径使用，本插件不走）。
        """
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("@register", src)
        import_lines = [line for line in src.splitlines() if line.startswith(("from ", "import "))]
        self.assertFalse(any("register" in line for line in import_lines))
        self.assertIn("class HumanChatQualityPlugin(Star)", src)  # 继承契约本体


if __name__ == "__main__":
    unittest.main()
