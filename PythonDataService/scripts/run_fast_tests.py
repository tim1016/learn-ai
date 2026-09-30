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
import time
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
    "tests/scripts",
    # The spec layer's own root, beside the package it tests (#2485): it was
    # silently uncollected, so its stale tests failed on master unnoticed.
    # Its conftest primes POLYGON_API_KEY and the Signal Program source
    # anchor, the same two facts tests/conftest.py primes for the main root.
    "app/engine/strategy/spec/tests",
    # #2619: suites only the daily run collected went red for weeks while
    # every PR stayed green. The cheap, dependency-light ones join the gate
    # so a signature change (deploy(exit_terms=...)), a launch preflight
    # probe, or a fixture-hash drift fails the PR that causes it, not the
    # next morning's run. Postgres-backed suites (backtest_runs/
    # test_service_db, the grid-search receipt-minting parity test) stay
    # daily-only: they skip without POSTGRES_URL by design.
    "tests/installation_migration",
    "tests/lean_sidecar",
    "tests/research/ml",
    "app/engine/tests",
    "tests/test_statistics.py",
)
DAILY_ONLY_PATHS = (
    "tests/unit/data_lake",
    "tests/integration/data_lake",
)


def pytest_command(
    extra_paths: Sequence[str],
    *,
    shard_index: int | None = None,
    shard_count: int | None = None,
) -> list[str]:
    """Build the bounded PR-suite command."""
    if (shard_index is None) != (shard_count is None):
        raise ValueError("shard_index and shard_count must be provided together")

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
    if shard_index is not None and shard_count is not None:
        command.extend(
            (
                "-p",
                "scripts.pytest_shard",
                "--pr-shard-index",
                str(shard_index),
                "--pr-shard-count",
                str(shard_count),
            )
        )
    return command


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - CI and supported developer hosts are POSIX
            process.kill()
    except ProcessLookupError:
        # The coordinator can finish between wait(timeout=...) expiring and the
        # kill. It still crossed the budget and returns 124 below.
        pass


def report_elapsed_seconds(
    elapsed: float,
    *,
    shard_index: int | None,
    shard_count: int | None,
    exceeded_budget: bool,
) -> str:
    """Report the run's wall time to the log and the CI step summary.

    Each shard prints its own time (#2682) so budget drift is visible in a
    run's summary page before a shard fails a PR at 99 % with no test
    failure. ``GITHUB_STEP_SUMMARY`` is set by GitHub Actions only.
    """
    scope = (
        f"shard {shard_index}/{shard_count}"
        if shard_index is not None and shard_count is not None
        else "unsharded"
    )
    message = (
        f"Python PR tests ({scope}) took {elapsed:.1f}s of the "
        f"{TEST_BUDGET_SECONDS}-second budget"
        + (" (exceeded)" if exceeded_budget else "")
    )
    logger.info("%s", message)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"{message}\n")
    return message


def run_fast_tests(
    extra_paths: Sequence[str],
    *,
    shard_index: int | None = None,
    shard_count: int | None = None,
) -> int:
    """Run pytest and return 124 when the suite exceeds two minutes."""
    started = time.monotonic()
    process = subprocess.Popen(
        pytest_command(
            extra_paths,
            shard_index=shard_index,
            shard_count=shard_count,
        ),
        start_new_session=os.name == "posix",
    )
    try:
        returncode = process.wait(timeout=TEST_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        process.wait()
        report_elapsed_seconds(
            time.monotonic() - started,
            shard_index=shard_index,
            shard_count=shard_count,
            exceeded_budget=True,
        )
        logger.error(
            "Python PR tests exceeded the hard %d-second budget. "
            "Move expensive coverage to the daily suite or make it faster.",
            TEST_BUDGET_SECONDS,
        )
        return 124
    report_elapsed_seconds(
        time.monotonic() - started,
        shard_index=shard_index,
        shard_count=shard_count,
        exceeded_budget=False,
    )
    return returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shard",
        metavar="INDEX/COUNT",
        help="run one deterministic, one-based CI shard",
    )
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

    shard_index: int | None = None
    shard_count: int | None = None
    if args.shard is not None:
        try:
            index_text, count_text = args.shard.split("/", maxsplit=1)
            shard_index = int(index_text)
            shard_count = int(count_text)
        except ValueError:
            parser.error("--shard must use INDEX/COUNT with integer values")
        if shard_count < 1 or not 1 <= shard_index <= shard_count:
            parser.error("--shard INDEX must be between 1 and COUNT")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return run_fast_tests(
        args.test_paths,
        shard_index=shard_index,
        shard_count=shard_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
