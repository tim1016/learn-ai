"""Shared crash-atomic file publication and durability primitives."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish bytes through a unique, fsynced sibling and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    candidate = Path(candidate_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
        fsync_parent_dir(path)
    except BaseException:
        with contextlib.suppress(OSError):
            candidate.unlink()
        raise


def fsync_parent_dir(child_path: Path) -> None:
    """Make the directory entry containing ``child_path`` durable."""

    dir_fd = os.open(str(child_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def fsync_file(path: Path) -> None:
    """Flush an already-written file's contents to durable storage."""

    with path.open("rb") as handle:
        os.fsync(handle.fileno())
