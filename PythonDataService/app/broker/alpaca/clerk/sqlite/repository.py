"""Event-sourced SQLite repository spine — PRD Phase 1 / issue #1375.

Implements the pinned contract in
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md``: the
account-scoped ``clerk.db``, the R9 two-phase mirror fence, the nine
fail-closed startup checks, and a durable per-account execution lease. SQL
stays private to this module (PRD §9.2) — callers never see a cursor; they
call :meth:`ClerkSqliteRepository.append_transition` and read back typed
snapshots.

This slice deliberately does not implement command/effect/broker-order
business logic (#1376 onward) — it proves the spine: append a transition,
fold it, mirror it, recover it from a corrupt database. It also does not
implement the full topology fence (scheduler/stream-consumer/reconciler
dedup) beyond the execution-lease primitive in ``control_meta`` — those
registrants don't exist as concepts until later slices; the lease itself is
in place for them to use.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.folds import DEFAULT_FOLD_REGISTRY, FoldRegistry
from app.broker.alpaca.clerk.sqlite.hashchain import (
    GENESIS,
    canonical_payload,
    canonicalize,
    compute_row_hash,
    verify_chain,
)
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, PendingTransition
from app.broker.alpaca.clerk.sqlite.registry import (
    EstablishedAccountsRegistry,
    EstablishedGeneration,
)
from app.broker.alpaca.paths import (
    fsync_directory_chain,
    resolve_contained_path,
    safe_path_component,
)
from app.utils.timestamps import Clock, now_ms_utc

DB_FILENAME = "clerk.db"
MIRROR_FILENAME = "custody_transitions.mirror"
DEFAULT_LEASE_TTL_MS = 30_000

_TRANSITION_COLUMNS: tuple[str, ...] = (
    "sequence",
    "prev_hash",
    "row_hash",
    "authority_generation",
    "strategy_instance_id",
    "run_id",
    "command_id",
    "effect_operation_id",
    "order_ref",
    "broker_order_id",
    "transition_kind",
    "custody_owner",
    "execution_authority",
    "operation_state",
    "broker_state",
    "proof_reference",
    "source_event_at_ms",
    "clerk_observed_at_ms",
    "recorded_at_ms",
    "summary_code",
    "facts_schema_version",
    "facts_json",
)


class ClerkSqliteError(Exception):
    """Base for every fail-closed condition this repository can raise."""


class AlreadyInitialized(ClerkSqliteError):
    """``initialize()`` called against an account that already has a database."""


class DatabaseMissingAfterEstablishment(ClerkSqliteError):
    """Startup check 2 (§9): ``clerk.db`` is gone but the registry proves it existed."""


class DatabaseIdentityMismatch(ClerkSqliteError):
    """Startup checks 3-4 (§9): identity token or ``account_id`` does not match."""


class SchemaVersionMismatch(ClerkSqliteError):
    """Startup check 5 (§9)."""


class IntegrityCheckFailed(ClerkSqliteError):
    """Startup check 7 (§9): ``PRAGMA integrity_check`` did not return ``ok``."""


class HashChainBroken(ClerkSqliteError):
    """Startup check 8 (§9): a stored row's hash disagrees with its recomputation."""


class ExecutionLeaseHeld(ClerkSqliteError):
    """Topology fence: another live process already holds this account's lease."""


@dataclass(frozen=True)
class TransitionInput:
    """Everything a caller supplies for one ``custody_transitions`` append.

    ``sequence``, ``prev_hash``, ``row_hash``, and ``recorded_at_ms`` are
    computed by the repository, never the caller.
    """

    transition_kind: str
    custody_owner: str
    execution_authority: str
    operation_state: str
    clerk_observed_at_ms: int
    summary_code: str
    facts_json: str
    strategy_instance_id: str | None = None
    run_id: str | None = None
    command_id: str | None = None
    effect_operation_id: str | None = None
    order_ref: str | None = None
    broker_order_id: str | None = None
    broker_state: str | None = None
    proof_reference: str | None = None
    source_event_at_ms: int | None = None
    facts_schema_version: int = 1


@dataclass(frozen=True)
class CommittedTransition:
    sequence: int
    row_hash: str
    prev_hash: str
    control_revision: int


@dataclass(frozen=True)
class ControlMetaSnapshot:
    schema_version: int
    account_id: str
    db_identity_token: str
    authority_generation: int
    control_revision: int
    created_at_ms: int
    last_open_at_ms: int


