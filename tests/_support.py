import os
import tempfile
import unittest
from pathlib import Path

_DEFAULT_TMPDIR = Path(__file__).resolve().parents[2]


def temporary_directory(test_case: unittest.TestCase) -> str:
    root = os.environ.get("HCQ_TEST_TMPDIR") or _DEFAULT_TMPDIR
    temp_dir = tempfile.TemporaryDirectory(dir=root)
    test_case.addCleanup(temp_dir.cleanup)
    return temp_dir.name
