"""Repository initialization and fail-closed startup verification."""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.folds import FoldRegistry
from app.broker.alpaca.clerk.sqlite.hashchain import verify_chain
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile
from app.broker.alpaca.clerk.sqlite.registry import (
    EstablishedAccountsRegistry,
    EstablishedGeneration,
)
from app.broker.alpaca.paths import fsync_directory_chain
from app.utils.timestamps import Clock

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


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
    """Create authority generation one for a never-established account."""
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
    if registry.is_established(account_id):
        raise DatabaseMissingAfterEstablishment(
            f"account {account_id!r} was previously established but {db_path} is "
            "missing — this requires the explicit recovery/reset workflow, not a "
            "fresh initialize()"
        )

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
            authority_generation=1,
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
            "VALUES (1, ?, 'alpaca', ?, ?, 1, 0, ?, ?, NULL, ?, ?)",
            (
                schema.SCHEMA_VERSION,
                account_id,
                db_identity_token,
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

    return repository_type(
        account_id=account_id,
        account_dir=account_dir,
        db_path=db_path,
        mirror_path=mirror_path,
        conn=conn,
        mirror=MirrorFile(mirror_path),
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
    if control_row["schema_version"] != schema.SCHEMA_VERSION:
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
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    db_path = writes.confined_account_file(artifacts_root, account_id, db_filename)
    mirror_path = writes.confined_account_file(artifacts_root, account_id, mirror_filename)
    registry = EstablishedAccountsRegistry(accounts_root)
    _require_existing_database(account_id=account_id, db_path=db_path, registry=registry)
    conn, control_row = _read_control_row(db_path)
    _validate_control_identity(
        conn=conn,
        control_row=control_row,
        registry=registry,
        account_id=account_id,
        db_path=db_path,
    )
    chain_payloads = _verified_chain_payloads(conn, db_path)

    mirror = MirrorFile(mirror_path)
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
