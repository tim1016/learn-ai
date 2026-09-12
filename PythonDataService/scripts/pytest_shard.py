"""Deterministically partition collected pytest cases across CI jobs."""

from __future__ import annotations

import hashlib

import pytest


def belongs_to_shard(nodeid: str, *, shard_index: int, shard_count: int) -> bool:
    """Return whether a pytest node belongs to a one-based stable shard."""
    if shard_count < 1 or not 1 <= shard_index <= shard_count:
        raise ValueError("shard_index must be between 1 and shard_count")
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    assigned_index = int.from_bytes(digest[:8], byteorder="big") % shard_count + 1
    return assigned_index == shard_index


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

    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        destination = (
            selected
            if belongs_to_shard(
                item.nodeid,
                shard_index=shard_index,
                shard_count=shard_count,
            )
            else deselected
        )
        destination.append(item)

    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
