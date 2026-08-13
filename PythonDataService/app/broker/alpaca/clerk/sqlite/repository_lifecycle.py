"""Repository initialization and fail-closed startup verification."""

from __future__ import annotations

import fcntl
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateResetRegistry,
)
from app.broker.alpaca.clerk.sqlite.folds import FoldRegistry
from app.broker.alpaca.clerk.sqlite.hashchain import verify_chain
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, MirrorIdentity
from app.broker.alpaca.clerk.sqlite.registry import (
    EstablishedAccountsRegistry,
    EstablishedGeneration,
)
from app.broker.alpaca.paths import fsync_directory_chain
from app.utils.timestamps import Clock

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


_UNSUPPORTED_WAL_FILESYSTEMS = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "fuse",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb3",
        "virtiofs",
    }
)
_RECOVERY_LOCK_DIRECTORY = "_recovery_locks"
_RECOVERY_FENCE_DEPTH: ContextVar[int] = ContextVar(
    "alpaca_clerk_recovery_fence_depth",
    default=0,
)


def _recovery_lock_path(artifacts_root: Path, account_id: str) -> Path:
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    lock_root = accounts_root / _RECOVERY_LOCK_DIRECTORY
    if lock_root.is_symlink():
        from app.broker.alpaca.clerk.sqlite.repository import RecoveryInProgress

        raise RecoveryInProgress("recovery lock directory must not be a symbolic link")
    lock_root.mkdir(parents=True, exist_ok=True)
    fsync_directory_chain(lock_root, artifacts_root)
    return lock_root / f"{account_dir.name}.lock"


