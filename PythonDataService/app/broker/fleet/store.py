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
from app.broker.fleet.errors import (
    FleetRegistryRecoveryPending,
    FleetRegistryUnavailable,
)
from app.broker.fleet.records import (
    AccountAssignmentRecord,
    ApprovedEndpointRecord,
    AssignmentState,
    ClerkRecord,
    ClerkSessionRecord,
    RoutingReceiptRecord,
    RoutingReceiptState,
    StoredLifecycleState,
)
from app.utils.advisory_lock import advisory_file_lock
from app.utils.timestamps import now_ms_utc

DATABASE_DIRECTORY = "fleet"
DATABASE_FILENAME = "registry.db"


def registry_database_path(control_dir: Path) -> Path:
    """Where the fleet registry lives: the coordinator's own control volume."""
    return control_dir / DATABASE_DIRECTORY / DATABASE_FILENAME


def new_registry_id() -> str:
    """Mint one opaque registry identity for a fresh control volume."""
    return f"fltr_{secrets.token_hex(12)}"


class FleetRegistryStore:
    """One open fleet registry database."""

    def __init__(self, conn: sqlite3.Connection, *, db_path: Path) -> None:
        """Hold one open connection and the reentrant write lock."""
        self._conn = conn
        self._lock = RLock()
        self.db_path = db_path
        self._operation_depth = 0
        self._database_identity = self._path_identity()

    # ---- lifecycle ------------------------------------------------------

    @classmethod
    def open(cls, *, control_dir: Path) -> FleetRegistryStore:
        """Open, creating and migrating under a cross-process advisory lock."""
        db_path = registry_database_path(control_dir)
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with advisory_file_lock(db_path):
                creating = not db_path.exists()
                conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    schema.configure_connection(conn)
                    cls._establish(conn, db_path=db_path)
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
    def _establish(conn: sqlite3.Connection, *, db_path: Path) -> None:
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

        # An existing registry must prove its integrity before the store is
        # handed out: corruption confined to the assignment tables or indexes
        # would otherwise reach the account-ownership fence as if it were
        # truth. ``quick_check`` is the fast variant of ``integrity_check``.
        integrity = conn.execute("PRAGMA quick_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            findings = "" if integrity is None else f": {integrity[0]}"
            raise FleetRegistryUnavailable(
                f"The fleet registry at {db_path} failed its integrity check{findings}.",
                next_step="Restore the registry from the coordinator control volume's "
                "backup, then retry.",
            )

    def close(self) -> None:
        """Close the registry connection."""
        with self._lock:
            self._conn.close()

    def _path_identity(self) -> tuple[int, int]:
        """Return the filesystem identity backing the live registry path."""
        try:
            stat = self.db_path.stat()
        except OSError as exc:
            raise FleetRegistryUnavailable(
                f"The fleet registry path {self.db_path} is unavailable: {exc}",
                next_step="Keep routing closed and inspect the coordinator control volume.",
            ) from exc
        return stat.st_dev, stat.st_ino

    def _require_current_database(self) -> None:
        """Refuse a connection whose database path was replaced by recovery."""
        if self._path_identity() != self._database_identity:
            raise FleetRegistryRecoveryPending(
                "This coordinator still holds the pre-restore fleet registry connection.",
                next_step="Restart the coordinator so it opens the restored registry before "
                "routing or assignment mutation resumes.",
            )

    @contextmanager
    def _current_database_operation(self) -> Iterator[None]:
        """Serialize path replacement and reject stale open SQLite handles."""
        with self._lock:
            if self._operation_depth:
                self._require_current_database()
                yield
                self._require_current_database()
                return
            with advisory_file_lock(self.db_path):
                self._operation_depth = 1
                try:
                    self._require_current_database()
                    yield
                    self._require_current_database()
                finally:
                    self._operation_depth = 0

    def backup_to(self, database: Path) -> None:
        """Write a consistent SQLite snapshot under the connection lock."""
        with self._current_database_operation():
            destination = sqlite3.connect(database)
            try:
                self._conn.backup(destination)
            finally:
                destination.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One serialized write; nested operations stay inside the outer commit."""
        with self._current_database_operation():
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
        with self._current_database_operation():
            if self._conn.in_transaction:
                yield
                return
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                self._conn.rollback()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run one read query under the connection lock."""
        with self._current_database_operation():
            return self._conn.execute(sql, parameters).fetchall()

    def _query_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Run one single-row read query under the connection lock."""
        with self._current_database_operation():
            return self._conn.execute(sql, parameters).fetchone()

    @property
    def schema_version(self) -> int:
        """The registry's schema version, from the guarded meta row."""
        row = self._query_one("SELECT schema_version FROM fleet_meta WHERE id = 1")
        if row is None:
            raise FleetRegistryUnavailable(
                "The fleet registry lost its metadata row.",
                next_step="Move the file aside and let the coordinator create a fresh registry.",
            )
        return int(row["schema_version"])

    @property
    def registry_id(self) -> str:
        """The registry's opaque identity, stable across reopens."""
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
        "deployment_namespace, volume_attestation_kind, volume_attestation_id, "
        "lifecycle_state, created_at_ms, retired_at_ms"
    )

    def read_clerk(self, clerk_id: str) -> ClerkRecord | None:
        """Read one clerk row by identity."""
        row = self._query_one(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE clerk_id = ?",
            (clerk_id,),
        )
        return None if row is None else _clerk_from_row(row)

    def read_clerk_on(
        self, conn: sqlite3.Connection, clerk_id: str
    ) -> ClerkRecord | None:
        """Read one clerk row on a caller's transaction connection.

        For rechecks that must serialize with a concurrent write: reading
        inside the ``BEGIN IMMEDIATE`` transaction is what excludes a rival
        transition, where the lock-free ``read_clerk`` only excludes other
        uses of this store's own connection.
        """
        row = conn.execute(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE clerk_id = ?",
            (clerk_id,),
        ).fetchone()
        return None if row is None else _clerk_from_row(row)

    def _clerk_list_sql(self, *, include_retired: bool) -> str:
        """The listing SQL ``list_clerks`` and ``list_clerks_on`` both need.

        One shared string so the provisioning pre-check's transactional read
        and the lock-free directory read can never drift apart on their
        WHERE/ORDER BY.
        """
        sql = f"SELECT {self._CLERK_COLUMNS} FROM clerks"
        if not include_retired:
            sql += " WHERE lifecycle_state <> 'retired'"
        sql += " ORDER BY broker ASC, created_at_ms ASC, clerk_id ASC"
        return sql

    def list_clerks_on(self, conn: sqlite3.Connection) -> list[ClerkRecord]:
        """List the active clerks on a caller's transaction connection.

        The transactional twin of ``list_clerks``, for pre-checks that must
        serialize with a concurrent write: reading inside the ``BEGIN
        IMMEDIATE`` transaction is what excludes a rival provisioning
        committing between the check and the insert it guards.
        """
        rows = conn.execute(self._clerk_list_sql(include_retired=False)).fetchall()
        return [_clerk_from_row(row) for row in rows]

    def read_assignment_on(
        self,
        conn: sqlite3.Connection,
        *,
        broker: str,
        canonical_account_id: str,
    ) -> AccountAssignmentRecord | None:
        """Read one assignment row on a caller's transaction connection."""
        row = conn.execute(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments "
            "WHERE broker = ? AND canonical_external_account_id = ?",
            (broker, canonical_account_id),
        ).fetchone()
        return None if row is None else _assignment_from_row(row)

    def find_clerk_by_volume(self, volume_id: str) -> ClerkRecord | None:
        """The active clerk owning a volume identity, if any (clone detection)."""
        row = self._query_one(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE volume_id = ? "
            "AND lifecycle_state <> 'retired'",
            (volume_id,),
        )
        return None if row is None else _clerk_from_row(row)

    def find_clerk_by_attestation(
        self,
        *,
        deployment_namespace: str,
        attestation_kind: str,
        attestation_id: str,
    ) -> ClerkRecord | None:
        """The active clerk owning one namespaced volume attestation, if any."""
        row = self._query_one(
            f"SELECT {self._CLERK_COLUMNS} FROM clerks WHERE deployment_namespace = ? "
            "AND volume_attestation_kind = ? AND volume_attestation_id = ? "
            "AND lifecycle_state <> 'retired'",
            (deployment_namespace, attestation_kind, attestation_id),
        )
        return None if row is None else _clerk_from_row(row)

    def list_clerks(self, *, include_retired: bool = False) -> list[ClerkRecord]:
        """List clerks in directory order, optionally including retired ones."""
        sql = self._clerk_list_sql(include_retired=include_retired)
        return [_clerk_from_row(row) for row in self._query(sql)]

    def insert_clerk(self, conn: sqlite3.Connection, clerk: ClerkRecord) -> None:
        """Insert one freshly provisioned clerk row."""
        conn.execute(
            "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, volume_id, "
            "volume_root, deployment_namespace, volume_attestation_kind, "
            "volume_attestation_id, lifecycle_state, created_at_ms, retired_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clerk.clerk_id,
                clerk.broker,
                clerk.worker_key,
                clerk.display_label,
                clerk.volume_id,
                clerk.volume_root,
                clerk.deployment_namespace,
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
        """Move one clerk's durable lifecycle state forward."""
        cursor = conn.execute(
            "UPDATE clerks SET lifecycle_state = ?, retired_at_ms = ? WHERE clerk_id = ?",
            (str(lifecycle_state), retired_at_ms, clerk_id),
        )
        return cursor.rowcount == 1

    # ---- sessions -------------------------------------------------------

    _SESSION_COLUMNS = (
        "broker, clerk_id, agent_instance_id, routing_epoch, started_at_ms, "
        "last_seen_at_ms, reported_binding_generation, reported_account_id, "
        "reported_state, endpoint_ref, adapter_version, reported_summary_json"
    )

    def read_session(self, clerk_id: str) -> ClerkSessionRecord | None:
        """Read the clerk's one current session row."""
        row = self._query_one(
            f"SELECT {self._SESSION_COLUMNS} FROM clerk_sessions WHERE clerk_id = ?",
            (clerk_id,),
        )
        return None if row is None else _session_from_row(row)

    def read_session_on(
        self, conn: sqlite3.Connection, clerk_id: str
    ) -> ClerkSessionRecord | None:
        """Read the current session row on a caller's transaction connection.

        Confirmation must compare the session *inside* its write transaction:
        a superseded instance otherwise slips its confirmation between the
        read and the commit (audit 2026-09-13, finding 1).
        """
        row = conn.execute(
            f"SELECT {self._SESSION_COLUMNS} FROM clerk_sessions WHERE clerk_id = ?",
            (clerk_id,),
        ).fetchone()
        return None if row is None else _session_from_row(row)

    def list_sessions(self) -> list[ClerkSessionRecord]:
        """List every current session row in directory order."""
        rows = self._query(
            f"SELECT {self._SESSION_COLUMNS} FROM clerk_sessions "
            "ORDER BY broker ASC, clerk_id ASC"
        )
        return [_session_from_row(row) for row in rows]

    def archive_session(
        self, conn: sqlite3.Connection, session: ClerkSessionRecord, *, superseded_at_ms: int
    ) -> None:
        """Move one superseded session into the append-only history."""
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
            "reported_state, endpoint_ref, adapter_version, reported_summary_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(clerk_id) DO UPDATE SET broker = excluded.broker, "
            "agent_instance_id = excluded.agent_instance_id, "
            "routing_epoch = excluded.routing_epoch, "
            "started_at_ms = excluded.started_at_ms, "
            "last_seen_at_ms = excluded.last_seen_at_ms, "
            "reported_binding_generation = excluded.reported_binding_generation, "
            "reported_account_id = excluded.reported_account_id, "
            "reported_state = excluded.reported_state, "
            "endpoint_ref = excluded.endpoint_ref, "
            "adapter_version = excluded.adapter_version, "
            "reported_summary_json = excluded.reported_summary_json",
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
                session.endpoint_ref,
                session.adapter_version,
                session.reported_summary_json,
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
        reported_summary_json: str | None = None,
    ) -> bool:
        """Refresh the heartbeat; only the same instance id may refresh.

        The ``WHERE`` clause — not an earlier read — makes a heartbeat from a
        superseded instance a no-op under a concurrent re-registration. The
        summary is preserved when the caller reports none, so a plain
        heartbeat cannot blank the lane's typed summary.
        """
        cursor = conn.execute(
            "UPDATE clerk_sessions SET last_seen_at_ms = ?, reported_binding_generation = ?, "
            "reported_account_id = ?, reported_state = ?, "
            "reported_summary_json = COALESCE(?, reported_summary_json) "
            "WHERE clerk_id = ? AND agent_instance_id = ?",
            (
                last_seen_at_ms,
                reported_binding_generation,
                reported_account_id,
                reported_state,
                reported_summary_json,
                clerk_id,
                agent_instance_id,
            ),
        )
        return cursor.rowcount == 1

    # ---- assignments ----------------------------------------------------

    _ASSIGNMENT_COLUMNS = (
        "broker, canonical_external_account_id, clerk_id, assignment_generation, state, "
        "effective_profile_id, effective_revision, confirmed_binding_generation, "
        "confirmed_profile_id, confirmed_revision, confirmed_at_ms, "
        "confirmed_agent_instance_id, confirmed_routing_epoch, recorded_at_ms, updated_at_ms"
    )

    def read_assignment(self, *, broker: str, canonical_account_id: str) -> AccountAssignmentRecord | None:
        """Read one broker-qualified assignment row."""
        row = self._query_one(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments "
            "WHERE broker = ? AND canonical_external_account_id = ?",
            (broker, canonical_account_id),
        )
        return None if row is None else _assignment_from_row(row)

    def list_assignments_for_clerk(self, clerk_id: str) -> list[AccountAssignmentRecord]:
        """List every assignment row a clerk has ever owned."""
        rows = self._query(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments WHERE clerk_id = ? "
            "ORDER BY recorded_at_ms ASC, broker ASC, canonical_external_account_id ASC",
            (clerk_id,),
        )
        return [_assignment_from_row(row) for row in rows]

    def list_assignments_for_clerk_on(
        self, conn: sqlite3.Connection, clerk_id: str
    ) -> list[AccountAssignmentRecord]:
        """List one clerk's assignments inside a caller's write transaction."""
        rows = conn.execute(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments WHERE clerk_id = ? "
            "ORDER BY recorded_at_ms ASC, broker ASC, canonical_external_account_id ASC",
            (clerk_id,),
        ).fetchall()
        return [_assignment_from_row(row) for row in rows]

    def list_active_assignments(self) -> list[AccountAssignmentRecord]:
        """List every non-released assignment row."""
        rows = self._query(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments "
            "WHERE state <> 'released' ORDER BY broker ASC, canonical_external_account_id ASC"
        )
        return [_assignment_from_row(row) for row in rows]

    def list_effective_assignments_for_clerk(
        self, clerk_id: str
    ) -> list[AccountAssignmentRecord]:
        """The clerk's effective assignments — the confirmed routing fence."""
        rows = self._query(
            f"SELECT {self._ASSIGNMENT_COLUMNS} FROM account_assignments "
            "WHERE clerk_id = ? AND state = 'effective' "
            "ORDER BY recorded_at_ms ASC, broker ASC, canonical_external_account_id ASC",
            (clerk_id,),
        )
        return [_assignment_from_row(row) for row in rows]

    def insert_assignment(
        self, conn: sqlite3.Connection, assignment: AccountAssignmentRecord
    ) -> None:
        """Insert the first reservation for one broker-qualified account."""
        conn.execute(
            "INSERT INTO account_assignments (broker, canonical_external_account_id, clerk_id, "
            "assignment_generation, state, effective_profile_id, effective_revision, "
            "confirmed_binding_generation, confirmed_profile_id, confirmed_revision, "
            "confirmed_at_ms, confirmed_agent_instance_id, confirmed_routing_epoch, "
            "recorded_at_ms, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _assignment_values(assignment),
        )
        self.append_assignment_history(conn, assignment)

    def cas_update_assignment(
        self,
        conn: sqlite3.Connection,
        assignment: AccountAssignmentRecord,
        *,
        previous_generation: int,
        previous_state: AssignmentState,
    ) -> bool:
        """Compare-and-swap one assignment row on its generation *and* state.

        Returns ``False`` when the recorded row moved. Generation alone does
        not fence transitions that keep it: confirmation and release both
        leave the number untouched, so a stale reader could otherwise land its
        transition over the other's. The check lives in the ``WHERE`` clause
        so two racing writers cannot both land.
        """
        values = _assignment_values(assignment)
        cursor = conn.execute(
            "UPDATE account_assignments SET clerk_id = ?, assignment_generation = ?, "
            "state = ?, effective_profile_id = ?, effective_revision = ?, "
            "confirmed_binding_generation = ?, confirmed_profile_id = ?, "
            "confirmed_revision = ?, confirmed_at_ms = ?, confirmed_agent_instance_id = ?, "
            "confirmed_routing_epoch = ?, updated_at_ms = ? "
            "WHERE broker = ? AND canonical_external_account_id = ? "
            "AND assignment_generation = ? AND state = ?",
            (
                *values[2:13],
                values[14],
                values[0],
                values[1],
                previous_generation,
                str(previous_state),
            ),
        )
        if cursor.rowcount == 1:
            self.append_assignment_history(conn, assignment)
        return cursor.rowcount == 1

    def reassign_assignment(
        self,
        conn: sqlite3.Connection,
        *,
        released: AccountAssignmentRecord,
        reserved_successor: AccountAssignmentRecord,
        previous_state: AssignmentState,
    ) -> None:
        """Release then reserve inside one caller-owned SQLite transaction.

        Both rows address the same broker-qualified account. A failure to
        install the successor raises while the transaction is still active so
        the release rolls back with it; reassignment can never leave a split
        two-commit handover behind.
        """
        if not self.cas_update_assignment(
            conn,
            released,
            previous_generation=released.assignment_generation,
            previous_state=previous_state,
        ):
            raise sqlite3.IntegrityError("assignment changed before release")
        if not self.cas_update_assignment(
            conn,
            reserved_successor,
            previous_generation=released.assignment_generation,
            previous_state=AssignmentState.RELEASED,
        ):
            raise sqlite3.IntegrityError("successor reservation refused")

    # ---- assignment history ----------------------------------------------

    def append_assignment_history(
        self, conn: sqlite3.Connection, assignment: AccountAssignmentRecord
    ) -> None:
        """Record one ownership transition in the append-only audit table.

        The current row in ``account_assignments`` is a pointer; this table is
        what makes a release auditable after the account is re-reserved —
        re-reservation overwrites the pointer, never the past. Only ownership
        transitions land here: a confirmed-observation refinement inside the
        same (generation, state) is not one, so the insert is ignored on the
        primary key rather than colliding. The confirmed observation lives on
        the current row only.
        """
        conn.execute(
            "INSERT OR IGNORE INTO account_assignment_history (broker, "
            "canonical_external_account_id, clerk_id, assignment_generation, state, "
            "effective_profile_id, effective_revision, recorded_at_ms, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    _HISTORY_ASSIGNMENT_COLUMNS = (
        "broker, canonical_external_account_id, clerk_id, assignment_generation, state, "
        "effective_profile_id, effective_revision, recorded_at_ms, updated_at_ms"
    )

    def list_assignment_history(
        self, *, broker: str, canonical_account_id: str
    ) -> list[AccountAssignmentRecord]:
        """Every ownership transition for one broker-qualified account, in order."""
        rows = self._query(
            f"SELECT {self._HISTORY_ASSIGNMENT_COLUMNS} FROM account_assignment_history "
            "WHERE broker = ? AND canonical_external_account_id = ? "
            "ORDER BY rowid ASC",
            (broker, canonical_account_id),
        )
        return [_history_from_row(row) for row in rows]

    # ---- approved endpoints ------------------------------------------------

    _ENDPOINT_COLUMNS = "endpoint_ref, clerk_id, base_url, created_at_ms, updated_at_ms"

    def read_approved_endpoint(self, clerk_id: str) -> ApprovedEndpointRecord | None:
        """The clerk's one approved internal endpoint, if the host approved one."""
        row = self._query_one(
            f"SELECT {self._ENDPOINT_COLUMNS} FROM approved_endpoints WHERE clerk_id = ?",
            (clerk_id,),
        )
        return None if row is None else _endpoint_from_row(row)

    def approve_endpoint(
        self,
        conn: sqlite3.Connection,
        *,
        clerk_id: str,
        endpoint_ref: str,
        base_url: str,
        now_ms: int,
    ) -> ApprovedEndpointRecord:
        """Install or re-target the clerk's approved endpoint (host ceremony only).

        Keyed by clerk: the clerk binding and endpoint reference are stable,
        and re-approval moves only the destination URL. The no-delete trigger
        keeps approvals from vanishing; the identity trigger keeps a row from
        migrating onto another clerk or another reference.
        """
        created = self.read_approved_endpoint(clerk_id)
        if created is None:
            record = ApprovedEndpointRecord(
                endpoint_ref=endpoint_ref,
                clerk_id=clerk_id,
                base_url=base_url,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            conn.execute(
                "INSERT INTO approved_endpoints (endpoint_ref, clerk_id, base_url, "
                "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
                (
                    record.endpoint_ref,
                    record.clerk_id,
                    record.base_url,
                    record.created_at_ms,
                    record.updated_at_ms,
                ),
            )
            return record
        if created.endpoint_ref != endpoint_ref:
            raise sqlite3.IntegrityError(
                f"clerk {clerk_id} already approves endpoint reference "
                f"{created.endpoint_ref!r}; a reference is stable, re-approve it "
                "with a new destination instead"
            )
        updated = ApprovedEndpointRecord(
            endpoint_ref=endpoint_ref,
            clerk_id=clerk_id,
            base_url=base_url,
            created_at_ms=created.created_at_ms,
            updated_at_ms=now_ms,
        )
        cursor = conn.execute(
            "UPDATE approved_endpoints SET base_url = ?, updated_at_ms = ? "
            "WHERE clerk_id = ? AND endpoint_ref = ? AND base_url = ?",
            (base_url, now_ms, clerk_id, endpoint_ref, created.base_url),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError(
                "the approved endpoint moved while re-approving; re-read and retry"
            )
        return updated

    # ---- routing receipts -----------------------------------------------

    _RECEIPT_COLUMNS = (
        "correlation_id, broker, clerk_id, operation_kind, nonsecret_target_ref, "
        "idempotency_key, state, upstream_receipt_ref, pinned_routing_epoch, "
        "pinned_binding_generation, pinned_agent_instance_id, dispatched_at_ms, "
        "created_at_ms, updated_at_ms"
    )

    def read_routing_receipt(self, correlation_id: str) -> RoutingReceiptRecord | None:
        """Read one routing receipt by correlation identity."""
        row = self._query_one(
            f"SELECT {self._RECEIPT_COLUMNS} FROM routing_receipts WHERE correlation_id = ?",
            (correlation_id,),
        )
        return None if row is None else _receipt_from_row(row)

    def find_routing_receipt_by_idempotency(
        self, *, broker: str, clerk_id: str, idempotency_key: str
    ) -> RoutingReceiptRecord | None:
        """Find a lane's receipt for one idempotency key."""
        row = self._query_one(
            f"SELECT {self._RECEIPT_COLUMNS} FROM routing_receipts "
            "WHERE broker = ? AND clerk_id = ? AND idempotency_key = ?",
            (broker, clerk_id, idempotency_key),
        )
        return None if row is None else _receipt_from_row(row)

    def list_routing_receipts(
        self,
        *,
        clerk_id: str | None = None,
        since_ms: int | None = None,
        before_ms: int | None = None,
        before_correlation_id: str | None = None,
        limit: int = 100,
    ) -> list[RoutingReceiptRecord]:
        """List receipts, newest first, optionally for one clerk, since a bound,
        and/or continuing a keyset page.

        ``since_ms``, when given, is an *inclusive* lower bound on
        ``created_at_ms`` (``created_at_ms >= since_ms``) — the audit read
        surface's contract (#2104).

        ``before_ms``/``before_correlation_id``, when given together, seek
        strictly before that ``(created_at_ms, correlation_id)`` pair in
        exactly the tuple order of the ``ORDER BY`` below — the keyset
        pagination contract (#2133). The tiebreak on ``correlation_id`` is
        load-bearing: two receipts can share one ``created_at_ms`` (a frozen
        test clock, or a genuine millisecond collision), and a bound on
        ``created_at_ms`` alone would silently skip or repeat rows sharing
        the page boundary's timestamp. The two values must be given together
        — a caller cannot seek past a timestamp without also naming which row
        at that timestamp it has already consumed.
        """
        if (before_ms is None) != (before_correlation_id is None):
            raise ValueError(
                "before_ms and before_correlation_id must be given together or not at all"
            )
        sql = f"SELECT {self._RECEIPT_COLUMNS} FROM routing_receipts"
        clauses: list[str] = []
        parameters: list[Any] = []
        if clerk_id is not None:
            clauses.append("clerk_id = ?")
            parameters.append(clerk_id)
        if since_ms is not None:
            clauses.append("created_at_ms >= ?")
            parameters.append(since_ms)
        if before_ms is not None:
            clauses.append(
                "(created_at_ms < ? OR (created_at_ms = ? AND correlation_id < ?))"
            )
            parameters.extend([before_ms, before_ms, before_correlation_id])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at_ms DESC, correlation_id DESC LIMIT ?"
        parameters.append(limit)
        return [_receipt_from_row(row) for row in self._query(sql, tuple(parameters))]

    def insert_routing_receipt(
        self, conn: sqlite3.Connection, receipt: RoutingReceiptRecord
    ) -> None:
        """Insert one routing attempt with its pinned context, pre-dispatch."""
        conn.execute(
            "INSERT INTO routing_receipts (correlation_id, broker, clerk_id, operation_kind, "
            "nonsecret_target_ref, idempotency_key, state, upstream_receipt_ref, "
            "pinned_routing_epoch, pinned_binding_generation, pinned_agent_instance_id, "
            "dispatched_at_ms, created_at_ms, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.correlation_id,
                receipt.broker,
                receipt.clerk_id,
                receipt.operation_kind,
                receipt.nonsecret_target_ref,
                receipt.idempotency_key,
                str(receipt.state),
                receipt.upstream_receipt_ref,
                receipt.pinned_routing_epoch,
                receipt.pinned_binding_generation,
                receipt.pinned_agent_instance_id,
                receipt.dispatched_at_ms,
                receipt.created_at_ms,
                receipt.updated_at_ms,
            ),
        )

    def mark_routing_receipt_dispatched(
        self, conn: sqlite3.Connection, *, correlation_id: str, dispatched_at_ms: int
    ) -> bool:
        """Set the dispatch timestamp; one-way and idempotent."""
        cursor = conn.execute(
            "UPDATE routing_receipts SET dispatched_at_ms = "
            "COALESCE(dispatched_at_ms, ?), updated_at_ms = ? WHERE correlation_id = ?",
            (dispatched_at_ms, dispatched_at_ms, correlation_id),
        )
        return cursor.rowcount == 1

    def update_routing_receipt_outcome(
        self,
        conn: sqlite3.Connection,
        *,
        correlation_id: str,
        state: RoutingReceiptState,
        upstream_receipt_ref: str | None,
        updated_at_ms: int,
    ) -> bool:
        """Move one receipt's outcome state forward.

        The delivered-terminal and dispatch-monotonic triggers are the fence;
        this method is the single write seam so both service rules and schema
        triggers see the same transitions.
        """
        cursor = conn.execute(
            "UPDATE routing_receipts SET state = ?, upstream_receipt_ref = "
            "COALESCE(?, upstream_receipt_ref), updated_at_ms = ? WHERE correlation_id = ?",
            (str(state), upstream_receipt_ref, updated_at_ms, correlation_id),
        )
        return cursor.rowcount == 1


def _optional_int(row: sqlite3.Row, column: str) -> int | None:
    """Read one nullable INTEGER column back as ``int | None``."""
    value = row[column]
    return None if value is None else int(value)


def _clerk_from_row(row: sqlite3.Row) -> ClerkRecord:
    """Map one clerks row to its record."""
    return ClerkRecord(
        clerk_id=row["clerk_id"],
        broker=row["broker"],
        worker_key=row["worker_key"],
        display_label=row["display_label"],
        volume_id=row["volume_id"],
        volume_root=row["volume_root"],
        deployment_namespace=row["deployment_namespace"],
        volume_attestation_kind=row["volume_attestation_kind"],
        volume_attestation_id=row["volume_attestation_id"],
        lifecycle_state=StoredLifecycleState(row["lifecycle_state"]),
        created_at_ms=int(row["created_at_ms"]),
        retired_at_ms=_optional_int(row, "retired_at_ms"),
    )


def _session_from_row(row: sqlite3.Row) -> ClerkSessionRecord:
    """Map one clerk_sessions row to its record."""
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
        endpoint_ref=row["endpoint_ref"],
        adapter_version=row["adapter_version"],
        reported_summary_json=row["reported_summary_json"],
    )


