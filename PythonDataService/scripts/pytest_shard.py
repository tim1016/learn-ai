"""Deterministically partition the test files across CI jobs.

Each shard collects only its own test files: the others are ignored before
pytest imports them. Importing and assertion-rewriting every test module was
most of each shard's time while every shard collected the whole suite
(#2751), and a file's tests still spread over the shard's xdist workers.

Files whose tests appear in ``pr_shard_durations.json`` are dealt to the
shards by longest-processing-time-first balance over their summed measured
durations, so a slow app-boot suite cannot pile onto one shard by hash luck
(#2682: the hash-only partition put shard 5/12 at 103-109 s of the 120 s
budget while other shards idled). Files missing from it — new, renamed, or
holding only sub-5 ms tests — keep the stable sha256 hash assignment: their
times are noise at shard scale, and the fallback keeps a stale durations file
harmless rather than load-bearing. A durations file that matches none of the
collected test files is a key mismatch, not staleness, and fails the run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from xdist.workermanage import WorkerController

DURATIONS_PATH = Path(__file__).with_name("pr_shard_durations.json")
_MATCH_REPORT_KEY = "pr_shard_durations_match"
_MATCH_REPORT = pytest.StashKey[str]()


@dataclass
class _FileShard:
    """This run's shard and the deal of every measured test file."""

    index: int
    count: int
    measured: dict[str, int]
    seen: set[str] = field(default_factory=set)

    def owner(self, path: str) -> int:
        return self.measured.get(path) or hash_shard(path, shard_count=self.count)


_FILE_SHARD = pytest.StashKey[_FileShard]()


def hash_shard(key: str, *, shard_count: int) -> int:
    """Return the one-based shard a key (a test file path) hashes to."""
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") % shard_count + 1


def load_pr_shard_durations() -> dict[str, float]:
    """Read the committed per-test durations that drive shard balance.

    The file is committed on purpose: a missing or unparsable file silently
    reverts every shard to the imbalanced hash partition, so both fail the
    run loudly instead. Regenerate with ``python -m
    scripts.update_pr_shard_durations``.
    """
    try:
        raw = json.loads(DURATIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{DURATIONS_PATH.name} is missing; regenerate it with "
            "'python -m scripts.update_pr_shard_durations'"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{DURATIONS_PATH} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(nodeid, str) and isinstance(duration, (int, float))
        for nodeid, duration in raw.items()
    ):
        raise ValueError(
            f"{DURATIONS_PATH} must be a JSON object of nodeid to seconds"
        )
    return {nodeid: float(duration) for nodeid, duration in raw.items()}


def assign_shards(
    keys: Iterable[str],
    *,
    shard_count: int,
    durations: dict[str, float],
) -> dict[str, int]:
    """Assign every key to a one-based shard, balancing measured time.

    Known keys are placed longest-first onto the currently lightest shard
    (LPT); the tie-break on the key keeps the deal deterministic. Unknown
    keys fall back to the stable hash shard.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    collected = list(keys)
    measured = sorted(
        ((durations[key], key) for key in collected if key in durations),
        key=lambda entry: (-entry[0], entry[1]),
    )
    loads = [0.0] * shard_count
    assignments: dict[str, int] = {}
    for duration, key in measured:
        shard = min(range(shard_count), key=lambda index: (loads[index], index))
        loads[shard] += duration
        assignments[key] = shard + 1
    for key in collected:
        if key not in assignments:
            assignments[key] = hash_shard(key, shard_count=shard_count)
    return assignments


def file_durations(durations: dict[str, float]) -> dict[str, float]:
    """Sum the measured test durations by the test file in each node id."""
    per_file: dict[str, float] = {}
    for nodeid, duration in durations.items():
        path = nodeid.split("::", maxsplit=1)[0]
        per_file[path] = per_file.get(path, 0.0) + duration
    return per_file


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pr-shard")
    group.addoption("--pr-shard-index", type=int)
    group.addoption("--pr-shard-count", type=int)


def pytest_configure(config: pytest.Config) -> None:
    # Validated here, not at collection: under xdist collection runs in the
    # workers, where a usage error loses its message.
    shard_index = config.getoption("pr_shard_index")
    shard_count = config.getoption("pr_shard_count")
    if shard_index is None and shard_count is None:
        return
    if shard_index is None or shard_count is None:
        raise pytest.UsageError("both PR shard options are required")
    if not 1 <= shard_index <= shard_count:
        raise pytest.UsageError(
            f"--pr-shard-index {shard_index} must be between 1 and "
            f"--pr-shard-count {shard_count}"
        )
    per_file = file_durations(load_pr_shard_durations())
    config.stash[_FILE_SHARD] = _FileShard(
        index=shard_index,
        count=shard_count,
        measured=assign_shards(per_file.keys(), shard_count=shard_count, durations=per_file),
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    shard = config.stash.get(_FILE_SHARD, None)
    if (
        shard is None
        or not collection_path.is_relative_to(config.rootpath)
        or not any(fnmatch(collection_path.name, glob) for glob in config.getini("python_files"))
    ):
        return None
    path = collection_path.relative_to(config.rootpath).as_posix()
    shard.seen.add(path)
    # None, never False: False would override every other ignore rule.
    return None if shard.owner(path) == shard.index else True


def pytest_collection_modifyitems(config: pytest.Config) -> None:
    shard = config.stash.get(_FILE_SHARD, None)
    if shard is None:
        return

    matched = len(shard.seen & shard.measured.keys())
    if shard.measured and shard.seen and matched == 0:
        # A key mismatch (another rootdir, a renamed tree) would otherwise
        # quietly hash-deal every file and bring the imbalance back.
        raise ValueError(
            f"none of the {len(shard.measured)} test files in {DURATIONS_PATH.name} "
            f"match the {len(shard.seen)} collected test files; regenerate it with "
            "'python -m scripts.update_pr_shard_durations'"
        )
    report = (
        f"PR shard {shard.index}/{shard.count}: {matched} of {len(shard.seen)} "
        f"test files matched {DURATIONS_PATH.name}; "
        f"the other {len(shard.seen) - matched} use the hash shard"
    )
    worker_output = getattr(config, "workeroutput", None)
    if worker_output is not None:
        # An xdist worker has no terminal; its controller prints the line.
        worker_output[_MATCH_REPORT_KEY] = report
    else:
        config.stash[_MATCH_REPORT] = report


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: WorkerController, error: object | None) -> None:
    report = getattr(node, "workeroutput", {}).get(_MATCH_REPORT_KEY)
    if report is not None:
        node.config.stash[_MATCH_REPORT] = report


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    config: pytest.Config,
) -> None:
    report = config.stash.get(_MATCH_REPORT, None)
    if report is not None:
        terminalreporter.write_line(report)
