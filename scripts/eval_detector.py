#!/usr/bin/env python3
"""Evaluate detector predictions against the frozen, human-readable fixture."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

repo = Path(__file__).resolve().parents[1]
package_name = "astrbot_plugin_human_chat_quality"
sys.path.insert(0, str(repo.parent))
detect_cliches = import_module(f"{package_name}.signal_detectors").detect_cliches
_is_formal_writing_request = import_module(f"{package_name}.core")._is_formal_writing_request


def _binary_metrics(expected: list[bool], actual: list[bool]) -> dict[str, float | int]:
    tp = sum(want and got for want, got in zip(expected, actual, strict=True))
    fp = sum(not want and got for want, got in zip(expected, actual, strict=True))
    fn = sum(want and not got for want, got in zip(expected, actual, strict=True))
    return {
        "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
        "fp": fp,
        "fn": fn,
    }


def _metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for row in rows:
        category = str(row["category"])
        expected = set(str(item) for item in row["expected_signals"])
        actual = set(detect_cliches(str(row["answer"])))
        bucket = buckets[category]
        for signal in expected & actual:
            bucket["tp"] += 1
        bucket["fp"] += len(actual - expected)
        bucket["fn"] += len(expected - actual)

    categories: dict[str, dict[str, float | int]] = {}
    for category, bucket in sorted(buckets.items()):
        tp, fp, fn = bucket["tp"], bucket["fp"], bucket["fn"]
        categories[category] = {
            "precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
            "fp": fp,
            "fn": fn,
        }
    return {
        "count": len(rows),
        "categories": categories,
        "formal_bypass": _binary_metrics(
            [bool(row["formal_bypass"]) for row in rows],
            [_is_formal_writing_request(SimpleNamespace(text=str(row["user"]))) for row in rows],
        ),
    }


def has_errors(report: dict[str, object]) -> bool:
    for split in report.values():
        if split["formal_bypass"]["fp"] or split["formal_bypass"]["fn"]:
            return True
        if any(metrics["fp"] or metrics["fn"] for metrics in split["categories"].values()):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="return non-zero when any frozen case mismatches")
    args = parser.parse_args(argv)
    rows = json.loads((repo / "tests" / "fixtures" / "detector_eval.json").read_text(encoding="utf-8"))
    report = {
        "dev": _metrics([row for row in rows if row["split"] == "dev"]),
        "holdout": _metrics([row for row in rows if row["split"] == "holdout"]),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if args.check and has_errors(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
