"""The profiles database: open it once, read and write it transactionally.

Storage only. Every rule about what a write *means* — conflicts, archive
refusals, idempotency, completeness — lives in ``service.py``; this module owns
the connection, the versioned schema, the row/record mapping, and nothing else.

Opening is guarded two ways, both borrowed from the Clerk rather than invented
here: the WAL-filesystem check (``assert_wal_filesystem_supported``) refuses a
bind mount SQLite cannot lock correctly, and a cross-process advisory file lock
serializes initialization and migration so two processes starting at once
cannot both create or both upgrade.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from app.broker_configuration import schema
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import ProfilesDatabaseUnavailable
from app.broker_configuration.records import (
    AccountNickname,
    BrokerProfile,
    ConfigurationEvent,
    InstallationSelection,
    LocalOwner,
    ProfileRevision,
)
from app.utils.timestamps import now_ms_utc

DATABASE_DIRECTORY = "broker_configuration"
DATABASE_FILENAME = "profiles.db"

# ``ValidatedLiveEnvelope`` field -> its column. Order matters and is not
# stated twice: the column list, the INSERT's placeholders and its parameter
# tuple below are all derived from this one mapping. Two hand-kept lists would
# let a transposition of, say, the two bps columns pass every test while
# changing the sha in production.
_ENVELOPE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("loss_fraction", "live_loss_fraction"),
    ("loss_usd", "live_loss_usd"),
    ("shadow_sessions", "live_shadow_sessions"),
    ("arming_max_sessions", "live_arming_max_sessions"),
    ("xh_entry_bps", "live_xh_entry_bps"),
    ("xh_exit_bps", "live_xh_exit_bps"),
)

_REVISION_COLUMN_NAMES: tuple[str, ...] = (
    "profile_id",
    "revision",
    "schema_version",
    "credential_slot",
    "endpoint_mode",
    "account_pin",
    "account_pinned_at_ms",
    *(column for _, column in _ENVELOPE_COLUMNS),
    "content_sha256",
    "complete",
    "author_owner_id",
    "created_at_ms",
)
_REVISION_COLUMNS = ", ".join(_REVISION_COLUMN_NAMES)
_REVISION_PLACEHOLDERS = ", ".join("?" * len(_REVISION_COLUMN_NAMES))


def profiles_database_path(clerk_dir: Path) -> Path:
    """Where the profiles database lives: a sibling of ``accounts/``.

    On the Clerk volume so it cannot be lost while the custody and arming
    records it explains survive a ``podman compose down -v`` (ADR 0060 D2).
    """
    return clerk_dir / DATABASE_DIRECTORY / DATABASE_FILENAME


def _discard_partial_database(db_path: Path) -> None:
    """Remove a database this process created but never finished establishing."""
    for path in (db_path, db_path.with_name(f"{db_path.name}-wal"), db_path.with_name(f"{db_path.name}-shm")):
        path.unlink(missing_ok=True)


def new_identifier(prefix: str) -> str:
    """An opaque server-generated identifier.

    Never derived from an account, a seal or a label: a profile ID must not be
    mistakable for a Clerk account directory or an authority generation
    (contract §1).
    """
    return f"{prefix}_{secrets.token_hex(8)}"


class ProfilesStore:
    """One open profiles database."""

    def __init__(self, conn: sqlite3.Connection, *, db_path: Path) -> None:
        self._conn = conn
        self._lock = RLock()
        self.db_path = db_path

    # ---- lifecycle ------------------------------------------------------

    @classmethod
    def open(cls, *, clerk_dir: Path) -> ProfilesStore:
        """Open, creating and migrating under a cross-process advisory lock."""
        from app.broker.alpaca.clerk.sqlite.repository import UnsupportedWalFilesystem
        from app.broker.alpaca.clerk.sqlite.repository_lifecycle import (
            assert_wal_filesystem_supported,
        )
        from app.utils.advisory_lock import advisory_file_lock

        db_path = profiles_database_path(clerk_dir)
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            assert_wal_filesystem_supported(db_path.parent)
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
                        # A file with tables but no metadata row is one the next
                        # open() refuses with "move it aside" — a manual
                        # intervention conjured out of a transient disk error.
                        # We know this file did not exist a moment ago, so
                        # removing it lets the next attempt start clean.
                        _discard_partial_database(db_path)
                    raise
        except ProfilesDatabaseUnavailable:
            raise
        # Named exhaustively rather than caught broadly: an unreadable database
        # is a state with a reason, and a bug in this module must stay a bug
        # instead of being reported to an operator as "volume unavailable".
        except (OSError, sqlite3.DatabaseError, UnsupportedWalFilesystem, ValueError) as exc:
            raise ProfilesDatabaseUnavailable(
                f"The broker configuration database at {db_path} could not be opened: {exc}",
                next_step="Check the Clerk volume is mounted, writable and container-local, then retry.",
            ) from exc
        return cls(conn, db_path=db_path)

    @staticmethod
    def _establish(conn: sqlite3.Connection) -> None:
        """Create the schema, or advance an older one to ``SCHEMA_VERSION``."""
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'configuration_meta'"
        ).fetchone()
        if row is None:
            schema.apply_schema(conn)
            now = now_ms_utc()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO configuration_meta (id, schema_version, created_at_ms, updated_at_ms) "
                    "VALUES (1, ?, ?, ?)",
                    (schema.SCHEMA_VERSION, now, now),
                )
                conn.execute(
                    "INSERT INTO installation_selection "
                    "(id, staged_profile_id, staged_revision, apply_requested, "
                    "apply_requested_at_ms, apply_requested_generation, selection_generation, "
                    "effective_profile_id, effective_revision, effective_account_id, "
                    "effective_acknowledged_at_ms, last_apply_outcome, last_apply_refusal_reason) "
                    "VALUES (1, NULL, NULL, 0, NULL, NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL)"
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            return

        meta = conn.execute("SELECT schema_version FROM configuration_meta WHERE id = 1").fetchone()
        if meta is None:
            raise ProfilesDatabaseUnavailable(
                "The broker configuration database has no metadata row and is not a profiles database.",
                next_step="Move the file aside and let the service create a fresh database.",
            )
        stored = int(meta["schema_version"])
        if stored > schema.SCHEMA_VERSION:
            raise ProfilesDatabaseUnavailable(
                f"The broker configuration database is schema_version={stored}, newer than this "
                f"build's {schema.SCHEMA_VERSION}.",
                next_step="Run the build that wrote it, or restore the matching version.",
            )
        if stored < schema.SCHEMA_VERSION:
            if not schema.is_upgradable_to_current(stored):
                raise ProfilesDatabaseUnavailable(
                    f"No registered upgrade path reaches schema_version={schema.SCHEMA_VERSION} "
                    f"from {stored}.",
                    next_step="Restore a database this build can read.",
                )
            schema.migrate_schema(conn, from_version=stored)
            conn.execute(
                "UPDATE configuration_meta SET updated_at_ms = ? WHERE id = 1",
                (now_ms_utc(),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One serialized, all-or-nothing write."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, parameters).fetchall()

    def _query_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, parameters).fetchone()

    @property
    def schema_version(self) -> int:
        row = self._query_one("SELECT schema_version FROM configuration_meta WHERE id = 1")
        if row is None:
            raise ProfilesDatabaseUnavailable(
                "The broker configuration database lost its metadata row.",
                next_step="Move the file aside and let the service create a fresh database.",
            )
        return int(row["schema_version"])

    # ---- owner ----------------------------------------------------------

    def read_owner(self) -> LocalOwner | None:
        row = self._query_one(
            "SELECT owner_id, display_label, created_at_ms, updated_at_ms FROM local_owner WHERE id = 1"
        )
        return None if row is None else _owner_from_row(row)

    def insert_owner(self, conn: sqlite3.Connection, owner: LocalOwner) -> None:
        conn.execute(
            "INSERT INTO local_owner (id, owner_id, display_label, created_at_ms, updated_at_ms) "
            "VALUES (1, ?, ?, ?, ?)",
            (owner.owner_id, owner.display_label, owner.created_at_ms, owner.updated_at_ms),
        )

    def update_owner_label(
        self, conn: sqlite3.Connection, *, display_label: str, updated_at_ms: int
    ) -> None:
        conn.execute(
            "UPDATE local_owner SET display_label = ?, updated_at_ms = ? WHERE id = 1",
            (display_label, updated_at_ms),
        )

    # ---- profiles -------------------------------------------------------

    def read_profile(self, profile_id: str) -> BrokerProfile | None:
        row = self._query_one(
            "SELECT profile_id, owner_id, broker, display_name, archived, created_at_ms, "
            "updated_at_ms FROM broker_profiles WHERE profile_id = ?",
            (profile_id,),
        )
        return None if row is None else _profile_from_row(row)

    def list_profiles(self, *, include_archived: bool) -> list[BrokerProfile]:
        sql = (
            "SELECT profile_id, owner_id, broker, display_name, archived, created_at_ms, "
            "updated_at_ms FROM broker_profiles"
        )
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at_ms ASC, profile_id ASC"
        return [_profile_from_row(row) for row in self._query(sql)]

    def insert_profile(self, conn: sqlite3.Connection, profile: BrokerProfile) -> None:
        conn.execute(
            "INSERT INTO broker_profiles (profile_id, owner_id, broker, display_name, archived, "
            "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                profile.profile_id,
                profile.owner_id,
                profile.broker,
                profile.display_name,
                int(profile.archived),
                profile.created_at_ms,
                profile.updated_at_ms,
            ),
        )

    def update_profile_metadata(
        self,
        conn: sqlite3.Connection,
        *,
        profile_id: str,
        display_name: str,
        archived: bool,
        updated_at_ms: int,
    ) -> None:
        conn.execute(
            "UPDATE broker_profiles SET display_name = ?, archived = ?, updated_at_ms = ? "
            "WHERE profile_id = ?",
            (display_name, int(archived), updated_at_ms, profile_id),
        )

    # ---- revisions ------------------------------------------------------

    def read_revision(self, profile_id: str, revision: int) -> ProfileRevision | None:
        row = self._query_one(
            f"SELECT {_REVISION_COLUMNS} FROM profile_revisions WHERE profile_id = ? AND revision = ?",
            (profile_id, revision),
        )
        return None if row is None else _revision_from_row(row)

    def latest_revision(self, profile_id: str) -> ProfileRevision | None:
        row = self._query_one(
            f"SELECT {_REVISION_COLUMNS} FROM profile_revisions WHERE profile_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (profile_id,),
        )
        return None if row is None else _revision_from_row(row)

    def list_revisions(self, profile_id: str) -> list[ProfileRevision]:
        rows = self._query(
            f"SELECT {_REVISION_COLUMNS} FROM profile_revisions WHERE profile_id = ? "
            "ORDER BY revision ASC",
            (profile_id,),
        )
        return [_revision_from_row(row) for row in rows]

    def insert_revision(self, conn: sqlite3.Connection, revision: ProfileRevision) -> None:
        envelope = revision.live_envelope
        values: dict[str, Any] = {
            "profile_id": revision.profile_id,
            "revision": revision.revision,
            "schema_version": revision.schema_version,
            "credential_slot": revision.credential_slot,
            "endpoint_mode": revision.endpoint_mode,
            "account_pin": revision.account_pin,
            "account_pinned_at_ms": revision.account_pinned_at_ms,
            "content_sha256": revision.content_sha256,
            "complete": int(revision.complete),
            "author_owner_id": revision.author_owner_id,
            "created_at_ms": revision.created_at_ms,
            **{
                column: (None if envelope is None else getattr(envelope, field))
                for field, column in _ENVELOPE_COLUMNS
            },
        }
        conn.execute(
            f"INSERT INTO profile_revisions ({_REVISION_COLUMNS}) "
            f"VALUES ({_REVISION_PLACEHOLDERS})",
            tuple(values[column] for column in _REVISION_COLUMN_NAMES),
        )

    def write_account_pin(
        self,
        conn: sqlite3.Connection,
        *,
        profile_id: str,
        revision: int,
        account_id: str,
        pinned_at_ms: int,
    ) -> bool:
        """Bind an unbound revision to one observed account, once.

        Returns ``False`` when the revision was already bound — the ``WHERE``
        clause, not the caller's earlier read, is what makes the pin a
        write-once transition under a concurrent second pin.
        """
        cursor = conn.execute(
            "UPDATE profile_revisions SET account_pin = ?, account_pinned_at_ms = ? "
            "WHERE profile_id = ? AND revision = ? AND account_pin IS NULL",
            (account_id, pinned_at_ms, profile_id, revision),
        )
        return cursor.rowcount == 1

    # ---- nicknames ------------------------------------------------------

    def list_nicknames(self) -> list[AccountNickname]:
        rows = self._query(
            "SELECT account_id, nickname, updated_at_ms FROM account_nicknames ORDER BY account_id ASC"
        )
        return [
            AccountNickname(
                account_id=row["account_id"],
                nickname=row["nickname"],
                updated_at_ms=int(row["updated_at_ms"]),
            )
            for row in rows
        ]

    def upsert_nickname(self, conn: sqlite3.Connection, nickname: AccountNickname) -> None:
        conn.execute(
            "INSERT INTO account_nicknames (account_id, nickname, updated_at_ms) VALUES (?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET nickname = excluded.nickname, "
            "updated_at_ms = excluded.updated_at_ms",
            (nickname.account_id, nickname.nickname, nickname.updated_at_ms),
        )

    # ---- selection ------------------------------------------------------

    def read_selection(self) -> InstallationSelection:
        row = self._query_one(
            "SELECT staged_profile_id, staged_revision, apply_requested, apply_requested_at_ms, "
            "apply_requested_generation, selection_generation, effective_profile_id, "
            "effective_revision, effective_account_id, effective_acknowledged_at_ms, "
            "last_apply_outcome, last_apply_refusal_reason FROM installation_selection WHERE id = 1"
        )
        if row is None:
            raise ProfilesDatabaseUnavailable(
                "The broker configuration database has no installation selection row.",
                next_step="Move the file aside and let the service create a fresh database.",
            )
        return _selection_from_row(row)

    def write_selection(
        self,
        conn: sqlite3.Connection,
        selection: InstallationSelection,
        *,
        previous_generation: int,
    ) -> bool:
        """Compare-and-swap the one selection row on its generation.

        Returns ``False`` when the recorded generation is no longer the one the
        caller read. The check has to be in the ``WHERE`` clause and not only in
        the service: two writers that both read generation N would otherwise
        both write N+1, and the second would silently clobber the first — the
        exact overwrite the contract's staleness rule exists to prevent.
        """
        cursor = conn.execute(
            "UPDATE installation_selection SET staged_profile_id = ?, staged_revision = ?, "
            "apply_requested = ?, apply_requested_at_ms = ?, apply_requested_generation = ?, "
            "selection_generation = ?, effective_profile_id = ?, effective_revision = ?, "
            "effective_account_id = ?, effective_acknowledged_at_ms = ?, last_apply_outcome = ?, "
            "last_apply_refusal_reason = ? WHERE id = 1 AND selection_generation = ?",
            (
                selection.staged_profile_id,
                selection.staged_revision,
                int(selection.apply_requested),
                selection.apply_requested_at_ms,
                selection.apply_requested_generation,
                selection.selection_generation,
                selection.effective_profile_id,
                selection.effective_revision,
                selection.effective_account_id,
                selection.effective_acknowledged_at_ms,
                selection.last_apply_outcome,
                selection.last_apply_refusal_reason,
                previous_generation,
            ),
        )
        return cursor.rowcount == 1

    # ---- events ---------------------------------------------------------

    def append_event(self, conn: sqlite3.Connection, event: ConfigurationEvent) -> None:
        conn.execute(
            "INSERT INTO configuration_events (event_id, sequence, actor_owner_id, action, "
            "profile_id, revision, previous_ref, next_ref, result, recorded_at_ms) VALUES "
            "(?, (SELECT COALESCE(MAX(sequence), 0) + 1 FROM configuration_events), "
            "?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.actor_owner_id,
                event.action,
                event.profile_id,
                event.revision,
                event.previous_ref,
                event.next_ref,
                event.result,
                event.recorded_at_ms,
            ),
        )

    def list_events(self, *, limit: int, before_sequence: int | None) -> list[ConfigurationEvent]:
        sql = (
            "SELECT event_id, sequence, actor_owner_id, action, profile_id, revision, "
            "previous_ref, next_ref, result, recorded_at_ms FROM configuration_events"
        )
        parameters: tuple[Any, ...] = ()
        if before_sequence is not None:
            sql += " WHERE sequence < ?"
            parameters = (before_sequence,)
        sql += " ORDER BY sequence DESC LIMIT ?"
        parameters = (*parameters, limit)
        return [_event_from_row(row) for row in self._query(sql, parameters)]

    def event_sequence(self, event_id: str) -> int | None:
        row = self._query_one(
            "SELECT sequence FROM configuration_events WHERE event_id = ?", (event_id,)
        )
        return None if row is None else int(row["sequence"])


def _owner_from_row(row: sqlite3.Row) -> LocalOwner:
    return LocalOwner(
        owner_id=row["owner_id"],
        display_label=row["display_label"],
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _profile_from_row(row: sqlite3.Row) -> BrokerProfile:
    return BrokerProfile(
        profile_id=row["profile_id"],
        owner_id=row["owner_id"],
        broker=row["broker"],
        display_name=row["display_name"],
        archived=bool(row["archived"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


def _revision_from_row(row: sqlite3.Row) -> ProfileRevision:
    stored = {field: row[column] for field, column in _ENVELOPE_COLUMNS}
    envelope = (
        None
        if any(value is None for value in stored.values())
        # Explicit reconstruction through the validated type, never SQLite type
        # affinity: this is the read path ADR 0060 Decision 6 pins.
        else ValidatedLiveEnvelope.from_mapping(stored)
    )
    return ProfileRevision(
        profile_id=row["profile_id"],
        revision=int(row["revision"]),
        schema_version=int(row["schema_version"]),
        credential_slot=row["credential_slot"],
        endpoint_mode=row["endpoint_mode"],
        account_pin=row["account_pin"],
        account_pinned_at_ms=(
            None if row["account_pinned_at_ms"] is None else int(row["account_pinned_at_ms"])
        ),
        live_envelope=envelope,
        content_sha256=row["content_sha256"],
        complete=bool(row["complete"]),
        author_owner_id=row["author_owner_id"],
        created_at_ms=int(row["created_at_ms"]),
    )


def _selection_from_row(row: sqlite3.Row) -> InstallationSelection:
    def _optional_int(column: str) -> int | None:
        value = row[column]
        return None if value is None else int(value)

    return InstallationSelection(
        staged_profile_id=row["staged_profile_id"],
        staged_revision=_optional_int("staged_revision"),
        apply_requested=bool(row["apply_requested"]),
        apply_requested_at_ms=_optional_int("apply_requested_at_ms"),
        apply_requested_generation=_optional_int("apply_requested_generation"),
        selection_generation=int(row["selection_generation"]),
        effective_profile_id=row["effective_profile_id"],
        effective_revision=_optional_int("effective_revision"),
        effective_account_id=row["effective_account_id"],
        effective_acknowledged_at_ms=_optional_int("effective_acknowledged_at_ms"),
        last_apply_outcome=row["last_apply_outcome"],
        last_apply_refusal_reason=row["last_apply_refusal_reason"],
    )


def _event_from_row(row: sqlite3.Row) -> ConfigurationEvent:
    return ConfigurationEvent(
        event_id=row["event_id"],
        actor_owner_id=row["actor_owner_id"],
        action=row["action"],
        profile_id=row["profile_id"],
        revision=None if row["revision"] is None else int(row["revision"]),
        previous_ref=row["previous_ref"],
        next_ref=row["next_ref"],
        result=row["result"],
        recorded_at_ms=int(row["recorded_at_ms"]),
    )


__all__ = [
    "DATABASE_DIRECTORY",
    "DATABASE_FILENAME",
    "ProfilesStore",
    "new_identifier",
    "profiles_database_path",
]
