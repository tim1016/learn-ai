"""Regenerate scripts/pr_shard_durations.json from a measured full run.

Runs the unsharded PR gate (the same roots, markers, and xdist settings as
``scripts.run_fast_tests``) with ``--durations=0`` and records each test's
combined setup+call+teardown seconds. ``scripts.pytest_shard`` sums them by
test file and deals the files across shards longest-first so no shard
approaches the 120-second target by hash luck (#2682); files missing from the
file keep the hash assignment.

Prefer ``--from-junit``: a local run does not predict a CI shard's time (#3144),
but each PR shard's junit file (the artifact ``python-shard-N-test-results``) holds
the seconds as CI's workers measured them, and a deal built from the mean of a few
such runs balanced a run it had not seen to within 1.15 to 1.30 times the mean
shard load, where the local file left 1.5 times.

The values jitter a few percent between runs and machines — only their
relative sizes drive the balance, so regenerate when the shard times
printed to the CI step summary drift toward the target, not on every
change. Measure with ``PYTEST_XDIST_AUTO_NUM_WORKERS=4``, CI's worker count:
on a many-core host ``-n auto`` runs so many workers that CPU-bound tests
slow down while waiting ones do not, which skews the relative sizes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.etree import ElementTree

from scripts.pytest_shard import DURATIONS_PATH, file_durations
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


def parse_junit(xml_text: str) -> dict[str, float]:
    """Each test's seconds from a ``junit_family=xunit1`` file, keyed ``file::name``.

    The shard deal sums by the file in a node id, so the class between them is
    dropped. A test that cannot name its file or its seconds is refused: a
    silent skip would leave its file out of the balance.
    """
    durations: dict[str, float] = {}
    for case in ElementTree.fromstring(xml_text).iter("testcase"):
        file, name, seconds = case.get("file"), case.get("name"), case.get("time")
        if file is None or name is None or seconds is None:
            raise ValueError(f"a junit testcase without a file, name or time: {case.attrib}")
        key = f"{file}::{name}"
        durations[key] = round(durations.get(key, 0.0) + float(seconds), 3)
    return durations


def mean_durations(runs: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Each test's mean seconds over the runs that held it."""
    seen: dict[str, list[float]] = {}
    for run in runs:
        for nodeid, seconds in run.items():
            seen.setdefault(nodeid, []).append(seconds)
    return {nodeid: round(sum(each) / len(each), 3) for nodeid, each in seen.items()}


def read_junit_run(path: Path) -> dict[str, float]:
    """One run's timings: a junit file, or a directory of the files its shards saved."""
    files = sorted(path.rglob("*.xml")) if path.is_dir() else [path]
    if not files:
        raise SystemExit(f"{path} holds no junit file")
    run: dict[str, float] = {}
    for file in files:
        for nodeid, seconds in parse_junit(file.read_text(encoding="utf-8")).items():
            run[nodeid] = round(run.get(nodeid, 0.0) + seconds, 3)
    return run


def write_durations(durations: Mapping[str, float]) -> None:
    DURATIONS_PATH.write_text(
        json.dumps(dict(sorted(durations.items())), indent=0, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-junit",
        nargs="+",
        type=Path,
        metavar="RUN",
        help=(
            "calibrate from CI instead of measuring here: each RUN is one CI run's junit file, or a directory "
            "of its shards' files (artifacts python-shard-N-test-results); a test's seconds are the mean over "
            "runs, written per test file"
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.from_junit:
        # The deal sums by file, so a CI calibration keeps one entry per file: about 1,200 lines, not 24,000.
        durations = {
            path: round(seconds, 3)
            for path, seconds in file_durations(mean_durations([read_junit_run(run) for run in args.from_junit])).items()
        }
        write_durations(durations)
        logger.info(
            "recorded %d test files, %.1fs combined, from %d CI run(s) -> %s",
            len(durations),
            sum(durations.values()),
            len(args.from_junit),
            DURATIONS_PATH.name,
        )
        return 0
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
