import io
import json
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

from scripts import run_tests
from scripts.run_tests import run_suite
from scripts import build_release
from tests._support import temporary_directory


class TestStrictRunner(unittest.TestCase):
    def test_clean_suite_succeeds(self):
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        self.assertEqual(run_suite(suite, io.StringIO()), 0)

    def test_skipped_suite_fails(self):
        @unittest.skip("synthetic skip")
        def skipped():
            pass

        suite = unittest.TestSuite([unittest.FunctionTestCase(skipped)])
        self.assertEqual(run_suite(suite, io.StringIO()), 1)

    def test_named_suites_are_supported(self):
        self.assertGreater(run_tests.load_suite("core").countTestCases(), 0)
        self.assertGreater(run_tests.load_suite("host").countTestCases(), 0)


class TestReleaseBuild(unittest.TestCase):
    def _copy_repo(self, name: str = "downloaded-plugin-main") -> tuple[Path, Path]:
        root = Path(temporary_directory(self))
        repo = root / name
        shutil.copytree(
            Path(__file__).resolve().parents[1],
            repo,
            ignore=shutil.ignore_patterns(".git", "data", ".ruff_cache", "__pycache__", "*.pyc"),
        )
        out_dir = root / "out"
        out_dir.mkdir()
        return repo, out_dir

    def test_archive_uses_metadata_name_and_runtime_manifest(self):
        repo, out_dir = self._copy_repo()
        (repo / "data").mkdir()
        (repo / "data" / "runtime_state.json").write_text("{}", encoding="utf-8")
        (repo / ".ruff_cache").mkdir()
        (repo / ".ruff_cache" / "cache").write_text("cache", encoding="utf-8")

        archive = build_release.build_archive(repo, out_dir)

        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            metadata = json.loads(zf.read("astrbot_plugin_human_chat_quality/_conf_schema.json"))

        expected = {f"astrbot_plugin_human_chat_quality/{path}" for path in build_release.RUNTIME_MANIFEST}
        self.assertEqual(names, expected)
        self.assertEqual(metadata["enabled"]["default"], True)
        self.assertTrue(all(name.startswith("astrbot_plugin_human_chat_quality/") for name in names))
        self.assertFalse(any(name.startswith("astrbot_plugin_human_chat_quality/tests/") for name in names))

    def test_runtime_manifest_preserves_natural_talk_license_notice(self):
        self.assertIn("THIRD_PARTY_NOTICES.md", build_release.RUNTIME_MANIFEST)
        notice = (Path(__file__).resolve().parents[1] / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Natural Talk Contributors", notice)
        self.assertIn("https://github.com/chengzhi-c/natural-talk", notice)
        self.assertIn("Permission is hereby granted", notice)

    def test_version_mismatch_fails_without_archive(self):
        repo, out_dir = self._copy_repo()
        current = build_release.validate_release(repo).version
        changelog = repo / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        changelog.write_text(text.replace(f"## [{current}]", "## [9.9.9]", 1), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "version"):
            build_release.build_archive(repo, out_dir)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_missing_runtime_file_fails(self):
        repo, out_dir = self._copy_repo()
        (repo / "runtime_state.py").unlink()

        with self.assertRaisesRegex(ValueError, "runtime_state.py"):
            build_release.build_archive(repo, out_dir)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_invalid_metadata_name_fails(self):
        repo, out_dir = self._copy_repo()
        metadata = repo / "metadata.yaml"
        text = metadata.read_text(encoding="utf-8")
        metadata.write_text(
            text.replace("name: astrbot_plugin_human_chat_quality", "name: ../private", 1), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "name"):
            build_release.build_archive(repo, out_dir)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_invalid_metadata_version_fails(self):
        repo, out_dir = self._copy_repo()
        current = build_release.validate_release(repo).version
        metadata = repo / "metadata.yaml"
        text = metadata.read_text(encoding="utf-8")
        metadata.write_text(text.replace(f"version: {current}", "version: ../private", 1), encoding="utf-8")
        changelog = repo / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        changelog.write_text(text.replace(f"## [{current}]", "## [../private]", 1), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "version"):
            build_release.build_archive(repo, out_dir)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_core_suite_runs_from_arbitrary_download_directory(self):
        repo, _ = self._copy_repo("renamed-download")
        result = subprocess.run(
            [sys.executable, "-S", "scripts/run_tests.py", "core"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
