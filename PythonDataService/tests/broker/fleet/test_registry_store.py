"""Registry store lifecycle, reopen, and the migration machinery (PRD FR-030/031)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.broker.fleet import schema
from app.broker.fleet.errors import FleetRegistryUnavailable
from app.broker.fleet.records import StoredLifecycleState
from app.broker.fleet.store import FleetRegistryStore, registry_database_path
from tests.broker.fleet.conftest import FrozenClock, provision_lane


def test_open_creates_schema_meta_and_reopens_idempotently(control_dir: Path) -> None:
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION
        assert store.registry_id.startswith("fltr_")
        registry_id = store.registry_id
        tables = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "fleet_meta",
            "clerks",
            "clerk_sessions",
            "clerk_session_history",
            "account_assignments",
            "routing_receipts",
        } <= tables
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        store.close()

    reopened = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert reopened.schema_version == schema.SCHEMA_VERSION
        # Same registry, not a second one: identity is stable across opens.
        assert reopened.registry_id == registry_id
    finally:
        reopened.close()


def test_unreadable_database_refuses_with_the_storage_family(tmp_path: Path) -> None:
    db_path = registry_database_path(tmp_path)
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"this is not a database")
    with pytest.raises(FleetRegistryUnavailable):
        FleetRegistryStore.open(control_dir=tmp_path)


def test_newer_schema_version_refuses_rather_than_guessing(tmp_path: Path) -> None:
    store = FleetRegistryStore.open(control_dir=tmp_path)
    try:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE fleet_meta SET schema_version = ? WHERE id = 1",
                (schema.SCHEMA_VERSION + 1,),
            )
    finally:
        store.close()
    with pytest.raises(FleetRegistryUnavailable, match="newer than this"):
        FleetRegistryStore.open(control_dir=tmp_path)


def test_clerk_rows_are_immutable_never_deleted_and_lifecycle_moves_forward(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    lane = provision_lane(fleet_service, broker="fake_alpha", label="alpha", tmp_path=control_dir.parent)
    store = fleet_service._store
    with pytest.raises(sqlite3.IntegrityError, match="identity is written once"), store.transaction() as conn:
        conn.execute(
            "UPDATE clerks SET broker = 'fake_beta' WHERE clerk_id = ?", (lane.clerk_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"), store.transaction() as conn:
        conn.execute("DELETE FROM clerks WHERE clerk_id = ?", (lane.clerk_id,))
    with pytest.raises(sqlite3.IntegrityError, match="forward only"), store.transaction() as conn:
        fleet_service._store.update_clerk_lifecycle(
            conn, clerk_id=lane.clerk_id, lifecycle_state=StoredLifecycleState.RETIRED,
            retired_at_ms=clock(),
        )
        fleet_service._store.update_clerk_lifecycle(
            conn, clerk_id=lane.clerk_id,
            lifecycle_state=StoredLifecycleState.PROVISIONED, retired_at_ms=None,
        )
    # Sanity: the failed transitions left the row untouched.
    clerk = store.read_clerk(lane.clerk_id)
    assert clerk is not None
    assert clerk.lifecycle_state == StoredLifecycleState.PROVISIONED


def test_foreign_key_pins_assignments_and_sessions_to_real_clerks(
    control_dir: Path, fleet_service
) -> None:
    from app.broker.fleet.records import AccountAssignmentRecord, AssignmentState

    store: FleetRegistryStore = fleet_service._store
    orphan = AccountAssignmentRecord(
        broker="fake_alpha",
        canonical_external_account_id="ORPHAN",
        clerk_id="clrk_0000000000000000000000ff",
        assignment_generation=1,
        state=AssignmentState.RESERVED,
        recorded_at_ms=1,
        updated_at_ms=1,
    )
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
        store.insert_assignment(conn, orphan)
