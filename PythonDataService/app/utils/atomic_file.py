"""Shared crash-atomic file publication and durability primitives."""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

_NEW_FILE_MODE = 0o666
_PRIVATE_CANDIDATE_MODE = 0o600
_MAX_CANDIDATE_ATTEMPTS = 100


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Publish bytes through a unique, fsynced sibling and atomic replace.

    ``mode`` overrides both the destination's existing permissions and the
    process umask. Use it for artifacts that contain operator secrets.
    """

    if mode is not None and not 0 <= mode <= 0o777:
        raise ValueError("atomic publication mode must contain permission bits only")

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, candidate, new_file_mode = _create_candidate(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), _PRIVATE_CANDIDATE_MODE)
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), _publication_mode(path, new_file_mode, mode))
            os.fsync(handle.fileno())
        os.replace(candidate, path)
        fsync_parent_dir(path)
    except BaseException:
        with contextlib.suppress(OSError):
            candidate.unlink()
        raise


def _create_candidate(path: Path) -> tuple[int, Path, int]:
    """Create a unique sibling and capture the kernel's umask-derived mode."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    for _ in range(_MAX_CANDIDATE_ATTEMPTS):
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(candidate, flags, _NEW_FILE_MODE)
        except FileExistsError:
            continue
        return descriptor, candidate, stat.S_IMODE(os.fstat(descriptor).st_mode)
    raise FileExistsError(f"Could not allocate a unique atomic-write candidate for {path}")


def _publication_mode(path: Path, new_file_mode: int, requested_mode: int | None) -> int:
    """Preserve an existing destination mode or use ordinary creation mode."""

    if requested_mode is not None:
        return requested_mode
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return new_file_mode


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
