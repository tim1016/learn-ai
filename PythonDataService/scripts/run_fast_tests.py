"""Run the change-gating Python suite within its two-minute budget.

The complete suite belongs to the daily workflow.  This runner is the single
source of truth for the deterministic, dependency-light baseline used during
development and on pull requests.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Sequence

logger = logging.getLogger(__name__)

TEST_BUDGET_SECONDS = 120
FAST_TEST_PATHS = (
    "tests/unit",
    "tests/indicators",
    "tests/edge",
    "tests/utils",
    "tests/engine",
    "tests/routers",
    "tests/schemas",
    "tests/services",
    "tests/operator",
    "tests/contracts",
    # Keep the whole Alpaca broker surface in the gate: filename-stem change
    # detection cannot reliably map its routers and shared custody modules to
    # their consumers.
    "tests/broker",
)
DAILY_ONLY_PATHS = (
    "tests/unit/data_lake",
    "tests/integration/data_lake",
)


def pytest_command(extra_paths: Sequence[str]) -> list[str]:
    """Build the bounded PR-suite command."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        *FAST_TEST_PATHS,
        *extra_paths,
    ]
    for path in DAILY_ONLY_PATHS:
        command.append(f"--ignore={path}")
    command.extend(("-n", "auto", "-q", "-m", "not slow", "--tb=short"))
    return command


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - CI and supported developer hosts are POSIX
            process.kill()
    except ProcessLookupError:
        # The coordinator can finish between wait(timeout=...) expiring and
        # the kill. It still crossed the budget and returns 124 below.
        pass


def run_fast_tests(extra_paths: Sequence[str]) -> int:
    """Run pytest and return 124 when the suite exceeds two minutes."""
    process = subprocess.Popen(
        pytest_command(extra_paths),
        start_new_session=os.name == "posix",
    )
    try:
        return process.wait(timeout=TEST_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        process.wait()
        logger.error(
            "Python PR tests exceeded the hard %d-second budget. "
            "Move expensive coverage to the daily suite or make it faster.",
            TEST_BUDGET_SECONDS,
        )
        return 124


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-baseline",
        action="store_true",
        help="write the baseline paths, one per line, for CI change detection",
    )
    parser.add_argument("test_paths", nargs="*", help="additional changed test paths")
    args = parser.parse_args(argv)

    if args.list_baseline:
        sys.stdout.write("\n".join(FAST_TEST_PATHS) + "\n")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return run_fast_tests(args.test_paths)


if __name__ == "__main__":
    raise SystemExit(main())
