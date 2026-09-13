"""The fleet registry: open it once, read and write it transactionally.

Storage only; the rules about what a write *means* live in ``service.py``.
The conventions are the repository's established SQLite discipline (PRD
FR-030): WAL, ``synchronous = FULL``, a cross-process advisory lock
serializing initialization and migration, additive-only registered upgrades,
and compare-and-swap guards in ``WHERE`` clauses rather than in caller
reads.

One deliberate divergence from the profiles store: this package performs the
WAL-filesystem check *functionally* rather than by importing the Clerk's
pre-flight helper — ``PRAGMA journal_mode = WAL`` either holds on the control
volume or raises here, and no Alpaca module is imported on any fleet path
(PRD FR-005, asserted by ``tests/broker/fleet/test_import_isolation.py``).
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from app.broker.fleet import schema
from app.broker.fleet.errors import FleetRegistryUnavailable
from app.broker.fleet.records import (
    AccountAssignmentRecord,
    AssignmentState,
    ClerkRecord,
    ClerkSessionRecord,
    RoutingReceiptRecord,
    RoutingReceiptState,
    StoredLifecycleState,
)
from app.utils.timestamps import now_ms_utc

DATABASE_DIRECTORY = "fleet"
DATABASE_FILENAME = "registry.db"


def registry_database_path(control_dir: Path) -> Path:
    """Where the fleet registry lives: the coordinator's own control volume."""
    return control_dir / DATABASE_DIRECTORY / DATABASE_FILENAME


def new_registry_id() -> str:
    return f"fltr_{secrets.token_hex(12)}"


