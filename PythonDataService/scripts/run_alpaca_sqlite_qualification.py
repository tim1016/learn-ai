"""Run deterministic Alpaca SQLite Clerk adversarial and scale qualification."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    atomic_write_text,
)
from app.broker.alpaca.clerk.sqlite.qualification import (
    SMOKE_ADVERSARIAL_TESTS,
    qualification_markdown,
    run_performance_profile,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.run_alpaca_sqlite_qualification",
        description="Run broker-free SQLite Clerk qualification evidence.",
    )
    parser.add_argument("--profile", choices=("smoke", "full"), required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument(
        "--skip-adversarial",
        action="store_true",
        help="Performance-only developer iteration; never use for qualification evidence.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_performance_profile(args.profile)
    if args.skip_adversarial:
        adversarial = {
            "status": "SKIPPED",
            "reason": "explicit --skip-adversarial developer iteration",
            "tests": [],
        }
    else:
        tests = (
            SMOKE_ADVERSARIAL_TESTS
            if args.profile == "smoke"
            else ("tests/broker/alpaca/clerk/sqlite", "tests/broker/alpaca/test_trade_updates.py")
        )
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *tests],
            check=False,
            capture_output=True,
            text=True,
        )
        adversarial = {
            "status": "PASSED" if completed.returncode == 0 else "FAILED",
            "return_code": completed.returncode,
            "tests": list(tests),
            "stdout_tail": completed.stdout[-8_000:],
            "stderr_tail": completed.stderr[-8_000:],
        }
    report["adversarial"] = adversarial
    atomic_write_json(args.json_output, report)
    markdown = qualification_markdown(report) + "\n" + _adversarial_markdown(adversarial)
    atomic_write_text(args.markdown_output, markdown)
    sys.stdout.write(json.dumps(_summary(report), sort_keys=True) + "\n")
    passed = (
        adversarial["status"] in {"PASSED", "SKIPPED"}
        and report["performance_budget"]["status"] == "PASSED"
    )
    return 0 if passed else 1


def _adversarial_markdown(adversarial: dict[str, Any]) -> str:
    return (
        "## Adversarial campaign\n\n"
        f"- Status: `{adversarial['status']}`\n"
        f"- Tests: `{len(adversarial['tests'])}` selected path(s)/node(s)\n"
    )


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": report["profile"],
        "generated_at_ms": report["generated_at_ms"],
        "scale_count": len(report["scales"]),
        "adversarial_status": report["adversarial"]["status"],
        "performance_budget_status": report["performance_budget"]["status"],
    }


if __name__ == "__main__":
    sys.exit(main())
