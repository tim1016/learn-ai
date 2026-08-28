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

# Sibling marker recording which price_adjustment_mode a lake tree is
# committed to. Written by app.data_lake.cache_import (the only current
# producer of a non-'raw' mode); read here so every lake writer -- the live
# ensure_data pipeline included -- shares the one check, not just the
# importer that happens to write the marker.
LAKE_ROOT_MODE_MARKER = ".cache_import_adjustment_mode"


class AtomicRenameUnsafeError(RuntimeError):
    """Raised when staging and lake live on different filesystems."""


class LakeRootModeConflictError(RuntimeError):
    """A write's adjustment mode disagrees with the lake root's committed marker.

    ``LeanMinuteBarPath`` (and the other path_policy path types) carry no
    adjustment-mode component, so a 'raw' write and a 'polygon_split_adjusted'
    write for the same (market, symbol, date, type) resolve to the identical
    on-disk path. Without this check, either writer could silently overwrite
    the other's bytes via ``os.replace`` while the earlier row's catalog hash
    still describes what used to be there. See
    ``app.data_lake.cache_import``'s module docstring, "One lake root per
    adjustment mode", for the full story and the (deliberately deferred)
    real fix -- an adjustment-mode-aware path, or the ``data_root_id``
    design ledgered for the data-lake integration slice (T10).
    """


def lake_root_mode_marker_path(lake_root: Path) -> Path:
    return lake_root / LAKE_ROOT_MODE_MARKER


def read_lake_root_mode(lake_root: Path) -> str | None:
    marker = lake_root_mode_marker_path(lake_root)
    if not marker.is_file():
        return None
    return marker.read_text().strip() or None


def commit_lake_root_mode(lake_root: Path, mode: str) -> None:
    lake_root_mode_marker_path(lake_root).write_text(mode)


def check_write_mode_compatible(lake_root: Path, price_adjustment_mode: str | None) -> None:
    """Refuse a write whose adjustment mode disagrees with ``lake_root``'s marker.

    ``price_adjustment_mode=None`` (metadata, factor-file, and map-file
    writes -- artifact kinds with no adjustment mode of their own) is always
    permitted regardless of the marker.

    An **unmarked** root stays permissive: the marker only exists once a
    ``cache_import`` run has stamped it, so requiring one here would break
    every ``ensure_data`` deployment that has never run an import -- this is
    the backward-compatible default, not a loosened check.
    """
    if price_adjustment_mode is None:
        return
    marker_mode = read_lake_root_mode(lake_root)
    if marker_mode is not None and marker_mode != price_adjustment_mode:
        raise LakeRootModeConflictError(
            f"{lake_root} is committed to adjustment mode {marker_mode!r} (see "
            f"{lake_root_mode_marker_path(lake_root)}), but this write is "
            f"{price_adjustment_mode!r} -- they would collide at the same on-disk path. "
            f"The full root-identity (data_root_id) design is ledgered for the flag-flip "
            f"integration slice (T10); until then, one lake root serves one adjustment mode."
        )


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
    *,
    price_adjustment_mode: str | None = None,
) -> str:
    """Stage `content` then atomically promote into `lake_root / rel_lake_path`.

    Returns the SHA-256 hex digest of the written bytes.

    Raises AtomicRenameUnsafeError if the same-filesystem invariant fails.
    Raises ValueError if rel_lake_path is absolute, contains '..', or
    contains '.' or empty segments.
    Raises LakeRootModeConflictError if ``price_adjustment_mode`` disagrees
    with ``lake_root``'s adjustment-mode marker (see
    ``check_write_mode_compatible``) -- checked here, the one seam every
    lake writer (the live ensure_data pipeline and app.data_lake.cache_import
    alike) shares, so no caller can promote mode-mismatched bytes over an
    existing lake tree by skipping a caller-side check.
    """
    if rel_lake_path.is_absolute():
        raise ValueError(f"rel_lake_path must be a relative path, got absolute: {rel_lake_path!r}")
    if not rel_lake_path.parts:
        raise ValueError(f"rel_lake_path must not be empty, got {rel_lake_path!r}")
    for part in rel_lake_path.parts:
        if part in ("..", ".", ""):
            raise ValueError(f"rel_lake_path must not contain '..', '.', or empty segments, got {rel_lake_path!r}")

    assert_same_filesystem(lake_root, staging_root)
    check_write_mode_compatible(lake_root, price_adjustment_mode)

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