class FleetRegistryStore:
    """One open fleet registry database."""

    def __init__(self, conn: sqlite3.Connection, *, db_path: Path) -> None:
        self._conn = conn
        self._lock = RLock()
        self.db_path = db_path

    # ---- lifecycle ------------------------------------------------------

    @classmethod
    def open(cls, *, control_dir: Path) -> FleetRegistryStore:
        """Open, creating and migrating under a cross-process advisory lock."""
        from app.utils.advisory_lock import advisory_file_lock

        db_path = registry_database_path(control_dir)
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with advisory_file_lock(db_path):
                creating = not db_path.exists()
                conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    schema.configure_connection(conn)
                    cls._establish(conn)
                except Exception:
                    conn.close()
                    if creating:
                        # A file this process created but never finished
                        # establishing must not become tomorrow's "move it
                        # aside" manual intervention.
                        for path in (
                            db_path,
                            db_path.with_name(f"{db_path.name}-wal"),
                            db_path.with_name(f"{db_path.name}-shm"),
                        ):
                            path.unlink(missing_ok=True)
                    raise
        # Named exhaustively: an unreadable registry is a state with a reason,
        # and a bug in this module must stay a bug rather than being reported
        # to an operator as "registry unavailable".
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            raise FleetRegistryUnavailable(
                f"The fleet registry at {db_path} could not be opened: {exc}",
                next_step="Check the coordinator control volume is mounted, writable "
                "and container-local, then retry.",
            ) from exc
        return cls(conn, db_path=db_path)

    @staticmethod
    def _establish(conn: sqlite3.Connection) -> None:
        """Create the schema, or advance an older one to ``SCHEMA_VERSION``."""
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'fleet_meta'"
        ).fetchone()
        if row is None:
            schema.apply_schema(conn)
            now = now_ms_utc()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO fleet_meta (id, schema_version, registry_id, created_at_ms, "
                    "updated_at_ms) VALUES (1, ?, ?, ?, ?)",
                    (schema.SCHEMA_VERSION, new_registry_id(), now, now),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            return

        meta = conn.execute(
            "SELECT schema_version FROM fleet_meta WHERE id = 1"
        ).fetchone()
        if meta is None:
            raise FleetRegistryUnavailable(
                "The fleet registry has no metadata row and is not a fleet registry.",
                next_step="Move the file aside and let the coordinator create a fresh registry.",
            )
        stored = int(meta["schema_version"])
        if stored > schema.SCHEMA_VERSION:
            raise FleetRegistryUnavailable(
                f"The fleet registry is schema_version={stored}, newer than this "
                f"build's {schema.SCHEMA_VERSION}.",
                next_step="Run the build that wrote it, or restore the matching version.",
            )
        if stored < schema.SCHEMA_VERSION:
            if not schema.is_upgradable_to_current(stored):
                raise FleetRegistryUnavailable(
                    f"No registered upgrade path reaches schema_version={schema.SCHEMA_VERSION} "
                    f"from {stored}.",
                    next_step="Restore a registry this build can read.",
                )
            schema.migrate_schema(conn, from_version=stored)
            conn.execute(
                "UPDATE fleet_meta SET updated_at_ms = ? WHERE id = 1",
                (now_ms_utc(),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One serialized write; nested operations stay inside the outer commit."""
        with self._lock:
            savepoint = f"fleet_{secrets.token_hex(8)}" if self._conn.in_transaction else None
            self._conn.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN IMMEDIATE")
            try:
                yield self._conn
                if savepoint:
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._conn.commit()
            except BaseException:
                if savepoint:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._conn.rollback()
                raise

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Keep a multi-query read on one WAL snapshot without reserving a write."""
        with self._lock:
            if self._conn.in_transaction:
                yield
                return
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                self._conn.rollback()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, parameters).fetchall()

    def _query_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, parameters).fetchone()

    @property
    def schema_version(self) -> int:
        row = self._query_one("SELECT schema_version FROM fleet_meta WHERE id = 1")
        if row is None:
            raise FleetRegistryUnavailable(
                "The fleet registry lost its metadata row.",
                next_step="Move the file aside and let the coordinator create a fresh registry.",
            )
        return int(row["schema_version"])

    @property
    def registry_id(self) -> str:
        row = self._query_one("SELECT registry_id FROM fleet_meta WHERE id = 1")
        if row is None:
            raise FleetRegistryUnavailable(
                "The fleet registry lost its metadata row.",
                next_step="Move the file aside and let the coordinator create a fresh registry.",
            )
        return str(row["registry_id"])

    # ---- clerks ---------------------------------------------------------

    _CLERK_COLUMNS = (
        "clerk_id, broker, worker_key, display_label, volume_id, volume_root, "
        "volume_attestation_kind, volume_attestation_id, lifecycle_state, "
        "created_at_ms, retired_at_ms"
    )

    def read_clerk(self, clerk_id: str) -> ClerkRecord | None:
        row = self._query_one(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE clerk_id = ?",
            (clerk_id,),
        )
        return None if row is None else _clerk_from_row(row)

    def find_clerk_by_volume(self, volume_id: str) -> ClerkRecord | None:
        """The active clerk owning a volume identity, if any (clone detection)."""
        row = self._query_one(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE volume_id = ? "
            "AND lifecycle_state <> 'retired'",
            (volume_id,),
        )
        return None if row is None else _clerk_from_row(row)

    def find_clerk_by_attestation(
        self, *, attestation_kind: str, attestation_id: str
    ) -> ClerkRecord | None:
        row = self._query_one(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE volume_attestation_kind = ? "
            "AND volume_attestation_id = ? AND lifecycle_state <> 'retired'",
            (attestation_kind, attestation_id),
        )
        return None if row is None else _clerk_from_row(row)

    def list_clerks(self, *, include_retired: bool = False) -> list[ClerkRecord]:
        sql = f"SELECT {self._CLERK_COLUMNS} FROM clerks"
        if not include_retired:
            sql += " WHERE lifecycle_state <> 'retired'"
        sql += " ORDER BY broker ASC, created_at_ms ASC, clerk_id ASC"
        return [_clerk_from_row(row) for row in self._query(sql)]

    def insert_clerk(self, conn: sqlite3.Connection, clerk: ClerkRecord) -> None:
        conn.execute(
            "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, volume_id, "
            "volume_root, volume_attestation_kind, volume_attestation_id, lifecycle_state, "
            "created_at_ms, retired_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clerk.clerk_id,
                clerk.broker,
                clerk.worker_key,
                clerk.display_label,
                clerk.volume_id,
                clerk.volume_root,
                clerk.volume_attestation_kind,
                clerk.volume_attestation_id,
                str(clerk.lifecycle_state),
                clerk.created_at_ms,
                clerk.retired_at_ms,
            ),
        )

    def update_clerk_lifecycle(
        self,
        conn: sqlite3.Connection,
        *,
        clerk_id: str,
        lifecycle_state: StoredLifecycleState,
        retired_at_ms: int | None,
    ) -> bool:
        cursor = conn.execute(
            "UPDATE clerks SET lifecycle_state = ?, retired_at_ms = ? WHERE clerk_id = ?",
            (str(lifecycle_state), retired_at_ms, clerk_id),
        )
        return cursor.rowcount == 1

    # ---- sessions -------------------------------------------------------

    _SESSION_COLUMNS = (
        "broker, clerk_id, agent_instance_id, routing_epoch, started_at_ms, "
        "last_seen_at_ms, reported_binding_generation, reported_account_id, reported_state"
    )

    def read_session(self, clerk_id: str) -> ClerkSessionRecord | None:
        row = self._query_one(
            f"SELECT {self._SESSION_COLUMNS} FROM clerk_sessions WHERE clerk_id = ?",
            (clerk_id,),
        )
        return None if row is None else _session_from_row(row)

    def list_sessions(self) -> list[ClerkSessionRecord]:
        rows = self._query(
            f"SELECT {self._SESSION_COLUMNS} FROM clerk_sessions "
            "ORDER BY broker ASC, clerk_id ASC"
        )
        return [_session_from_row(row) for row in rows]

    def archive_session(
        self, conn: sqlite3.Connection, session: ClerkSessionRecord, *, superseded_at_ms: int
    ) -> None:
        conn.execute(
            "INSERT INTO clerk_session_history (clerk_id, broker, agent_instance_id, "
            "routing_epoch, started_at_ms, superseded_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (
                session.clerk_id,
                session.broker,
                session.agent_instance_id,
                session.routing_epoch,
                session.started_at_ms,
                superseded_at_ms,
            ),
        )

    def upsert_session(self, conn: sqlite3.Connection, session: ClerkSessionRecord) -> None:
        """Install the one current session row for a clerk.

        The monotonic-epoch trigger plus this single row make a duplicated
        *current* session structurally impossible: a second registration
        either refreshes the same instance id or supersedes the row under a
        higher epoch (after ``archive_session`` recorded the old one).
        """
        conn.execute(
            "INSERT INTO clerk_sessions (clerk_id, broker, agent_instance_id, routing_epoch, "
            "started_at_ms, last_seen_at_ms, reported_binding_generation, reported_account_id, "
            "reported_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(clerk_id) DO UPDATE SET broker = excluded.broker, "
            "agent_instance_id = excluded.agent_instance_id, "
            "routing_epoch = excluded.routing_epoch, "
            "started_at_ms = excluded.started_at_ms, "
            "last_seen_at_ms = excluded.last_seen_at_ms, "
            "reported_binding_generation = excluded.reported_binding_generation, "
            "reported_account_id = excluded.reported_account_id, "
            "reported_state = excluded.reported_state",
            (
                session.clerk_id,
                session.broker,
                session.agent_instance_id,
                session.routing_epoch,
                session.started_at_ms,
                session.last_seen_at_ms,
                session.reported_binding_generation,
                session.reported_account_id,
                session.reported_state,
            ),
        )

    def touch_session(
        self,
        conn: sqlite3.Connection,
        *,
        clerk_id: str,
        agent_instance_id: str,
        last_seen_at_ms: int,
        reported_binding_generation: int | None,
        reported_account_id: str | None,
        reported_state: str | None,
    ) -> bool:
        """Refresh the heartbeat; only the same instance id may refresh.

        The ``WHERE`` clause — not an earlier read — makes a heartbeat from a
        superseded instance a no-op under a concurrent re-registration.
        """
        cursor = conn.execute(
            "UPDATE clerk_sessions SET last_seen_at_ms = ?, reported_binding_generation = ?, "
            "reported_account_id = ?, reported_state = ? WHERE clerk_id = ? "
            "AND agent_instance_id = ?",
            (
                last_seen_at_ms,
                reported_binding_generation,
                reported_account_id,
                reported_state,
                clerk_id,
                agent_instance_id,
            ),
        )
        return cursor.rowcount == 1

    # ---- assignments ----------------------------------------------------

    _ASSIGNMENT_COLUMNS = (
        "broker, canonical_external_account_id, clerk_id, assignment_generation, state, "
        "effective_profile_id, effective_revision, recorded_at_ms, updated_at_ms"
    )

    def read_assignment(self, *, broker: str, canonical_account_id: str) -> AccountAssignmentRecord | None:
        row = self._query_one(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments "
            "WHERE broker = ? AND canonical_external_account_id = ?",
            (broker, canonical_account_id),
        )
        return None if row is None else _assignment_from_row(row)

    def list_assignments_for_clerk(self, clerk_id: str) -> list[AccountAssignmentRecord]:
        rows = self._query(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments WHERE clerk_id = ? "
            "ORDER BY recorded_at_ms ASC, broker ASC, canonical_external_account_id ASC",
            (clerk_id,),
        )
        return [_assignment_from_row(row) for row in rows]

    def list_active_assignments(self) -> list[AccountAssignmentRecord]:
        rows = self._query(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments "
            "WHERE state <> 'released' ORDER BY broker ASC, canonical_external_account_id ASC"
        )
        return [_assignment_from_row(row) for row in rows]

    def insert_assignment(
        self, conn: sqlite3.Connection, assignment: AccountAssignmentRecord
    ) -> None:
        conn.execute(
            "INSERT INTO account_assignments (broker, canonical_external_account_id, clerk_id, "
            "assignment_generation, state, effective_profile_id, effective_revision, "
            "recorded_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assignment.broker,
                assignment.canonical_external_account_id,
                assignment.clerk_id,
                assignment.assignment_generation,
                str(assignment.state),
                assignment.effective_profile_id,
                assignment.effective_revision,
                assignment.recorded_at_ms,
                assignment.updated_at_ms,
            ),
        )

    def cas_update_assignment(
        self,
        conn: sqlite3.Connection,
        assignment: AccountAssignmentRecord,
        *,
        previous_generation: int,
    ) -> bool:
        """Compare-and-swap one assignment row on its generation.

        Returns ``False`` when the recorded generation moved. Like the profiles
        selection row, the check lives in the ``WHERE`` clause so two racing
        writers cannot both land.
        """
        cursor = conn.execute(
            "UPDATE account_assignments SET clerk_id = ?, assignment_generation = ?, "
            "state = ?, effective_profile_id = ?, effective_revision = ?, updated_at_ms = ? "
            "WHERE broker = ? AND canonical_external_account_id = ? AND assignment_generation = ?",
            (
                assignment.clerk_id,
                assignment.assignment_generation,
                str(assignment.state),
                assignment.effective_profile_id,
                assignment.effective_revision,
                assignment.updated_at_ms,
                assignment.broker,
                assignment.canonical_external_account_id,
                previous_generation,
            ),
        )
        return cursor.rowcount == 1

    # ---- routing receipts -----------------------------------------------

    _RECEIPT_COLUMNS = (
        "correlation_id, broker, clerk_id, operation_kind, nonsecret_target_ref, "
        "idempotency_key, state, upstream_receipt_ref, created_at_ms, updated_at_ms"
    )

    def read_routing_receipt(self, correlation_id: str) -> RoutingReceiptRecord | None:
        row = self._query_one(
            f"SELECT {self._RECEIPT_COLUMNS} FROM routing_receipts WHERE correlation_id = ?",
            (correlation_id,),
        )
        return None if row is None else _receipt_from_row(row)

    def find_routing_receipt_by_idempotency(
        self, *, broker: str, clerk_id: str, idempotency_key: str
    ) -> RoutingReceiptRecord | None:
        row = self._query_one(
            f"SELECT {self._RECEIPT_COLUMNS} FROM routing_receipts "
            "WHERE broker = ? AND clerk_id = ? AND idempotency_key = ?",
            (broker, clerk_id, idempotency_key),
        )
        return None if row is None else _receipt_from_row(row)

    def list_routing_receipts(self, *, clerk_id: str | None = None, limit: int = 100) -> list[RoutingReceiptRecord]:
        sql = f"SELECT {self._RECEIPT_COLUMNS} FROM routing_receipts"
        parameters: tuple[Any, ...] = ()
        if clerk_id is not None:
            sql += " WHERE clerk_id = ?"
            parameters = (clerk_id,)
        sql += " ORDER BY created_at_ms DESC, correlation_id DESC LIMIT ?"
        parameters = (*parameters, limit)
        return [_receipt_from_row(row) for row in self._query(sql, parameters)]

    def insert_routing_receipt(
        self, conn: sqlite3.Connection, receipt: RoutingReceiptRecord
    ) -> None:
        conn.execute(
            "INSERT INTO routing_receipts (correlation_id, broker, clerk_id, operation_kind, "
            "nonsecret_target_ref, idempotency_key, state, upstream_receipt_ref, created_at_ms, "
            "updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.correlation_id,
                receipt.broker,
                receipt.clerk_id,
                receipt.operation_kind,
                receipt.nonsecret_target_ref,
                receipt.idempotency_key,
                str(receipt.state),
                receipt.upstream_receipt_ref,
                receipt.created_at_ms,
                receipt.updated_at_ms,
            ),
        )

    def update_routing_receipt_outcome(
        self,
        conn: sqlite3.Connection,
        *,
        correlation_id: str,
        state: RoutingReceiptState,
        upstream_receipt_ref: str | None,
        updated_at_ms: int,
    ) -> bool:
        cursor = conn.execute(
            "UPDATE routing_receipts SET state = ?, upstream_receipt_ref = ?, "
            "updated_at_ms = ? WHERE correlation_id = ?",
            (str(state), upstream_receipt_ref, updated_at_ms, correlation_id),
        )
        return cursor.rowcount == 1


