#!/usr/bin/env python3
"""Compare the vendored lite fixture and injected core against an upstream natural-talk checkout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 上游日常对话与回答问题核心准绳标识集合
REQUIRED_GUIDELINE_TAGS = (
    "D1",
    "D2",
    "D4",
    "D5",
    "D6",
    "B1",
    "B3",
    "B4",
    "B5",
    "B8",
    "B10",
    "C1",
    "C3",
    "C4",
)

# 严禁渗入日常对话与问答规则的脱节残留词（小说氛围/成文清理）
FORBIDDEN_DESYNC_PHRASES = (
    "成文清理时",
    "声音填满空间",
    "静有重量",
    "世界退回壳里",
    "智慧的导师",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check synchronization with natural-talk upstream.")
    parser.add_argument("--upstream", required=True, help="path to a natural-talk checkout")
    args = parser.parse_args(argv)
    upstream = Path(args.upstream)

    skill_path = upstream / "SKILL.md"
    dialogue_path = upstream / "references" / "dialogue.md"

    polish_path = upstream / "references" / "polish.md"
    scan_path = upstream / "scripts" / "scan-mechanical.py"

    if not skill_path.is_file():
        print(f"upstream SKILL.md missing: {skill_path}", file=sys.stderr)
        return 2
    if not dialogue_path.is_file():
        print(f"upstream references/dialogue.md missing: {dialogue_path}", file=sys.stderr)
        return 2

    upstream_parts = [skill_path.read_text(encoding="utf-8"), dialogue_path.read_text(encoding="utf-8")]
    if polish_path.is_file():
        upstream_parts.append(polish_path.read_text(encoding="utf-8"))
    if scan_path.is_file():
        upstream_parts.append(scan_path.read_text(encoding="utf-8"))
    upstream_content = "\n".join(upstream_parts)

    # 验证上游单一事实源中是否包含核心准绳定义
    missing_in_upstream = [tag for tag in REQUIRED_GUIDELINE_TAGS if tag not in upstream_content]
    if missing_in_upstream:
        print(f"upstream is missing guideline definitions: {missing_in_upstream}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(REPO))
    from tests._support import ensure_plugin_package

    ensure_plugin_package()
    from astrbot_plugin_human_chat_quality.quality_rules import _LITE_CORE

    # 验证本地核心规则与本地 fixture 严格一致
    fixture_path = REPO / "tests" / "fixtures" / "system-prompt-lite.txt"
    if not fixture_path.is_file():
        print(f"local fixture missing: {fixture_path}", file=sys.stderr)
        return 1
    fixture_text = fixture_path.read_text(encoding="utf-8")
    if _LITE_CORE != fixture_text:
        print("quality_rules._LITE_CORE does not match tests/fixtures/system-prompt-lite.txt", file=sys.stderr)
        return 1

    # 验证本地规则覆盖日常问答核心准绳
    missing_in_core = [f"[{tag}]" for tag in REQUIRED_GUIDELINE_TAGS if f"[{tag}]" not in _LITE_CORE]
    if missing_in_core:
        print(f"quality_rules._LITE_CORE is missing required guideline tags: {missing_in_core}", file=sys.stderr)
        return 1

    # 验证脱节残留词未渗入
    leaked = [phrase for phrase in FORBIDDEN_DESYNC_PHRASES if phrase in _LITE_CORE]
    if leaked:
        print(f"quality_rules._LITE_CORE contains desynchronized phrases: {leaked}", file=sys.stderr)
        return 1

    print("upstream sync: OK (dialogue & QA guidelines verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
