#!/usr/bin/env python3
"""构建发布 zip：测试预检 + 清单校验 + 版本一致性 + 打包（纯 stdlib）。

用法：在插件仓库根目录运行  python scripts/build_release.py
输出：仓库上级目录  astrbot_plugin_human_chat_quality-v<version>.zip

任一预检失败即拒绝打包（测试红 = 不能发布）。
"""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_NAME = REPO.name

MUST_INCLUDE = (
    "main.py",
    "quality_rules.py",
    "runtime_state.py",
    "__init__.py",
    "_conf_schema.json",
    "metadata.yaml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "tests/test_main_flow.py",
    "tests/test_quality_rules.py",
    "tests/test_runtime_state.py",
    "ruff.toml",
)
# CHANGELOG 条目恒为「## [x.y.z] - 日期」，锚定格式防日期误配
CHANGELOG_VERSION_RE = re.compile(r"^## \[([^\]\s]+)\]")
META_VERSION_RE = re.compile(r'^version:\s*["\']?([^"\'\s]+)')


def _fail(message: str) -> None:
    print(f"[build_release] FAIL: {message}")
    sys.exit(1)


def version_from_metadata() -> str:
    for line in (REPO / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        match = META_VERSION_RE.match(line.strip())
        if match:
            return match.group(1)
    _fail("metadata.yaml 缺少 version 字段")


def version_from_changelog() -> str:
    for line in (REPO / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        match = CHANGELOG_VERSION_RE.match(line.strip())
        if match:
            return match.group(1)
    _fail("CHANGELOG.md 缺少版本条目（## [x.y.z] - 日期）")


def main() -> None:
    # 1. 测试与门禁预检：任一失败即拒绝打包
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=REPO,
        check=True,
    )
    subprocess.run(["ruff", "check", "."], cwd=REPO, check=True)
    subprocess.run(["ruff", "format", "--check", "."], cwd=REPO, check=True)

    # 2. 清单校验
    missing = [rel for rel in MUST_INCLUDE if not (REPO / rel).is_file()]
    if missing:
        _fail(f"发布清单缺失：{', '.join(missing)}")

    # 3. 版本一致性（metadata 与 CHANGELOG 首条）
    meta_version = version_from_metadata()
    changelog_version = version_from_changelog()
    if meta_version != changelog_version:
        print(f"[build_release] WARN: metadata version {meta_version!r} != CHANGELOG 首条 {changelog_version!r}")

    # 4. 打包到仓库外，zip 内带顶层目录名（与 AstrBot 插件目录约定一致）
    out_path = REPO.parent / f"{PLUGIN_NAME}-v{meta_version}.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(MUST_INCLUDE):
            zf.write(REPO / rel, f"{PLUGIN_NAME}/{rel}")
    print(f"[build_release] OK: {out_path}")


if __name__ == "__main__":
    main()