def _optional_int(row: sqlite3.Row, column: str) -> int | None:
    value = row[column]
    return None if value is None else int(value)


def _clerk_from_row(row: sqlite3.Row) -> ClerkRecord:
    return ClerkRecord(
        clerk_id=row["clerk_id"],
        broker=row["broker"],
        worker_key=row["worker_key"],
        display_label=row["display_label"],
        volume_id=row["volume_id"],
        volume_root=row["volume_root"],
        volume_attestation_kind=row["volume_attestation_kind"],
        volume_attestation_id=row["volume_attestation_id"],
        lifecycle_state=StoredLifecycleState(row["lifecycle_state"]),
        created_at_ms=int(row["created_at_ms"]),
        retired_at_ms=_optional_int(row, "retired_at_ms"),
    )


def _session_from_row(row: sqlite3.Row) -> ClerkSessionRecord:
    return ClerkSessionRecord(
        broker=row["broker"],
        clerk_id=row["clerk_id"],
        agent_instance_id=row["agent_instance_id"],
        routing_epoch=int(row["routing_epoch"]),
        started_at_ms=int(row["started_at_ms"]),
        last_seen_at_ms=int(row["last_seen_at_ms"]),
        reported_binding_generation=_optional_int(row, "reported_binding_generation"),
        reported_account_id=row["reported_account_id"],
        reported_state=row["reported_state"],
    )


def _assignment_from_row(row: sqlite3.Row) -> AccountAssignmentRecord:
    return AccountAssignmentRecord(
        broker=row["broker"],
        canonical_external_account_id=row["canonical_external_account_id"],
        clerk_id=row["clerk_id"],
        assignment_generation=int(row["assignment_generation"]),
        state=AssignmentState(row["state"]),
        effective_profile_id=row["effective_profile_id"],
        effective_revision=_optional_int(row, "effective_revision"),
        recorded_at_ms=int(row["recorded_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _receipt_from_row(row: sqlite3.Row) -> RoutingReceiptRecord:
    return RoutingReceiptRecord(
        correlation_id=row["correlation_id"],
        broker=row["broker"],
        clerk_id=row["clerk_id"],
        operation_kind=row["operation_kind"],
        nonsecret_target_ref=row["nonsecret_target_ref"],
        idempotency_key=row["idempotency_key"],
        state=RoutingReceiptState(row["state"]),
        upstream_receipt_ref=row["upstream_receipt_ref"],
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


__all__ = [
    "DATABASE_DIRECTORY",
    "DATABASE_FILENAME",
    "FleetRegistryStore",
    "new_registry_id",
    "registry_database_path",
]
