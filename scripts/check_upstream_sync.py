#!/usr/bin/env python3
"""Compare the vendored lite fixture and injected core against an upstream natural-talk checkout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 清单单源：上游必备准绳标签与禁入词以 tests/fixtures/stable-rules-anchors.json 为准
_ANCHORS_PATH = REPO / "tests" / "fixtures" / "stable-rules-anchors.json"


def _load_anchors() -> tuple[tuple[str, ...], tuple[str, ...]]:
    data = json.loads(_ANCHORS_PATH.read_text(encoding="utf-8"))
    return tuple(data["guideline_tags"]), tuple(data["forbidden"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check synchronization with natural-talk upstream.")
    parser.add_argument("--upstream", required=True, help="path to a natural-talk checkout")
    args = parser.parse_args(argv)
    upstream = Path(args.upstream)
    if not _ANCHORS_PATH.is_file():
        print(f"anchors fixture missing: {_ANCHORS_PATH}", file=sys.stderr)
        return 1
    required_tags, forbidden_phrases = _load_anchors()

    skill_path = upstream / "SKILL.md"
    rules_full_path = upstream / "references" / "rules-full.md"

    scan_path = upstream / "scripts" / "scan-mechanical.py"

    if not skill_path.is_file():
        print(f"upstream SKILL.md missing: {skill_path}", file=sys.stderr)
        return 2
    if not rules_full_path.is_file():
        print(f"upstream references/rules-full.md missing: {rules_full_path}", file=sys.stderr)
        return 2

    # 上游 2026-09-14 起合并 references：dialogue.md 并入 SKILL.md/rules-full.md，
    # 规范源以 SKILL.md + rules-full.md 为准。
    upstream_parts = [skill_path.read_text(encoding="utf-8"), rules_full_path.read_text(encoding="utf-8")]
    fiction_path = upstream / "references" / "fiction.md"
    if fiction_path.is_file():
        upstream_parts.append(fiction_path.read_text(encoding="utf-8"))
    if scan_path.is_file():
        upstream_parts.append(scan_path.read_text(encoding="utf-8"))
    upstream_content = "\n".join(upstream_parts)

    # 验证上游单一事实源中是否包含核心准绳定义
    missing_in_upstream = [tag for tag in required_tags if tag not in upstream_content]
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
    missing_in_core = [f"[{tag}]" for tag in required_tags if f"[{tag}]" not in _LITE_CORE]
    if missing_in_core:
        print(f"quality_rules._LITE_CORE is missing required guideline tags: {missing_in_core}", file=sys.stderr)
        return 1

    # 验证脱节残留词未渗入
    leaked = [phrase for phrase in forbidden_phrases if phrase in _LITE_CORE]
    if leaked:
        print(f"quality_rules._LITE_CORE contains desynchronized phrases: {leaked}", file=sys.stderr)
        return 1

    print("upstream sync: OK (SKILL.md + rules-full.md guidelines verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
