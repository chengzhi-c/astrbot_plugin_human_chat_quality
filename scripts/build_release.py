#!/usr/bin/env python3
"""Validate and build the plugin release archive."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

RUNTIME_MANIFEST = (
    "main.py",
    "core.py",
    "quality_rules.py",
    "runtime_state.py",
    "signal_detectors.py",
    "protocols.py",
    "constants.py",
    "__init__.py",
    "_conf_schema.json",
    "metadata.yaml",
    "README.md",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE",
)
_CHANGELOG_VERSION_RE = re.compile(r"^## \[([^\]\s]+)\]")
_META_FIELD_RE = re.compile(r"^(name|version):\s*[\"']?([^\"'\s]+)")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ReleaseInfo:
    name: str
    version: str


def _metadata_fields(repo: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (repo / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        match = _META_FIELD_RE.match(line.strip())
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def _changelog_version(repo: Path) -> str:
    for line in (repo / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        match = _CHANGELOG_VERSION_RE.match(line.strip())
        if match:
            return match.group(1)
    raise ValueError("CHANGELOG.md is missing its first version heading")


def validate_release(repo: Path) -> ReleaseInfo:
    repo = repo.resolve()
    missing = [path for path in RUNTIME_MANIFEST if not (repo / path).is_file()]
    if missing:
        raise ValueError(f"release manifest missing: {', '.join(missing)}")

    fields = _metadata_fields(repo)
    name = fields.get("name")
    version = fields.get("version")
    if not name or not _SAFE_NAME_RE.fullmatch(name):
        raise ValueError(f"metadata name is invalid: {name!r}")
    if not version or not _SAFE_NAME_RE.fullmatch(version):
        raise ValueError(f"metadata version is invalid: {version!r}")
    changelog_version = _changelog_version(repo)
    if version != changelog_version:
        raise ValueError(f"metadata version {version!r} does not match changelog {changelog_version!r}")
    return ReleaseInfo(name, version)


def build_archive(repo: Path, out_dir: Path) -> Path:
    repo = repo.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    info = validate_release(repo)
    archive = out_dir / f"{info.name}-v{info.version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for relative in RUNTIME_MANIFEST:
            source = repo / relative
            target = f"{info.name}/{relative}"
            zf.write(source, target)

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        expected = {f"{info.name}/{relative}" for relative in RUNTIME_MANIFEST}
        if names != expected:
            archive.unlink(missing_ok=True)
            raise ValueError("archive contents do not match the runtime manifest")
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts:
                archive.unlink(missing_ok=True)
                raise ValueError("archive contains an unsafe path")
    return archive


def _run_gate(command: list[str], repo: Path) -> None:
    subprocess.run(command, cwd=repo, check=True)


def _compile_command() -> list[str]:
    runtime_python = [path for path in RUNTIME_MANIFEST if path.endswith(".py")]
    return [sys.executable, "-m", "compileall", "-q", *runtime_python, "scripts", "tests"]


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    _run_gate([sys.executable, "scripts/run_tests.py", "all"], repo)
    _run_gate([sys.executable, "-S", "scripts/eval_detector.py", "--check"], repo)
    _run_gate(_compile_command(), repo)
    _run_gate(["ruff", "check", "."], repo)
    _run_gate(["ruff", "format", "--check", "."], repo)
    archive = build_archive(repo, repo.parent)
    print(f"[build_release] OK: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
