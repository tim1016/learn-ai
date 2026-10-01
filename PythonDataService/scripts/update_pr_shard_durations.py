"""Regenerate scripts/pr_shard_durations.json from a measured full run.

Runs the unsharded PR gate (the same roots, markers, and xdist settings as
``scripts.run_fast_tests``) with ``--durations=0`` and records each test's
combined setup+call+teardown seconds. ``scripts.pytest_shard`` deals those
measured tests across shards longest-first so no shard approaches the
120-second budget by hash luck (#2682); tests missing from the file keep
the hash assignment.

The values jitter a few percent between runs and machines — only their
relative sizes drive the balance, so regenerate when the shard times
printed to the CI step summary drift toward the budget, not on every
change.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.pytest_shard import DURATIONS_PATH
from scripts.run_fast_tests import pytest_command

logger = logging.getLogger(__name__)

# The node id runs to the end of the line: parametrize ids may hold spaces.
_DURATION_LINE = re.compile(
    r"^(?P<seconds>[0-9]+(?:\.[0-9]+)?)s "
    r"(?P<phase>call|setup|teardown)\s+(?P<nodeid>.+)$"
)


def parse_durations(output: str) -> dict[str, float]:
    """Combine per-phase durations from ``--durations=0`` output by node id."""
    combined: dict[str, float] = {}
    for line in output.splitlines():
        match = _DURATION_LINE.match(line.strip())
        if match is None:
            continue
        combined[match.group("nodeid")] = round(
            combined.get(match.group("nodeid"), 0.0) + float(match.group("seconds")),
            3,
        )
    return combined


def write_durations(durations: Mapping[str, float]) -> None:
    DURATIONS_PATH.write_text(
        json.dumps(dict(sorted(durations.items())), indent=0, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    completed = subprocess.run(
        [*pytest_command(), "--durations=0"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    durations = parse_durations(completed.stdout)
    if not durations:
        sys.stderr.write(completed.stdout[-2000:])
        sys.stderr.write(completed.stderr[-2000:])
        raise SystemExit("no test durations were parsed from the measuring run")
    write_durations(durations)
    logger.info(
        "pytest exited %d; recorded %d tests, %.1fs combined -> %s",
        completed.returncode,
        len(durations),
        sum(durations.values()),
        DURATIONS_PATH.name,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
