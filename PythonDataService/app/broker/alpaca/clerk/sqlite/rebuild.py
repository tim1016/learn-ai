"""Disaster-recovery rebuild from the finalized custody mirror."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.folds import FoldRegistry
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, MirrorIdentity
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository_lifecycle import (
    assert_wal_filesystem_supported,
)
from app.broker.alpaca.paths import fsync_directory_chain
from app.utils.timestamps import Clock

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


def rebuild_repository_from_mirror(
    *,
    repository_type: type[ClerkSqliteRepository],
    account_id: str,
    artifacts_root: Path,
    clock: Clock,
    lease_owner: str | None,
    fold_registry: FoldRegistry,
    db_filename: str,
    mirror_filename: str,
) -> ClerkSqliteRepository:
    """Replay a verified mirror into a fresh database, preserving the old DB."""
    # Imported here to avoid a module cycle: repository exposes the public
    # exception types while delegating this cohesive recovery workflow.
    from app.broker.alpaca.clerk.sqlite.repository import (
        AlreadyInitialized,
        DatabaseMissingAfterEstablishment,
    )

    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    db_path = writes.confined_account_file(artifacts_root, account_id, db_filename)
    mirror_path = writes.confined_account_file(artifacts_root, account_id, mirror_filename)
    if db_path.is_file():
        raise AlreadyInitialized(
            f"{db_path} exists — move it aside before rebuild_from_mirror()"
        )

    established = EstablishedAccountsRegistry(accounts_root).latest(account_id)
    if established is None:
        raise DatabaseMissingAfterEstablishment(
            f"no established-accounts registry entry for {account_id!r}; "
            "cannot rebuild an authority that was never established"
        )
    rebuilt_rows = MirrorFile(
        mirror_path,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=established.authority_generation,
            db_identity_token=established.db_identity_token,
        ),
    ).rebuild()
    if any(row.authority_generation != established.authority_generation for row in rebuilt_rows):
        raise DatabaseMissingAfterEstablishment(
            "mirror contains a transition from a different authority generation"
        )

    assert_wal_filesystem_supported(account_dir)
    account_dir.mkdir(parents=True, exist_ok=True)
    fsync_directory_chain(account_dir, artifacts_root)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    conn.row_factory = sqlite3.Row

    now = clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO control_meta "
            "(id, schema_version, broker, account_id, db_identity_token, "
            "authority_generation, control_revision, created_at_ms, last_open_at_ms, "
            "reset_provenance_json, execution_lease_owner, execution_lease_expires_at_ms) "
            "VALUES (1, ?, 'alpaca', ?, ?, ?, 0, ?, ?, "
            "'{\"rebuilt_from_mirror\": true}', NULL, NULL)",
            (
                schema.SCHEMA_VERSION,
                account_id,
                established.db_identity_token,
                established.authority_generation,
                now,
                now,
            ),
        )
        for row in rebuilt_rows:
            payload = dict(row.payload)
            writes.insert_custody_transition_row(
                conn,
                sequence=row.sequence,
                prev_hash=row.prev_hash,
                row_hash=row.row_hash,
                payload=payload,
            )
            fold_registry.apply(conn, payload)
            writes.advance_control_revision(conn)
            writes.insert_mirror_fence_prepare_row(
                conn,
                sequence=row.sequence,
                row_hash=row.row_hash,
                authority_generation=row.authority_generation,
                recorded_at_ms=row.recorded_at_ms,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        db_path.unlink(missing_ok=True)
        raise

    return repository_type.open(
        account_id=account_id,
        artifacts_root=artifacts_root,
        clock=clock,
        lease_owner=lease_owner,
        fold_registry=fold_registry,
    )
