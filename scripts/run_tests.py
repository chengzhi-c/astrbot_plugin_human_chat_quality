#!/usr/bin/env python3
"""Run the plugin test suites with a process-friendly exit code."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from typing import TextIO

REPO = Path(__file__).resolve().parents[1]


def run_suite(suite: unittest.TestSuite, stream: TextIO | None = None) -> int:
    result = unittest.TextTestRunner(stream=stream or io.StringIO()).run(suite)
    failed = result.failures or result.errors or result.skipped or result.unexpectedSuccesses
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    if (argv or sys.argv[1:]) != ["all"]:
        print("usage: python scripts/run_tests.py all", file=sys.stderr)
        return 2
    sys.path.insert(0, str(REPO))
    suite = unittest.defaultTestLoader.discover(str(REPO / "tests"))
    return run_suite(suite, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
