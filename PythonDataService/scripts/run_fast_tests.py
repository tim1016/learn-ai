"""Run the quick Python tests within the two-minute budget.

A quick test is any test not marked ``slow``, wherever it lives: the runner
collects pytest.ini's ``testpaths``, the same roots the daily run collects.
Tests that need PostgreSQL skip without ``POSTGRES_URL`` and run daily, with
the ``slow`` ones.
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


def pytest_command(
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
        "-n",
        "auto",
        "-q",
        "-m",
        "not slow",
        "--tb=short",
    ]
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
        try:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write(f"{message}\n")
        except OSError as exc:
            # The time is a diagnostic; it must never change the gate's verdict.
            logger.warning(
                "Could not append the test time to GITHUB_STEP_SUMMARY %s: %s",
                summary_path,
                exc,
            )
    return message


def run_fast_tests(
    *,
    shard_index: int | None = None,
    shard_count: int | None = None,
) -> int:
    """Run pytest and return 124 when the suite exceeds two minutes."""
    started = time.monotonic()
    process = subprocess.Popen(
        pytest_command(
            shard_index=shard_index,
            shard_count=shard_count,
        ),
        start_new_session=os.name == "posix",
    )
    exceeded = False
    try:
        returncode = process.wait(timeout=TEST_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        _stop_process_group(process)
        process.wait()
        returncode = 124
        exceeded = True
        logger.error(
            "Python PR tests exceeded the hard %d-second budget. "
            "Move expensive coverage to the daily suite or make it faster.",
            TEST_BUDGET_SECONDS,
        )
    report_elapsed_seconds(
        time.monotonic() - started,
        shard_index=shard_index,
        shard_count=shard_count,
        exceeded_budget=exceeded,
    )
    return returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shard",
        metavar="INDEX/COUNT",
        help="run one deterministic, one-based CI shard",
    )
    args = parser.parse_args(argv)

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
        shard_index=shard_index,
        shard_count=shard_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
