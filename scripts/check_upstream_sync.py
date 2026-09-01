#!/usr/bin/env python3
"""Compare the vendored lite fixture and injected core against an upstream checkout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CLEANUP_SENTENCE = "成文清理时，对话层和 C6 不带入；保护资料引用、事实与结构，默认只输出清理后的正文。\n\n"
REPO = Path(__file__).resolve().parents[1]


def _lite_core_from_fixture(fixture: str) -> str:
    if CLEANUP_SENTENCE not in fixture:
        raise ValueError("lite fixture is missing the cleanup-mode sentence")
    return fixture.replace(CLEANUP_SENTENCE, "").rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True, help="path to a natural-talk checkout")
    args = parser.parse_args(argv)
    upstream = Path(args.upstream)
    lite_path = upstream / "templates" / "system-prompt-lite.txt"
    fixture_path = REPO / "tests" / "fixtures" / "system-prompt-lite.txt"
    if not lite_path.is_file():
        print(f"upstream lite missing: {lite_path}", file=sys.stderr)
        return 2
    upstream_lite = lite_path.read_text(encoding="utf-8")
    fixture = fixture_path.read_text(encoding="utf-8")
    if fixture != upstream_lite:
        print(
            "tests/fixtures/system-prompt-lite.txt differs from upstream templates/system-prompt-lite.txt",
            file=sys.stderr,
        )
        return 1
    sys.path.insert(0, str(REPO))
    from tests._support import ensure_plugin_package

    ensure_plugin_package()
    from astrbot_plugin_human_chat_quality.quality_rules import _LITE_CORE

    expected = _lite_core_from_fixture(fixture)
    if _LITE_CORE != expected:
        print("quality_rules._LITE_CORE is not the lite fixture with the cleanup sentence removed", file=sys.stderr)
        return 1
    print("upstream lite sync: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
