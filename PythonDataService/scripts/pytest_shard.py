"""Deterministically partition collected pytest cases across CI jobs.

Tests present in ``pr_shard_durations.json`` are dealt to the shards by
longest-processing-time-first balance over their measured durations, so a
slow app-boot suite cannot pile onto one shard by hash luck (#2682: the
hash-only partition put shard 5/12 at 103-109 s of the 120 s budget while
other shards idled). Tests missing from the file — new, renamed, or
sub-5 ms — keep the stable sha256 hash assignment: their times are noise
at shard scale, and the fallback keeps a stale durations file harmless
rather than load-bearing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

DURATIONS_PATH = Path(__file__).with_name("pr_shard_durations.json")


def hash_shard(nodeid: str, *, shard_count: int) -> int:
    """Return the one-based shard a pytest node hashes to."""
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") % shard_count + 1


def belongs_to_shard(nodeid: str, *, shard_index: int, shard_count: int) -> bool:
    """Return whether a pytest node belongs to a one-based stable shard."""
    if not 1 <= shard_index <= shard_count:
        raise ValueError("shard_index must be between 1 and shard_count")
    return hash_shard(nodeid, shard_count=shard_count) == shard_index


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


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    shard_index = config.getoption("pr_shard_index")
    shard_count = config.getoption("pr_shard_count")
    if shard_index is None and shard_count is None:
        return
    if shard_index is None or shard_count is None:
        raise pytest.UsageError("both PR shard options are required")

    assignments = assign_shards(
        (item.nodeid for item in items),
        shard_count=shard_count,
        durations=load_pr_shard_durations(),
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
