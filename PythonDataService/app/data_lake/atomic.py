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

Lease-gated callers (issue #1888): a writer that is executing under a
catalog artifact lease (app.data_lake.catalog_client's claim_*/steal_or_
retry_minute_bar/refresh_complete_artifact) must not call the unconditional
``atomic_write_and_promote`` below directly. Use
:func:`write_lease_gated_artifact` instead -- it stages first, then asks the
catalog whether the lease is still held at the recorded generation, and only
promotes on a "yes". This is what closes the zombie-writer race: a writer
whose lease was stolen and re-completed by another worker while it was still
fetching would otherwise reach ``os.replace`` with no check at all and
silently overwrite the winner's file with its own stale bytes. This module
deliberately imports ``catalog_client`` (an asyncpg module) for that one
function even though the rest of this module is pure-filesystem and
synchronous -- the whole point of the fix is joining catalog authority to
the promote step, so the coupling is direct rather than routed through an
extra indirection layer. ``atomic_write_and_promote`` itself is kept,
unconditional, for callers that do not hold a catalog lease at promote time
(app.data_lake.metadata_bundle's bundle publish, and cache_import's
already-verified-hash restore path) -- their content is either
content-addressed and idempotent, or already checked against the exact hash
the catalog currently records, so there is no writer to fence against.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.data_lake import catalog_client

logger = logging.getLogger(__name__)


class AtomicRenameUnsafeError(RuntimeError):
    """Raised when staging and lake live on different filesystems."""


class ArtifactLeaseLostError(RuntimeError):
    """Raised by write_lease_gated_artifact when the catalog no longer
    authorizes this writer's promotion -- the lease was stolen (or the
    artifact was already completed by another writer) before this writer
    reached the rename. Callers must treat this as "lost the race", the
    same as any other contention outcome, and must not retry the promote
    for this attempt: the staged bytes are stale by definition once this is
    raised."""


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


def _validate_rel_lake_path(rel_lake_path: PurePosixPath) -> None:
    if rel_lake_path.is_absolute():
        raise ValueError(f"rel_lake_path must be a relative path, got absolute: {rel_lake_path!r}")
    if not rel_lake_path.parts:
        raise ValueError(f"rel_lake_path must not be empty, got {rel_lake_path!r}")
    for part in rel_lake_path.parts:
        if part in ("..", ".", ""):
            raise ValueError(f"rel_lake_path must not contain '..', '.', or empty segments, got {rel_lake_path!r}")


def stage_content(
    content: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> tuple[Path, str]:
    """Stage `content` under a request/worker/attempt-scoped path and fsync it.

    Returns ``(staged_path, sha256_hex)``. Does not touch ``lake_root`` at
    all -- this is the half of the old ``atomic_write_and_promote`` that
    involves no decision about whether promotion is still authorized, so a
    lease-gated caller can run it before asking the catalog anything.

    Raises AtomicRenameUnsafeError if the same-filesystem invariant fails.
    Raises ValueError if rel_lake_path is absolute, contains '..', or
    contains '.' or empty segments.
    """
    _validate_rel_lake_path(rel_lake_path)
    assert_same_filesystem(lake_root, staging_root)

    staged = stage_path_for(staging_root, rel_lake_path, request_id, worker_id, attempt)
    staged.parent.mkdir(parents=True, exist_ok=True)

    with staged.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    _fsync_path(staged.parent)

    sha = hashlib.sha256(content).hexdigest()
    return staged, sha


def promote_staged(staged: Path, lake_root: Path, rel_lake_path: PurePosixPath) -> None:
    """Atomically rename ``staged`` into ``lake_root / rel_lake_path``.

    POSIX rename(2) is atomic with respect to any reader of the destination
    path, but it is not atomic with respect to the catalog decision that
    authorized calling it — see write_lease_gated_artifact's docstring for
    the residual gap that leaves open, and confirm_lease_generation's for
    what remains guaranteed regardless.
    """
    final = lake_root / Path(*rel_lake_path.parts)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, final)
    _fsync_path(final.parent)


def atomic_write_and_promote(
    content: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
) -> str:
    """Stage `content` then atomically promote into `lake_root / rel_lake_path`,
    unconditionally -- no catalog lease check. See the module docstring's
    "Lease-gated callers" section: only call this directly when the caller
    does not hold (or need) a catalog artifact lease at promote time. A
    lease-gated caller must use :func:`write_lease_gated_artifact` instead.

    Returns the SHA-256 hex digest of the written bytes.

    Raises AtomicRenameUnsafeError if the same-filesystem invariant fails.
    Raises ValueError if rel_lake_path is absolute, contains '..', or
    contains '.' or empty segments.

    Takes no adjustment mode. It used to, to refuse a write whose mode
    disagreed with a marker committing the whole tree to one mode; since
    #1839 the mode is a segment of ``lake_root`` itself
    (``path_policy.resolve_lake_root``), so two modes cannot name the same
    final path and there is nothing left here to check.
    """
    staged, sha = stage_content(content, lake_root, staging_root, rel_lake_path, request_id, worker_id, attempt)
    promote_staged(staged, lake_root, rel_lake_path)
    return sha


async def write_lease_gated_artifact(
    *,
    content: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_lake_path: PurePosixPath,
    request_id: UUID,
    worker_id: str,
    attempt: int,
    artifact_id: int,
    lease_generation: int,
) -> str:
    """The reordered promotion sequence (issue #1888): stage first, then ask
    the catalog whether ``worker_id`` still holds artifact ``artifact_id``'s
    lease at ``lease_generation``, and only promote on "yes".

    Returns the SHA-256 hex digest of the promoted bytes. Raises
    :class:`ArtifactLeaseLostError` instead of promoting when the catalog
    refuses -- the caller must not call ``complete_artifact`` either in that
    case (the lease is no longer theirs to complete), and must not retry the
    promote for this attempt.

    This is the single production path for every writer that claimed the
    artifact through app.data_lake.catalog_client's claim_*/steal_or_retry_
    minute_bar/refresh_complete_artifact -- see the module docstring for why
    the two call sites that publish before claiming (metadata bundle
    extraction, cache-import's hash-verified restore) are exempt and still
    use the unconditional ``atomic_write_and_promote`` above.
    """
    staged, sha = stage_content(content, lake_root, staging_root, rel_lake_path, request_id, worker_id, attempt)
    if not await catalog_client.confirm_lease_generation(artifact_id, worker_id, lease_generation):
        raise ArtifactLeaseLostError(
            f"artifact {artifact_id}: {worker_id} no longer holds the lease at generation "
            f"{lease_generation} (stolen or already completed by another writer); "
            f"refusing to promote {rel_lake_path} over it"
        )
    promote_staged(staged, lake_root, rel_lake_path)
    return sha
