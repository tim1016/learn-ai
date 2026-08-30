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
``atomic_write_and_promote`` below directly. Use :func:`publish_artifact`
instead -- it stages the bytes, then hands the rename to
``catalog_client.publish_under_lease``, which performs the authorization
check, the rename, and the completion receipt inside a single transaction
holding a ``SELECT ... FOR UPDATE`` row lock. That is what closes the
zombie-writer race: a concurrent steal must block on the lock for the whole
publication and re-evaluate afterwards, so it cannot interleave between
"you still hold the lease" and ``os.replace``. A writer whose lease was
stolen never reaches the rename at all.

This module deliberately imports ``catalog_client`` (an asyncpg module) for
that one function even though the rest of it is pure-filesystem and
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

import contextlib
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.data_lake import catalog_client

logger = logging.getLogger(__name__)


class AtomicRenameUnsafeError(RuntimeError):
    """Raised when staging and lake live on different filesystems."""


# Re-exported so callers keep importing the publication failure from the
# module whose interface raises it; it is defined in catalog_client because
# that is where the transaction deciding it lives (and atomic imports
# catalog_client, not the other way round).
ArtifactLeaseLostError = catalog_client.ArtifactLeaseLostError


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
    path. It carries no authorization of its own: a lease-holding writer
    must reach it only through :func:`publish_artifact`, which calls it
    under the catalog's publication row lock.
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
    lease-gated caller must use :func:`publish_artifact` instead.

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


async def publish_artifact(
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
    row_count: int,
    first_bar_start_ms: int,
    last_bar_start_ms: int,
    data_contract_hash: str | None = None,
) -> str:
    """Publish one artifact under its catalog lease (issue #1888).

    The single interface a lease-holding writer uses to get bytes onto the
    lake. It hides the whole operation -- there is deliberately no
    caller-visible stage/authorize/promote/complete choreography to get
    wrong, and no way to promote without also recording the receipt:

    1. Stage and fsync the bytes outside any transaction (no lake access,
       nothing to authorize yet).
    2. Hand the rename to ``catalog_client.publish_under_lease``, which locks
       the artifact row, validates the lease, performs the rename under that
       lock, writes the completion receipt in the same transaction, and
       commits.

    Returns the SHA-256 hex digest of the published bytes. Raises
    :class:`ArtifactLeaseLostError` when the catalog refuses to authorize
    the publication -- the canonical file is untouched, the staged bytes are
    removed, and the caller must not retry this attempt or call
    ``fail_artifact`` (the row is no longer theirs to transition).

    The staged file is removed on every failure path, not just the refusal:
    staging is request/worker/attempt-scoped with no sweeper behind it, so a
    contended path that leaked its staged copy would accumulate full
    artifacts on the lake filesystem indefinitely.
    """
    staged, sha = stage_content(content, lake_root, staging_root, rel_lake_path, request_id, worker_id, attempt)
    promoted = False

    def _promote() -> None:
        nonlocal promoted
        promote_staged(staged, lake_root, rel_lake_path)
        promoted = True

    try:
        await catalog_client.publish_under_lease(
            artifact_id=artifact_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            promote=_promote,
            row_count=row_count,
            first_bar_start_ms=first_bar_start_ms,
            last_bar_start_ms=last_bar_start_ms,
            file_size_bytes=len(content),
            file_sha256=sha,
            data_contract_hash=data_contract_hash,
        )
    finally:
        # promote_staged renames the staged path away on success, so there is
        # nothing left to unlink then. Every other exit -- refusal, a failed
        # rename, a rolled-back completion -- leaves it behind.
        if not promoted:
            with contextlib.suppress(OSError):
                staged.unlink()
    return sha