def _account_paths(artifacts_root: Path, account_id: str) -> tuple[Path, Path]:
    """Return ``(accounts_root, account_dir)``, both containment-checked."""
    safe_account_id = safe_path_component(account_id, "account_id")
    accounts_root = resolve_contained_path(artifacts_root, "accounts", "alpaca")
    account_dir = resolve_contained_path(artifacts_root, "accounts", "alpaca", safe_account_id)
    return accounts_root, account_dir


def _row_to_payload(row: sqlite3.Row) -> dict:
    return {column: row[column] for column in _TRANSITION_COLUMNS}


_NON_KEY_TRANSITION_COLUMNS = tuple(
    c for c in _TRANSITION_COLUMNS if c not in ("sequence", "prev_hash", "row_hash")
)


def _insert_custody_transition_row(
    conn: sqlite3.Connection, *, sequence: int, prev_hash: str, row_hash: str, payload: dict
) -> None:
    """The one INSERT that writes a ``custody_transitions`` row.

    Shared by the live-append commit path and mirror-rebuild replay so the
    column list and value order are defined exactly once.
    """
    conn.execute(
        f"INSERT INTO custody_transitions (sequence, prev_hash, row_hash, "
        f"{', '.join(_NON_KEY_TRANSITION_COLUMNS)}) "
        f"VALUES (?, ?, ?, {', '.join('?' for _ in _NON_KEY_TRANSITION_COLUMNS)})",
        (sequence, prev_hash, row_hash, *(payload[c] for c in _NON_KEY_TRANSITION_COLUMNS)),
    )


