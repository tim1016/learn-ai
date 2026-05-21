"""Atomic-write helpers for the data lake writer.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.2

Contract:
  1. Stage the content under a request/worker/attempt-scoped path (so retries
     and parallel workers never collide).
  2. fsync the file and its parent directory.
  3. POSIX atomic rename(2) into the canonical lake path. Lake parent dirs are
     created on the way.
  4. fsync the lake parent directory so the rename hits disk.

Pre-condition: lake_root and staging_root MUST share the same filesystem
(same stat.st_dev). atomic_write_and_promote asserts this on every call.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

logger = logging.getLogger(__name__)


class AtomicRenameUnsafeError(RuntimeError):
    """Raised when staging and lake live on different filesystems."""


def assert_same_filesystem(lake_root: Path, staging_root: Path) -> None:
    """Both paths must exist AND share the same stat.st_dev.

    Raises FileNotFoundError if either path does not exist.
    Raises AtomicRenameUnsafeError if they live on different filesystems.
    """
    lake_dev = lake_root.stat().st_dev
    staging_dev = staging_root.stat().st_dev
    if lake_dev != staging_dev:
        raise AtomicRenameUnsafeError(
            f"lake_root and staging_root are on different filesystems "
            f"(st_dev {lake_dev} vs {staging_dev}). "
            f"POSIX rename(2) is not atomic across filesystems; "
            f"the writer refuses to proceed. "
            f"Reconfigure so both paths share a single mount."
        )


def stage_path_for(
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> Path:
    """Build the per-attempt staging path for a relative lake path.

    The .tmp suffix marks the file as in-flight; promotion strips it via
    rename(2). Per-(request_id, worker_id, attempt) scoping makes retry and
    parallel-worker collisions structurally impossible.
    """
    rel = Path(*rel_lake_path.parts)
    return staging_root / str(request_id) / worker_id / f"attempt_{attempt}" / rel.with_suffix(rel.suffix + ".tmp")


def _fsync_path(path: Path) -> None:
    """Open the path and fsync its file descriptor.

    Works for both regular files and directories. On Windows, fsync on a
    directory descriptor is a no-op (Windows has no equivalent system call),
    so we open file descriptors directly via os.open. The caller is responsible
    for ensuring the path exists.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        # Directory fsync is unsupported on some platforms (e.g. Windows).
        # The write itself is still durable; the parent-dir fsync is a
        # best-effort hardening step on POSIX-y systems.
        logger.debug("fsync on %s not supported on this platform", path)
    finally:
        os.close(fd)


def atomic_write_and_promote(
    content: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> str:
    """Stage `content` then atomically promote into `lake_root / rel_lake_path`.

    Returns the SHA-256 hex digest of the written bytes.

    Raises AtomicRenameUnsafeError if the same-filesystem invariant fails.
    """
    assert_same_filesystem(lake_root, staging_root)

    staged = stage_path_for(staging_root, rel_lake_path, request_id, worker_id, attempt)
    staged.parent.mkdir(parents=True, exist_ok=True)

    # Write + fsync the staged file.
    with staged.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    _fsync_path(staged.parent)

    # Compute the byte hash.
    sha = hashlib.sha256(content).hexdigest()

    # Promote: ensure lake parent exists, then rename.
    final = lake_root / Path(*rel_lake_path.parts)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, final)
    _fsync_path(final.parent)

    return sha
