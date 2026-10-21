"""Run the quick Python tests: balanced to finish in two minutes, warned past that.

A quick test is any test not marked ``slow``, wherever it lives: the runner
collects pytest.ini's ``testpaths``, the same roots the daily run collects.
Tests that need PostgreSQL skip without ``POSTGRES_URL`` and run daily, with
the ``slow`` ones.

Time never fails the run (owner decision, 2026-10-20): only the tests decide
the exit code. A shard past its target says so in the log, the step summary
and a GitHub warning annotation, and is the next one to trim.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: What a shard is balanced to finish within. A run past it still passes or
#: fails on its tests alone, and warns: trim its slowest tests into the daily run.
TEST_TARGET_SECONDS = 120


def pytest_command(
    *,
    shard_index: int | None = None,
    shard_count: int | None = None,
    junitxml: Path | None = None,
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
        # A shard's long tests can sit in one file; idle workers take queued
        # tests from busy ones instead of waiting out the tail.
        "--dist",
        "worksteal",
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
    if junitxml is not None:
        # xunit1 names each test's file, which the shard balance is dealt by.
        command.extend(("--junitxml", str(junitxml), "-o", "junit_family=xunit1"))
    return command


def report_elapsed_seconds(
    elapsed: float,
    *,
    shard_index: int | None,
    shard_count: int | None,
) -> str:
    """Report the run's wall time to the log and the CI step summary.

    Each shard prints its own time (#2682) so budget drift is visible in a
    run's summary page. A shard past its target also raises a GitHub warning
    annotation, which shows on the run without opening the log.
    ``GITHUB_STEP_SUMMARY`` and ``GITHUB_ACTIONS`` are set by GitHub Actions only.
    """
    scope = (
        f"shard {shard_index}/{shard_count}"
        if shard_index is not None and shard_count is not None
        else "unsharded"
    )
    over_target = elapsed > TEST_TARGET_SECONDS
    message = (
        f"Python PR tests ({scope}) took {elapsed:.1f}s of the "
        f"{TEST_TARGET_SECONDS}-second target" + (" (over target)" if over_target else "")
    )
    logger.log(logging.WARNING if over_target else logging.INFO, "%s", message)
    if over_target and os.environ.get("GITHUB_ACTIONS") == "true":
        # A workflow command is read from stdout; the log above goes to stderr.
        sys.stdout.write(
            f"::warning title=Python PR tests ({scope}) are over their "
            f"{TEST_TARGET_SECONDS}-second target::Took {elapsed:.1f}s. This does not fail "
            "the run. To trim it, mark its slowest tests slow so they run in the daily "
            "suite, or rebalance the shards.\n"
        )
        sys.stdout.flush()
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
    junitxml: Path | None = None,
) -> int:
    """Run pytest and return pytest's own exit code, however long it took."""
    started = time.monotonic()
    returncode = subprocess.run(
        pytest_command(
            shard_index=shard_index,
            shard_count=shard_count,
            junitxml=junitxml,
        ),
        check=False,
    ).returncode
    report_elapsed_seconds(
        time.monotonic() - started,
        shard_index=shard_index,
        shard_count=shard_count,
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
        "--junitxml",
        type=Path,
        help="save each test's outcome and seconds, to calibrate the shard balance on CI hardware (#3144)",
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
        junitxml=args.junitxml,
    )


if __name__ == "__main__":
    raise SystemExit(main())
