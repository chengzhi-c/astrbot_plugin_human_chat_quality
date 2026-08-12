#!/usr/bin/env python3
"""Run the plugin test suites with a process-friendly exit code."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from typing import TextIO

REPO = Path(__file__).resolve().parents[1]
SUITES = {
    "core": ("tests.test_core_flow", "tests.test_quality_rules", "tests.test_runtime_state"),
    "host": ("tests.test_host_contract",),
}


def run_suite(suite: unittest.TestSuite, stream: TextIO | None = None) -> int:
    result = unittest.TextTestRunner(stream=stream or io.StringIO()).run(suite)
    failed = result.failures or result.errors or result.skipped or result.unexpectedSuccesses
    return 1 if failed else 0


def load_suite(name: str) -> unittest.TestSuite:
    if name == "all":
        return unittest.defaultTestLoader.discover(str(REPO / "tests"))
    return unittest.defaultTestLoader.loadTestsFromNames(SUITES[name])


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 1 or args[0] not in {"all", *SUITES}:
        print("usage: python scripts/run_tests.py {all|core|host}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(REPO))
    return run_suite(load_suite(args[0]), sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
