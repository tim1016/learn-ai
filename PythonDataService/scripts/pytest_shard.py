"""Deterministically partition collected pytest cases across CI jobs.

Tests present in ``pr_shard_durations.json`` are dealt to the shards by
longest-processing-time-first balance over their measured durations, so a
slow app-boot suite cannot pile onto one shard by hash luck (#2682: the
hash-only partition put shard 5/12 at 103-109 s of the 120 s budget while
other shards idled). Tests missing from the file — new, renamed, or
sub-5 ms — keep the stable sha256 hash assignment: their times are noise
at shard scale, and the fallback keeps a stale durations file harmless
rather than load-bearing. A file that matches none of the collected tests
is a key mismatch, not staleness, and fails the run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from xdist.workermanage import WorkerController

DURATIONS_PATH = Path(__file__).with_name("pr_shard_durations.json")
_MATCH_REPORT_KEY = "pr_shard_durations_match"
_MATCH_REPORT = pytest.StashKey[str]()


def hash_shard(nodeid: str, *, shard_count: int) -> int:
    """Return the one-based shard a pytest node hashes to."""
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
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
    nodeids: Iterable[str],
    *,
    shard_count: int,
    durations: dict[str, float],
) -> dict[str, int]:
    """Assign every node id to a one-based shard, balancing measured time.

    Known tests are placed longest-first onto the currently lightest shard
    (LPT); the tie-break on node id keeps the deal deterministic. Unknown
    tests fall back to the stable hash shard.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    collected = list(nodeids)
    measured = sorted(
        (
            (durations[nodeid], nodeid)
            for nodeid in collected
            if nodeid in durations
        ),
        key=lambda entry: (-entry[0], entry[1]),
    )
    loads = [0.0] * shard_count
    assignments: dict[str, int] = {}
    for duration, nodeid in measured:
        shard = min(range(shard_count), key=lambda index: (loads[index], index))
        loads[shard] += duration
        assignments[nodeid] = shard + 1
    for nodeid in collected:
        if nodeid not in assignments:
            assignments[nodeid] = hash_shard(nodeid, shard_count=shard_count)
    return assignments


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


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    shard_index = config.getoption("pr_shard_index")
    shard_count = config.getoption("pr_shard_count")
    if shard_index is None or shard_count is None:
        return

    durations = load_pr_shard_durations()
    matched = sum(1 for item in items if item.nodeid in durations)
    if durations and items and matched == 0:
        # A key mismatch (another rootdir, a renamed tree) would otherwise
        # quietly hash-deal every test and bring the imbalance back.
        raise ValueError(
            f"none of the {len(durations)} tests in {DURATIONS_PATH.name} match "
            f"the {len(items)} collected tests; regenerate it with "
            "'python -m scripts.update_pr_shard_durations'"
        )
    report = (
        f"PR shard {shard_index}/{shard_count}: {matched} of {len(items)} "
        f"collected tests matched {DURATIONS_PATH.name}; "
        f"the other {len(items) - matched} use the hash shard"
    )
    worker_output = getattr(config, "workeroutput", None)
    if worker_output is not None:
        # An xdist worker has no terminal; its controller prints the line.
        worker_output[_MATCH_REPORT_KEY] = report
    else:
        config.stash[_MATCH_REPORT] = report

    assignments = assign_shards(
        (item.nodeid for item in items),
        shard_count=shard_count,
        durations=durations,
    )
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        destination = (
            selected if assignments[item.nodeid] == shard_index else deselected
        )
        destination.append(item)

    config.hook.pytest_deselected(items=deselected)
    items[:] = selected


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