def _assignment_values(assignment: AccountAssignmentRecord) -> tuple[Any, ...]:
    """One ordered parameter tuple shared by insert and compare-and-swap."""
    return (
        assignment.broker,
        assignment.canonical_external_account_id,
        assignment.clerk_id,
        assignment.assignment_generation,
        str(assignment.state),
        assignment.effective_profile_id,
        assignment.effective_revision,
        assignment.confirmed_binding_generation,
        assignment.confirmed_profile_id,
        assignment.confirmed_revision,
        assignment.confirmed_at_ms,
        assignment.confirmed_agent_instance_id,
        assignment.confirmed_routing_epoch,
        assignment.recorded_at_ms,
        assignment.updated_at_ms,
    )


def _assignment_from_row(row: sqlite3.Row) -> AccountAssignmentRecord:
    """Map one assignment row to its record."""
    return AccountAssignmentRecord(
        broker=row["broker"],
        canonical_external_account_id=row["canonical_external_account_id"],
        clerk_id=row["clerk_id"],
        assignment_generation=int(row["assignment_generation"]),
        state=AssignmentState(row["state"]),
        effective_profile_id=row["effective_profile_id"],
        effective_revision=_optional_int(row, "effective_revision"),
        confirmed_binding_generation=_optional_int(row, "confirmed_binding_generation"),
        confirmed_profile_id=row["confirmed_profile_id"],
        confirmed_revision=_optional_int(row, "confirmed_revision"),
        confirmed_at_ms=_optional_int(row, "confirmed_at_ms"),
        confirmed_agent_instance_id=row["confirmed_agent_instance_id"],
        confirmed_routing_epoch=_optional_int(row, "confirmed_routing_epoch"),
        recorded_at_ms=int(row["recorded_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _endpoint_from_row(row: sqlite3.Row) -> ApprovedEndpointRecord:
    """Map one approved_endpoints row to its record."""
    return ApprovedEndpointRecord(
        endpoint_ref=row["endpoint_ref"],
        clerk_id=row["clerk_id"],
        base_url=row["base_url"],
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _history_from_row(row: sqlite3.Row) -> AccountAssignmentRecord:
    """Map one history row: ownership facts only, no confirmed observation."""
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
    """Map one routing_receipts row to its record."""
    return RoutingReceiptRecord(
        correlation_id=row["correlation_id"],
        broker=row["broker"],
        clerk_id=row["clerk_id"],
        operation_kind=row["operation_kind"],
        nonsecret_target_ref=row["nonsecret_target_ref"],
        idempotency_key=row["idempotency_key"],
        state=RoutingReceiptState(row["state"]),
        upstream_receipt_ref=row["upstream_receipt_ref"],
        pinned_routing_epoch=_optional_int(row, "pinned_routing_epoch"),
        pinned_binding_generation=_optional_int(row, "pinned_binding_generation"),
        pinned_agent_instance_id=row["pinned_agent_instance_id"],
        dispatched_at_ms=_optional_int(row, "dispatched_at_ms"),
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