@contextmanager
def _recovery_flock(
    *,
    artifacts_root: Path,
    account_id: str,
    operation: int,
) -> Iterator[None]:
    from app.broker.alpaca.clerk.sqlite.repository import RecoveryInProgress

    lock_path = _recovery_lock_path(artifacts_root, account_id)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RecoveryInProgress(f"cannot open recovery fence {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryInProgress(
                f"account {account_id!r} startup/recovery fence is held by another process"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def startup_recovery_fence(
    *,
    artifacts_root: Path,
    account_id: str,
) -> Iterator[None]:
    """Keep authority startup from crossing an offline recovery file swap."""
    if _RECOVERY_FENCE_DEPTH.get() > 0:
        yield
        return
    with _recovery_flock(
        artifacts_root=artifacts_root,
        account_id=account_id,
        operation=fcntl.LOCK_SH,
    ):
        yield


@contextmanager
def exclusive_recovery_fence(
    *,
    artifacts_root: Path,
    account_id: str,
) -> Iterator[None]:
    """Exclude startup until one destructive recovery publication completes."""
    with _recovery_flock(
        artifacts_root=artifacts_root,
        account_id=account_id,
        operation=fcntl.LOCK_EX,
    ):
        token = _RECOVERY_FENCE_DEPTH.set(_RECOVERY_FENCE_DEPTH.get() + 1)
        try:
            yield
        finally:
            _RECOVERY_FENCE_DEPTH.reset(token)


def _decode_mountinfo_path(value: str) -> str:
    """Decode the escapes Linux uses for whitespace in ``mountinfo`` fields."""
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _linux_filesystem_type(path: Path) -> str | None:
    """Return the most-specific Linux mount type containing ``path``.

    Native non-Linux execution has no ``/proc/self/mountinfo`` and returns
    ``None``. The production container is Linux, where this catches Podman
    macOS bind mounts (``virtiofs``) before SQLite creates a WAL database on a
    filesystem outside the container's shared-memory/locking domain.
    """
    mountinfo = Path("/proc/self/mountinfo")
    try:
        records = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    resolved = path.resolve()
    best_match: tuple[int, str] | None = None
    for record in records:
        before_separator, separator, after_separator = record.partition(" - ")
        before_fields = before_separator.split()
        after_fields = after_separator.split()
        if not separator or len(before_fields) < 5 or not after_fields:
            continue
        mount_point = Path(_decode_mountinfo_path(before_fields[4]))
        if resolved != mount_point and mount_point not in resolved.parents:
            continue
        candidate = (len(mount_point.parts), after_fields[0].lower())
        if best_match is None or candidate[0] > best_match[0]:
            best_match = candidate
    return best_match[1] if best_match is not None else None


def assert_wal_filesystem_supported(path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.repository import UnsupportedWalFilesystem

    filesystem_type = wal_filesystem_type(path)
    unsupported = filesystem_type in _UNSUPPORTED_WAL_FILESYSTEMS or (
        filesystem_type is not None and filesystem_type.startswith("fuse")
    )
    if unsupported:
        raise UnsupportedWalFilesystem(
            "SQLite WAL authority requires a VM-local filesystem; "
            f"{path} resolves to {filesystem_type!r}. Mount ALPACA_CLERK_DIR "
            "from a container-local named volume and run recovery tooling "
            "inside that same host boundary."
        )


def wal_filesystem_type(path: Path) -> str | None:
    """Return the mounted filesystem type used by the WAL storage guard."""

    return _linux_filesystem_type(path)


def initialize_repository(
    *,
    repository_type: type[ClerkSqliteRepository],
    account_id: str,
    artifacts_root: Path,
    clock: Clock,
    lease_owner: str | None,
    lease_ttl_ms: int,
    fold_registry: FoldRegistry,
    db_filename: str,
    mirror_filename: str,
) -> ClerkSqliteRepository:
    """Create an initial authority or one authorized developer-reset successor."""
    with startup_recovery_fence(
        artifacts_root=artifacts_root,
        account_id=account_id,
    ):
        return _initialize_repository_unfenced(
            repository_type=repository_type,
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            lease_owner=lease_owner,
            lease_ttl_ms=lease_ttl_ms,
            fold_registry=fold_registry,
            db_filename=db_filename,
            mirror_filename=mirror_filename,
        )


def _initialize_repository_unfenced(
    *,
    repository_type: type[ClerkSqliteRepository],
    account_id: str,
    artifacts_root: Path,
    clock: Clock,
    lease_owner: str | None,
    lease_ttl_ms: int,
    fold_registry: FoldRegistry,
    db_filename: str,
    mirror_filename: str,
) -> ClerkSqliteRepository:
    from app.broker.alpaca.clerk.sqlite.repository import (
        AlreadyInitialized,
        DatabaseMissingAfterEstablishment,
    )

    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    registry = EstablishedAccountsRegistry(accounts_root)
    db_path = writes.confined_account_file(artifacts_root, account_id, db_filename)
    mirror_path = writes.confined_account_file(artifacts_root, account_id, mirror_filename)
    if db_path.is_file():
        raise AlreadyInitialized(f"{db_path} already exists; use open()")
    established = registry.latest(account_id)
    reset_registry = DeveloperCleanSlateResetRegistry(accounts_root)
    if established is not None and not reset_registry.authorizes_reinitialize(
        account_id=account_id,
        prior_authority_generation=established.authority_generation,
        artifacts_root=artifacts_root,
    ):
        raise DatabaseMissingAfterEstablishment(
            f"account {account_id!r} was previously established but {db_path} is "
            "missing — this requires the explicit recovery/reset workflow, not a "
            "fresh initialize()"
        )
    authority_generation = 1 if established is None else established.authority_generation + 1

    assert_wal_filesystem_supported(account_dir)
    account_dir.mkdir(parents=True, exist_ok=True)
    fsync_directory_chain(account_dir, artifacts_root)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    schema.configure_connection(conn)
    schema.apply_schema(conn)

    now = clock()
    db_identity_token = secrets.token_hex(16)
    owner = lease_owner or writes.default_lease_owner()
    # Registry-first makes an interrupted initialization fail closed as an
    # established-but-missing database, never as permission to recreate it.
    registry.record(
        EstablishedGeneration(
            account_id=account_id,
            authority_generation=authority_generation,
            db_identity_token=db_identity_token,
            established_at_ms=now,
        )
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO control_meta "
            "(id, schema_version, broker, account_id, db_identity_token, "
            "authority_generation, control_revision, created_at_ms, last_open_at_ms, "
            "reset_provenance_json, execution_lease_owner, execution_lease_expires_at_ms) "
            "VALUES (1, ?, 'alpaca', ?, ?, ?, 0, ?, ?, NULL, ?, ?)",
            (
                schema.SCHEMA_VERSION,
                account_id,
                db_identity_token,
                authority_generation,
                now,
                now,
                owner,
                now + lease_ttl_ms,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    mirror = MirrorFile(
        mirror_path,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=authority_generation,
            db_identity_token=db_identity_token,
        ),
    )
    try:
        mirror.initialize_identity()
    except Exception:
        conn.close()
        raise
    return repository_type(
        account_id=account_id,
        account_dir=account_dir,
        db_path=db_path,
        mirror_path=mirror_path,
        conn=conn,
        mirror=mirror,
        clock=clock,
        fold_registry=fold_registry,
        lease_owner=owner,
        lease_ttl_ms=lease_ttl_ms,
    )


def _require_existing_database(
    *, account_id: str, db_path: Path, registry: EstablishedAccountsRegistry
) -> None:
    from app.broker.alpaca.clerk.sqlite.repository import (
        DatabaseMissingAfterEstablishment,
    )

    if db_path.is_file():
        return
    if registry.is_established(account_id):
        raise DatabaseMissingAfterEstablishment(
            f"account {account_id!r} has an established authority but "
            f"{db_path} is missing; run the recovery/reset workflow"
        )
    raise FileNotFoundError(f"{db_path} does not exist; use initialize()")


def _read_control_row(db_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row]:
    from app.broker.alpaca.clerk.sqlite.repository import (
        ClerkSqliteError,
        IntegrityCheckFailed,
    )

    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        schema.configure_connection(conn)
        control_row = conn.execute(
            "SELECT schema_version, account_id, db_identity_token, authority_generation "
            "FROM control_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise IntegrityCheckFailed(f"{db_path} is corrupt: {exc}") from exc
    if control_row is None:
        conn.close()
        raise ClerkSqliteError(f"{db_path} has no control_meta row — not a valid clerk.db")
    return conn, control_row


def _validate_control_identity(
    *,
    conn: sqlite3.Connection,
    control_row: sqlite3.Row,
    registry: EstablishedAccountsRegistry,
    account_id: str,
    db_path: Path,
) -> None:
    from app.broker.alpaca.clerk.sqlite.repository import (
        DatabaseIdentityMismatch,
        SchemaVersionMismatch,
    )

    latest = registry.latest(account_id)
    if latest is not None and (
        latest.db_identity_token != control_row["db_identity_token"]
        or latest.authority_generation != control_row["authority_generation"]
    ):
        conn.close()
        raise DatabaseIdentityMismatch(
            f"{db_path} identity/generation disagrees with the established-accounts "
            f"registry for {account_id!r} — possible file substitution or generation rollback"
        )
    if control_row["account_id"] != account_id:
        conn.close()
        raise DatabaseIdentityMismatch(
            f"{db_path} embeds account_id {control_row['account_id']!r}, "
            f"requested {account_id!r}"
        )
    if control_row["schema_version"] < schema.SCHEMA_VERSION:
        if control_row["schema_version"] == 8 and schema.SCHEMA_VERSION == 9:
            conn.close()
            raise SchemaVersionMismatch(
                f"{db_path} schema_version=8 requires the offline v8-to-v9 Clerk upgrade; "
                "stop the data plane, create a verified backup, and run the v9 upgrade ceremony"
            )
        try:
            schema.migrate_schema(conn, from_version=control_row["schema_version"])
        except ValueError as exc:
            conn.close()
            raise SchemaVersionMismatch(
                f"{db_path} schema_version={control_row['schema_version']}, "
                f"expected {schema.SCHEMA_VERSION}: {exc}"
            ) from exc
    elif control_row["schema_version"] > schema.SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionMismatch(
            f"{db_path} schema_version={control_row['schema_version']}, "
            f"expected {schema.SCHEMA_VERSION}"
        )


def _verified_chain_payloads(
    conn: sqlite3.Connection, db_path: Path
) -> list[dict[str, object]]:
    from app.broker.alpaca.clerk.sqlite.repository import (
        HashChainBroken,
        IntegrityCheckFailed,
    )

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise IntegrityCheckFailed(f"{db_path} integrity_check returned {integrity!r}")
        transition_rows = conn.execute(
            f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} "
            "FROM custody_transitions ORDER BY sequence ASC"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise IntegrityCheckFailed(f"{db_path} is corrupt: {exc}") from exc
    except IntegrityCheckFailed:
        conn.close()
        raise
    chain_payloads = [writes.row_to_payload(row) for row in transition_rows]
    bad_sequence = verify_chain(chain_payloads)
    if bad_sequence is not None:
        conn.close()
        raise HashChainBroken(f"{db_path} hash-chain mismatch at sequence {bad_sequence}")
    return chain_payloads


def _acquire_execution_lease(
    *,
    conn: sqlite3.Connection,
    account_id: str,
    owner: str,
    now: int,
    lease_ttl_ms: int,
) -> None:
    from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseHeld

    cursor = conn.execute(
        "UPDATE control_meta SET last_open_at_ms = ?, execution_lease_owner = ?, "
        "execution_lease_expires_at_ms = ? WHERE id = 1 AND "
        "(execution_lease_owner IS NULL OR execution_lease_owner = ? "
        "OR execution_lease_expires_at_ms < ?)",
        (now, owner, now + lease_ttl_ms, owner, now),
    )
    if cursor.rowcount == 0:
        conn.close()
        raise ExecutionLeaseHeld(
            f"account {account_id!r} execution lease is held by another live process"
        )
    conn.commit()


def open_repository(
    *,
    repository_type: type[ClerkSqliteRepository],
    account_id: str,
    artifacts_root: Path,
    clock: Clock,
    lease_owner: str | None,
    lease_ttl_ms: int,
    fold_registry: FoldRegistry,
    db_filename: str,
    mirror_filename: str,
) -> ClerkSqliteRepository:
    """Open authority only after all pinned startup checks succeed."""
    with startup_recovery_fence(
        artifacts_root=artifacts_root,
        account_id=account_id,
    ):
        return _open_repository_unfenced(
            repository_type=repository_type,
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            lease_owner=lease_owner,
            lease_ttl_ms=lease_ttl_ms,
            fold_registry=fold_registry,
            db_filename=db_filename,
            mirror_filename=mirror_filename,
        )


def _open_repository_unfenced(
    *,
    repository_type: type[ClerkSqliteRepository],
    account_id: str,
    artifacts_root: Path,
    clock: Clock,
    lease_owner: str | None,
    lease_ttl_ms: int,
    fold_registry: FoldRegistry,
    db_filename: str,
    mirror_filename: str,
) -> ClerkSqliteRepository:
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    db_path = writes.confined_account_file(artifacts_root, account_id, db_filename)
    mirror_path = writes.confined_account_file(artifacts_root, account_id, mirror_filename)
    registry = EstablishedAccountsRegistry(accounts_root)
    _require_existing_database(account_id=account_id, db_path=db_path, registry=registry)
    assert_wal_filesystem_supported(account_dir)
    conn, control_row = _read_control_row(db_path)
    _validate_control_identity(
        conn=conn,
        control_row=control_row,
        registry=registry,
        account_id=account_id,
        db_path=db_path,
    )
    chain_payloads = _verified_chain_payloads(conn, db_path)

    mirror = MirrorFile(
        mirror_path,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=control_row["authority_generation"],
            db_identity_token=control_row["db_identity_token"],
        ),
    )
    try:
        mirror.reconcile(chain_payloads, clock=clock)
    except Exception:
        conn.close()
        raise

    owner = lease_owner or writes.default_lease_owner()
    now = clock()
    _acquire_execution_lease(
        conn=conn,
        account_id=account_id,
        owner=owner,
        now=now,
        lease_ttl_ms=lease_ttl_ms,
    )

    return repository_type(
        account_id=account_id,
        account_dir=account_dir,
        db_path=db_path,
        mirror_path=mirror_path,
        conn=conn,
        mirror=mirror,
        clock=clock,
        fold_registry=fold_registry,
        lease_owner=owner,
        lease_ttl_ms=lease_ttl_ms,
    )