def _advance_control_revision(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE control_meta SET control_revision = control_revision + 1 WHERE id = 1")
    return conn.execute("SELECT control_revision FROM control_meta WHERE id = 1").fetchone()[
        "control_revision"
    ]


def _insert_mirror_fence_prepare_row(
    conn: sqlite3.Connection,
    *,
    sequence: int,
    row_hash: str,
    authority_generation: int,
    recorded_at_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO mirror_fence (sequence, phase, row_hash, authority_generation, recorded_at_ms) "
        "VALUES (?, 'PREPARE', ?, ?, ?)",
        (sequence, row_hash, authority_generation, recorded_at_ms),
    )


class ClerkSqliteRepository:
    """One Alpaca account's event-sourced SQLite authority.

    Construct via :meth:`initialize` (a brand-new generation) or
    :meth:`open` (an existing one) — never call ``__init__`` directly, both
    classmethods run the checks the pinned contract requires before handing
    back a usable instance.
    """

    def __init__(
        self,
        *,
        account_id: str,
        account_dir: Path,
        conn: sqlite3.Connection,
        mirror: MirrorFile,
        clock: Clock,
        fold_registry: FoldRegistry,
    ) -> None:
        self._account_id = account_id
        self._account_dir = account_dir
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._mirror = mirror
        self._clock = clock
        self._folds = fold_registry
        # Pinned contracts doc §2: "one application-owned write coordinator
        # ... belt-and-suspenders, not a substitute for BEGIN IMMEDIATE."
        # BEGIN IMMEDIATE's lock only protects from the point it's acquired;
        # append_transition reads next-sequence/prev_hash/authority_generation
        # before that point, so this lock is what actually closes the window
        # between that read and the transaction that depends on it — not the
        # database lock, which starts later.
        self._write_lock = threading.Lock()

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def db_path(self) -> Path:
        return self._account_dir / DB_FILENAME

    @property
    def mirror_path(self) -> Path:
        return self._account_dir / MIRROR_FILENAME

    def close(self) -> None:
        """Release the execution lease and close the connection."""
        self._conn.execute(
            "UPDATE control_meta SET execution_lease_owner = NULL, "
            "execution_lease_expires_at_ms = NULL WHERE id = 1"
        )
        self._conn.commit()
        self._conn.close()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def initialize(
        cls,
        *,
        account_id: str,
        artifacts_root: Path,
        clock: Clock = now_ms_utc,
        lease_owner: str | None = None,
        fold_registry: FoldRegistry = DEFAULT_FOLD_REGISTRY,
    ) -> ClerkSqliteRepository:
        """Create authority generation 1 for an account that has never had one.

        Refuses if ``clerk.db`` already exists (use :meth:`open`), or if the
        established-accounts registry already has an entry for this account
        with no matching database on disk — that combination is PRD §15.4's
        "removed after authority was established" case, which needs the
        explicit recovery/reset workflow (Slice 9), not a silent fresh init.
        """
        accounts_root, account_dir = _account_paths(artifacts_root, account_id)
        registry = EstablishedAccountsRegistry(accounts_root)
        db_path = account_dir / DB_FILENAME

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

        conn = sqlite3.connect(db_path, isolation_level=None)
        schema.configure_connection(conn)
        schema.apply_schema(conn)

        now = clock()
        db_identity_token = secrets.token_hex(16)
        owner = lease_owner or f"pid:{os.getpid()}"

        # Registry write happens *before* the DB commit, not after: the
        # registry is what lets a future open() tell "removed after
        # establishment" apart from "never established" (§1a). If this
        # ordering were reversed and the process died between the DB commit
        # and the registry write, a fully-usable clerk.db would exist with
        # no registry record of it — exactly the silent-recreate failure the
        # registry exists to prevent, just deferred one step. Writing the
        # registry first means any interruption in this window instead fails
        # toward "registry says established, DB incomplete/missing", which
        # every startup check in §9 already treats as fail-closed.
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
                    now + DEFAULT_LEASE_TTL_MS,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise

        return cls(
            account_id=account_id,
            account_dir=account_dir,
            conn=conn,
            mirror=MirrorFile(account_dir / MIRROR_FILENAME),
            clock=clock,
            fold_registry=fold_registry,
        )

    @classmethod
    def open(
        cls,
        *,
        account_id: str,
        artifacts_root: Path,
        clock: Clock = now_ms_utc,
        lease_owner: str | None = None,
        lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS,
        fold_registry: FoldRegistry = DEFAULT_FOLD_REGISTRY,
    ) -> ClerkSqliteRepository:
        """Open an existing authority, running all nine fail-closed checks (§9)."""
        # Check 1: path confinement.
        accounts_root, account_dir = _account_paths(artifacts_root, account_id)
        db_path = account_dir / DB_FILENAME

        # Check 2: missing-database vs. established-accounts registry.
        registry = EstablishedAccountsRegistry(accounts_root)
        if not db_path.is_file():
            if registry.is_established(account_id):
                raise DatabaseMissingAfterEstablishment(
                    f"account {account_id!r} has an established authority but "
                    f"{db_path} is missing; run the recovery/reset workflow"
                )
            raise FileNotFoundError(f"{db_path} does not exist; use initialize()")

        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            # Corruption severe enough to break basic statement execution
            # (not just an explicit integrity_check) surfaces here as a raw
            # sqlite3.DatabaseError from *any* statement below, including the
            # PRAGMAs. Translate all of it to the same typed, fail-closed
            # exception check 7 raises for the milder case — the caller
            # should not have to catch a driver-internal exception type to
            # get the "this database is corrupt" outcome.
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

        # Check 3: database identity token matches the registry's record of it.
        latest = registry.latest(account_id)
        if latest is not None and latest.db_identity_token != control_row["db_identity_token"]:
            conn.close()
            raise DatabaseIdentityMismatch(
                f"{db_path} identity token disagrees with the established-accounts "
                f"registry for {account_id!r} — possible file substitution"
            )

        # Check 4: account identity embedded in the database matches the request.
        if control_row["account_id"] != account_id:
            conn.close()
            raise DatabaseIdentityMismatch(
                f"{db_path} embeds account_id {control_row['account_id']!r}, "
                f"requested {account_id!r}"
            )

        # Check 5: schema version.
        if control_row["schema_version"] != schema.SCHEMA_VERSION:
            conn.close()
            raise SchemaVersionMismatch(
                f"{db_path} schema_version={control_row['schema_version']}, "
                f"expected {schema.SCHEMA_VERSION}"
            )

        # Check 6: authority generation is read (above) and carried for this session —
        # no further action; it is not re-validated against an external source here.

        try:
            # Check 7: PRAGMA integrity_check.
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise IntegrityCheckFailed(f"{db_path} integrity_check returned {integrity!r}")

            # Check 8: hash-chain verification.
            transition_rows = conn.execute(
                f"SELECT {', '.join(_TRANSITION_COLUMNS)} FROM custody_transitions ORDER BY sequence ASC"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise IntegrityCheckFailed(f"{db_path} is corrupt: {exc}") from exc
        except IntegrityCheckFailed:
            conn.close()
            raise
        chain_payloads = [_row_to_payload(row) for row in transition_rows]
        bad_sequence = verify_chain(chain_payloads)
        if bad_sequence is not None:
            conn.close()
            raise HashChainBroken(f"{db_path} hash-chain mismatch at sequence {bad_sequence}")

        # Check 9: mirror reconciliation — finalize a commit that never got its
        # external fsync (crash between §4 steps 2 and 3; the DB is intact, so
        # this is the one case startup completes the fence instead of failing).
        mirror = MirrorFile(account_dir / MIRROR_FILENAME)
        if chain_payloads:
            last = chain_payloads[-1]
            if not mirror.has_finalize(last["sequence"]):
                mirror.finalize(
                    sequence=last["sequence"],
                    authority_generation=last["authority_generation"],
                    row_hash=last["row_hash"],
                    recorded_at_ms=clock(),
                )

        owner = lease_owner or f"pid:{os.getpid()}"
        now = clock()
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

        return cls(
            account_id=account_id,
            account_dir=account_dir,
            conn=conn,
            mirror=mirror,
            clock=clock,
            fold_registry=fold_registry,
        )

    # ------------------------------------------------------------------
    # The spine: append a transition through the R9 three-step fence
    # ------------------------------------------------------------------

    def append_transition(self, transition: TransitionInput) -> CommittedTransition:
        """Prepare (fsync mirror) → commit (SQLite txn + fold) → finalize (fsync mirror).

        Raises whatever the mirror I/O or the SQLite transaction raises; on
        any failure before the SQLite commit, no transition is recorded and
        no fold ran (R1: "a failed prepare, SQLite commit, or finalization
        produces no broker call" — this method is the fence that guarantees
        that for every caller above it).

        The whole method runs under the process-local write coordinator
        (§2: "belt-and-suspenders, not a substitute for BEGIN IMMEDIATE").
        The next-sequence/prev_hash/authority_generation reads below happen
        *before* BEGIN IMMEDIATE opens (inside ``_commit_transition_row``),
        so the database lock alone does not protect them — this lock does.
        """
        with self._write_lock:
            last = self._conn.execute(
                "SELECT sequence, row_hash FROM custody_transitions ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            next_sequence = (last["sequence"] if last else 0) + 1
            prev_hash = last["row_hash"] if last else GENESIS
            authority_generation = self._conn.execute(
                "SELECT authority_generation FROM control_meta WHERE id = 1"
            ).fetchone()["authority_generation"]
            recorded_at_ms = self._clock()

            payload = {
                "authority_generation": authority_generation,
                "strategy_instance_id": transition.strategy_instance_id,
                "run_id": transition.run_id,
                "command_id": transition.command_id,
                "effect_operation_id": transition.effect_operation_id,
                "order_ref": transition.order_ref,
                "broker_order_id": transition.broker_order_id,
                "transition_kind": transition.transition_kind,
                "custody_owner": transition.custody_owner,
                "execution_authority": transition.execution_authority,
                "operation_state": transition.operation_state,
                "broker_state": transition.broker_state,
                "proof_reference": transition.proof_reference,
                "source_event_at_ms": transition.source_event_at_ms,
                "clerk_observed_at_ms": transition.clerk_observed_at_ms,
                "recorded_at_ms": recorded_at_ms,
                "summary_code": transition.summary_code,
                "facts_schema_version": transition.facts_schema_version,
                "facts_json": transition.facts_json,
            }
            payload_canonical = canonical_payload(payload)
            row_hash = compute_row_hash(prev_hash, payload_canonical)

            # Step 1 — PREPARE: fsync the mirror line before the SQLite txn opens.
            self._mirror.prepare(
                PendingTransition(
                    sequence=next_sequence,
                    authority_generation=authority_generation,
                    row_hash=row_hash,
                    prev_hash=prev_hash,
                    payload_canonical=payload_canonical,
                    recorded_at_ms=recorded_at_ms,
                )
            )

            # Step 2 — COMMIT: one synchronous=FULL SQLite transaction.
            control_revision = self._commit_transition_row(
                sequence=next_sequence,
                prev_hash=prev_hash,
                row_hash=row_hash,
                payload=payload,
            )

            # Step 3 — FINALIZE: fsync the matching external mirror line. Writes
            # nothing back into SQLite (see mirror.py's module docstring).
            self._mirror.finalize(
                sequence=next_sequence,
                authority_generation=authority_generation,
                row_hash=row_hash,
                recorded_at_ms=self._clock(),
            )
            return CommittedTransition(
                sequence=next_sequence,
                row_hash=row_hash,
                prev_hash=prev_hash,
                control_revision=control_revision,
            )

    def _commit_transition_row(
        self, *, sequence: int, prev_hash: str, row_hash: str, payload: dict
    ) -> int:
        """Insert order matches the pinned §4 transaction matrix literally:
        transition insert -> fold -> revision advance -> mirror_fence insert.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            _insert_custody_transition_row(
                self._conn, sequence=sequence, prev_hash=prev_hash, row_hash=row_hash, payload=payload
            )
            self._folds.apply(self._conn, payload)
            control_revision = _advance_control_revision(self._conn)
            _insert_mirror_fence_prepare_row(
                self._conn,
                sequence=sequence,
                row_hash=row_hash,
                authority_generation=payload["authority_generation"],
                recorded_at_ms=payload["recorded_at_ms"],
            )
            self._conn.commit()
            return control_revision
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Concrete business use of the spine this slice owns
    # ------------------------------------------------------------------

    def register_strategy_instance(
        self, *, strategy_instance_id: str, symbol: str, config_hash: str
    ) -> CommittedTransition:
        """Insert-once bot registration — needs no command/effect lifecycle."""
        transition = TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="STRATEGY_INSTANCE_REGISTERED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=self._clock(),
            summary_code="STRATEGY_INSTANCE_REGISTERED",
            facts_json=canonicalize({"symbol": symbol, "config_hash": config_hash}),
        )
        return self.append_transition(transition)

    # ------------------------------------------------------------------
    # Typed read snapshots — SQL stays private to this module
    # ------------------------------------------------------------------

    def control_meta_snapshot(self) -> ControlMetaSnapshot:
        row = self._conn.execute(
            "SELECT schema_version, account_id, db_identity_token, authority_generation, "
            "control_revision, created_at_ms, last_open_at_ms FROM control_meta WHERE id = 1"
        ).fetchone()
        return ControlMetaSnapshot(**dict(row))

    def custody_transitions(self) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_TRANSITION_COLUMNS)} FROM custody_transitions ORDER BY sequence ASC"
        ).fetchall()
        return [_row_to_payload(row) for row in rows]

    def strategy_instances(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms "
            "FROM strategy_instances ORDER BY created_at_ms ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Disaster recovery
    # ------------------------------------------------------------------

    @classmethod
    def rebuild_from_mirror(
        cls,
        *,
        account_id: str,
        artifacts_root: Path,
        clock: Clock = now_ms_utc,
        lease_owner: str | None = None,
        fold_registry: FoldRegistry = DEFAULT_FOLD_REGISTRY,
    ) -> ClerkSqliteRepository:
        """Rebuild a corrupt/missing ``clerk.db`` from the finalized mirror sequence.

        The corrupt database (if present) is preserved for diagnosis, never
        overwritten in place — this writes a fresh database at the same path
        only after the caller has moved the old one aside (matching PRD §13:
        "the DB is preserved for diagnosis, never overwritten"). Callers own
        that move; this method assumes ``db_path`` does not exist yet.
        """
        accounts_root, account_dir = _account_paths(artifacts_root, account_id)
        db_path = account_dir / DB_FILENAME
        if db_path.is_file():
            raise AlreadyInitialized(
                f"{db_path} exists — move it aside before rebuild_from_mirror()"
            )

        mirror = MirrorFile(account_dir / MIRROR_FILENAME)
        rebuilt_rows = mirror.rebuild()  # raises MirrorChainBroken fail-closed

        registry = EstablishedAccountsRegistry(accounts_root)
        established = registry.latest(account_id)
        if established is None:
            raise DatabaseMissingAfterEstablishment(
                f"no established-accounts registry entry for {account_id!r}; "
                "cannot rebuild an authority that was never established"
            )

        account_dir.mkdir(parents=True, exist_ok=True)
        fsync_directory_chain(account_dir, artifacts_root)
        conn = sqlite3.connect(db_path, isolation_level=None)
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
            # Same order as _commit_transition_row: transition insert -> fold
            # -> revision advance -> mirror_fence insert (pinned §4).
            for row in rebuilt_rows:
                payload = dict(row.payload)
                _insert_custody_transition_row(
                    conn,
                    sequence=row.sequence,
                    prev_hash=row.prev_hash,
                    row_hash=row.row_hash,
                    payload=payload,
                )
                fold_registry.apply(conn, payload)
                _advance_control_revision(conn)
                _insert_mirror_fence_prepare_row(
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

        return cls.open(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            lease_owner=lease_owner,
            fold_registry=fold_registry,
        )
