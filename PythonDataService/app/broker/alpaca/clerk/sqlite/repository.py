"""Event-sourced SQLite repository spine — PRD Phase 1 / issue #1375, repaired
by the corrective foundation slice.

Implements the pinned contract in
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md``: the
account-scoped ``clerk.db``, the R9 two-phase mirror fence, the fail-closed
startup checks, and a durable, renewed per-account execution lease. SQL stays
private to this storage package (principally ``reads.py``, ``writes.py``, and
``repository_lifecycle.py``; PRD §9.2) — callers never see a cursor; they call
:meth:`ClerkSqliteRepository.commit_first_transition` (or
:meth:`append_transition` for kinds with no idempotent-admission concept, e.g.
bot registration) and read back typed snapshots.

Corrective foundation slice (see ``docs/audits/open-pr-review-2026-08-05.md``
and ``docs/superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md``):
the prior ``reserve_command()`` + public ``serialized()`` design let a command
become durable as a bare ``commands`` row with no ``custody_transitions``
insert and no mirror fence — directly contradicting PRD §4 goal 3 and §9.3,
both of which require reservation, effect creation, transition, fold, and
revision advance in one SQLite transaction. Both are deleted here.
:meth:`commit_first_transition` is the one operation that replaces them: a
content-addressed lookup and (if fresh) a transition append, held under one
private write coordinator, whose fold is what creates the command (and, from
issue #1377 onward, the effect/order) — never a separate write.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from app.broker.alpaca.clerk.sqlite import reads, writes
from app.broker.alpaca.clerk.sqlite.folds import DEFAULT_FOLD_REGISTRY, FoldRegistry
from app.broker.alpaca.clerk.sqlite.hashchain import (
    GENESIS,
    canonical_payload,
    canonicalize,
    compute_row_hash,
)
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, PendingTransition
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    CommittedTransition,
    ControlMetaSnapshot,
    EffectOperationResource,
    OperationClaim,
    OrderResource,
    RunResource,
    TransitionInput,
)
from app.utils.timestamps import Clock, now_ms_utc

DB_FILENAME = "clerk.db"
MIRROR_FILENAME = "custody_transitions.mirror"
DEFAULT_LEASE_TTL_MS = 30_000
DEFAULT_CLAIM_TTL_MS = 60_000


class ClerkSqliteError(Exception):
    """Base for every fail-closed condition this repository can raise."""


class AlreadyInitialized(ClerkSqliteError):
    """``initialize()`` called against an account that already has a database."""


class DatabaseMissingAfterEstablishment(ClerkSqliteError):
    """Startup check 2 (§9): ``clerk.db`` is gone but the registry proves it existed."""


class DatabaseIdentityMismatch(ClerkSqliteError):
    """Startup check 3-4 (§9): identity token, generation, or account_id does not match."""


class SchemaVersionMismatch(ClerkSqliteError):
    """Startup check 5 (§9)."""


class IntegrityCheckFailed(ClerkSqliteError):
    """Startup check 7 (§9): ``PRAGMA integrity_check`` did not return ``ok``."""


class HashChainBroken(ClerkSqliteError):
    """Startup check 8 (§9): a stored row's hash disagrees with its recomputation."""


class ExecutionLeaseHeld(ClerkSqliteError):
    """Topology fence: another live process already holds this account's lease."""


class ExecutionLeaseLost(ClerkSqliteError):
    """§9a: this handle's lease expired or was reassigned; it can no longer write."""


class RepositoryPoisoned(ClerkSqliteError):
    """§9a: a transition committed but its mirror finalize was unconfirmed.

    No further mutation or operation claim is permitted until
    :meth:`ClerkSqliteRepository.reconcile_poison` (or a fresh ``open()``)
    re-runs the exact fence reconciliation and finds it consistent.
    """


class OperationClaimError(ClerkSqliteError):
    """Scope D: an ``effect_operations`` claim attempt found a live owner."""


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
        db_path: Path,
        mirror_path: Path,
        conn: sqlite3.Connection,
        mirror: MirrorFile,
        clock: Clock,
        fold_registry: FoldRegistry,
        lease_owner: str,
        lease_ttl_ms: int,
    ) -> None:
        self._account_id = account_id
        self._account_dir = account_dir
        self._db_path = db_path
        self._mirror_path = mirror_path
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._mirror = mirror
        self._clock = clock
        self._folds = fold_registry
        self._lease_owner = lease_owner
        self._lease_ttl_ms = lease_ttl_ms
        self._poisoned = False
        # Pinned contracts doc §2: "one application-owned write coordinator
        # ... belt-and-suspenders, not a substitute for BEGIN IMMEDIATE."
        # BEGIN IMMEDIATE's lock only protects from the point it's acquired;
        # append_transition reads next-sequence/prev_hash/authority_generation
        # before that point, so this lock is what actually closes the window
        # between that read and the transaction that depends on it — not the
        # database lock, which starts later. Reentrant (not plain Lock):
        # commit_first_transition holds it across its own lookup-then-append
        # sequence, and append_transition (called from inside that sequence)
        # takes it again from the same thread — must not deadlock.
        self._write_lock = threading.RLock()

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def lease_owner(self) -> str:
        """This process's execution-lease identity — the same owner an
        operation claim should be acquired under, since a claim is only
        meaningful as proof that *this* live process is the one about to
        contact the broker (§2's lease + claim close the same gap)."""
        return self._lease_owner

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def mirror_path(self) -> Path:
        return self._mirror_path

    @property
    def clock(self) -> Clock:
        return self._clock

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
        lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS,
        fold_registry: FoldRegistry = DEFAULT_FOLD_REGISTRY,
    ) -> ClerkSqliteRepository:
        """Create authority generation 1 for an account that has never had one.

        Refuses if ``clerk.db`` already exists (use :meth:`open`), or if the
        established-accounts registry already has an entry for this account
        with no matching database on disk — that combination is PRD §15.4's
        "removed after authority was established" case, which needs the
        explicit recovery/reset workflow (Slice 9), not a silent fresh init.
        """
        from app.broker.alpaca.clerk.sqlite.repository_lifecycle import (
            initialize_repository,
        )

        return initialize_repository(
            repository_type=cls,
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            lease_owner=lease_owner,
            lease_ttl_ms=lease_ttl_ms,
            fold_registry=fold_registry,
            db_filename=DB_FILENAME,
            mirror_filename=MIRROR_FILENAME,
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
        """Open an existing authority, running all fail-closed checks (§9)."""
        from app.broker.alpaca.clerk.sqlite.repository_lifecycle import open_repository

        return open_repository(
            repository_type=cls,
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            lease_owner=lease_owner,
            lease_ttl_ms=lease_ttl_ms,
            fold_registry=fold_registry,
            db_filename=DB_FILENAME,
            mirror_filename=MIRROR_FILENAME,
        )

    # ------------------------------------------------------------------
    # Lease renewal and poison handling (§9a)
    # ------------------------------------------------------------------

    def _renew_execution_lease(self) -> None:
        """Verify and renew before every mutation/claim — a lease acquired
        once at open and never revisited is not a live-process fence."""
        now = self._clock()
        cursor = self._conn.execute(
            "UPDATE control_meta SET execution_lease_expires_at_ms = ? "
            "WHERE id = 1 AND execution_lease_owner = ? AND execution_lease_expires_at_ms >= ?",
            (now + self._lease_ttl_ms, self._lease_owner, now),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            raise ExecutionLeaseLost(
                f"account {self._account_id!r} execution lease was lost or expired; "
                "this handle can no longer write"
            )

    def _assert_not_poisoned(self) -> None:
        if self._poisoned:
            raise RepositoryPoisoned(
                f"account {self._account_id!r} repository is poisoned after an "
                "unconfirmed mirror finalization; call reconcile_poison() or reopen "
                "before further writes"
            )

    def reconcile_poison(self) -> None:
        """Re-run the exact §9 check-9 reconciliation; clear the poison flag
        only if it finds the fence consistent."""
        with self._write_lock:
            rows = self.custody_transitions()
            self._mirror.reconcile(rows, clock=self._clock)
            self._poisoned = False

    # ------------------------------------------------------------------
    # The spine: append a transition through the R9 three-step fence
    # ------------------------------------------------------------------

    def append_transition(self, transition: TransitionInput) -> CommittedTransition:
        """Prepare (fsync mirror) → commit (SQLite txn + fold) → finalize (fsync mirror).

        Raises whatever the mirror I/O or the SQLite transaction raises; on
        any failure before the SQLite commit, no transition is recorded and
        no fold ran (R1: "a failed prepare, SQLite commit, or finalization
        produces no broker call" — this method is the fence that guarantees
        that for every caller above it). A failure *after* the SQLite commit
        but during the finalize fsync poisons this handle (§9a) rather than
        leaving it able to accept further writes unaware the fence is
        unconfirmed.
        """
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()

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
            # nothing back into SQLite (see mirror.py's module docstring). The
            # SQLite commit above already happened — a failure here poisons
            # this handle rather than silently leaving the fence unconfirmed.
            try:
                self._mirror.finalize(
                    sequence=next_sequence,
                    authority_generation=authority_generation,
                    row_hash=row_hash,
                    recorded_at_ms=self._clock(),
                )
            except Exception:
                self._poisoned = True
                raise
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
            writes.insert_custody_transition_row(
                self._conn, sequence=sequence, prev_hash=prev_hash, row_hash=row_hash, payload=payload
            )
            self._folds.apply(self._conn, payload)
            control_revision = writes.advance_control_revision(self._conn)
            writes.insert_mirror_fence_prepare_row(
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
    # Command commit — the generic R2 content-addressed primitive. This is
    # spine-level (every command of every kind commits the same way), not
    # business logic — business orchestration for a specific command (what
    # to hash, what transition to build, what admission means) lives in its
    # own domain module built on top of this and append_transition().
    # ------------------------------------------------------------------

    def commit_first_transition(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        payload_hash: str,
        build_transition: Callable[[], TransitionInput],
    ) -> CommandCreated | CommandExistingSame | CommandExistingConflict:
        """A command first becomes durable as part of a canonical custody
        transition — never a standalone reservation write.

        The content-addressed lookup and the transition append are one
        atomic operation: held under the write coordinator for its whole
        duration, so two concurrent callers with the same
        ``idempotency_key`` can never both observe "no existing command"
        and both append. R2's three-way split happens here: a fresh key
        proceeds to ``build_transition()`` (which may itself read
        additional state — e.g. "is there already an active run?" — safely,
        since the lock is already held); the same key with the same hash is
        a transport retry / genuine re-request (existing resource, no new
        effect, no error); the same key with a different hash is a durable
        conflict (no effect).

        ``build_transition`` is a small typed planner, not an arbitrary SQL
        callback — it returns a :class:`TransitionInput` whose fold is what
        actually creates the command (and, from #1377 onward, the effect
        and captured order) row.
        """
        with self._write_lock:
            # Checked here too, not only inside append_transition: an
            # existing-command retry short-circuits *before* ever calling
            # append_transition, so without this it could return a command
            # whose mirror fence is unconfirmed while the handle is poisoned
            # (open-pr-review-2026-08-05.md P2 "Block retries on poisoned
            # handles").
            self._assert_not_poisoned()
            authority_generation = self._conn.execute(
                "SELECT authority_generation FROM control_meta WHERE id = 1"
            ).fetchone()["authority_generation"]
            existing = reads.command_by_idempotency_key(
                self._conn,
                authority_generation=authority_generation,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing.payload_hash == payload_hash:
                    return CommandExistingSame(existing)
                return CommandExistingConflict(existing)

            transition = build_transition()
            committed = self.append_transition(transition)
            command = reads.command(self._conn, command_id)
            assert command is not None, (
                f"fold for {transition.transition_kind!r} did not create "
                f"commands row {command_id!r}"
            )
            return CommandCreated(command=command, committed=committed)

    def get_command(self, command_id: str) -> CommandResource | None:
        # Locked, not a bare read: the router dispatches Start/Stop/GET via
        # asyncio.to_thread, so this can genuinely run on a different OS
        # thread than an in-flight writer using the same cached connection.
        # Without the lock this can observe another transaction's
        # uncommitted rows (open-pr-review-2026-08-05.md P1 "Use a
        # committed-read path for command GETs").
        with self._write_lock:
            return reads.command(self._conn, command_id)

    def active_run(self, strategy_instance_id: str) -> RunResource | None:
        with self._write_lock:
            row = self._conn.execute(
                "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, "
                "stopped_at_ms FROM runs WHERE strategy_instance_id = ? AND state = 'ACTIVE'",
                (strategy_instance_id,),
            ).fetchone()
            return RunResource(**dict(row)) if row is not None else None

    # ------------------------------------------------------------------
    # Operation claims — CAS fencing before broker contact (Scope D). The
    # claim primitive is implemented and tested here; no broker call is
    # added in this slice — issue #1377 consumes it.
    # ------------------------------------------------------------------

    def claim_effect_operation(
        self, *, effect_operation_id: str, owner: str, ttl_ms: int = DEFAULT_CLAIM_TTL_MS
    ) -> OperationClaim:
        """Acquire an exclusive attempt-scoped claim before broker contact.

        Owner identity is diagnostic, not re-entrancy: a second coroutine in
        the same process must not renew through a live claim and duplicate an
        economic side effect.
        """
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            now = self._clock()
            token = secrets.token_hex(16)
            expires_at_ms = now + ttl_ms
            cursor = self._conn.execute(
                "UPDATE effect_operations SET claim_owner = ?, claim_token = ?, "
                "claimed_at_ms = ?, claim_expires_at_ms = ? WHERE effect_operation_id = ? "
                "AND (claim_owner IS NULL OR claim_expires_at_ms < ?)",
                (owner, token, now, expires_at_ms, effect_operation_id, now),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                if reads.effect_operation(self._conn, effect_operation_id) is None:
                    raise OperationClaimError(
                        f"effect_operation {effect_operation_id!r} does not exist"
                    )
                raise OperationClaimError(
                    f"effect_operation {effect_operation_id!r} is already claimed by a live owner"
                )
            return OperationClaim(
                effect_operation_id=effect_operation_id,
                owner=owner,
                token=token,
                claimed_at_ms=now,
                expires_at_ms=expires_at_ms,
            )

    def claim_before_broker_contact(self, effect_operation_id: str) -> OperationClaim:
        """Claim under this process's own lease identity — pinned contract §2:
        "a transactionally claimed operation work item ... acquired before any
        broker contact." A live ``BEGIN IMMEDIATE`` transaction proves
        single-writer-at-the-database; it does not prove single-*process*, so
        an event-loop stall or a slow network call between accepting an
        operation and its next broker call could otherwise let a stale owner
        contact the broker after another process has taken over. Fails
        closed: :class:`OperationClaimError` propagates and no broker call
        happens if the claim cannot be acquired — this fences a
        *different*-owner recovery sweep or successor process out entirely.

        Claims are attempt-scoped and must be released with
        :meth:`release_operation_claim` in a ``finally`` block. A live claim
        blocks every overlapping attempt, including one with the same owner.
        """
        return self.claim_effect_operation(effect_operation_id=effect_operation_id, owner=self._lease_owner)

    def release_operation_claim(self, *, effect_operation_id: str, token: str) -> bool:
        """Release exactly the attempt identified by ``token`` (CAS fenced)."""
        with self._write_lock:
            cursor = self._conn.execute(
                "UPDATE effect_operations SET claim_owner = NULL, claim_token = NULL, "
                "claimed_at_ms = NULL, claim_expires_at_ms = NULL "
                "WHERE effect_operation_id = ? AND claim_token = ?",
                (effect_operation_id, token),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def renew_operation_claim(
        self,
        *,
        effect_operation_id: str,
        token: str,
        ttl_ms: int = DEFAULT_CLAIM_TTL_MS,
    ) -> bool:
        """Extend only the same still-live attempt token (CAS fenced)."""
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            now = self._clock()
            cursor = self._conn.execute(
                "UPDATE effect_operations SET claimed_at_ms = ?, claim_expires_at_ms = ? "
                "WHERE effect_operation_id = ? AND claim_token = ? "
                "AND claim_expires_at_ms >= ?",
                (now, now + ttl_ms, effect_operation_id, token, now),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def verify_operation_claim(self, *, effect_operation_id: str, token: str) -> bool:
        """True iff ``token`` is the live (unexpired) claim on this operation —
        later operation updates must present it (Scope D)."""
        with self._write_lock:
            row = self._conn.execute(
                "SELECT claim_token, claim_expires_at_ms FROM effect_operations "
                "WHERE effect_operation_id = ?",
                (effect_operation_id,),
            ).fetchone()
            if row is None:
                return False
            return row["claim_token"] == token and row["claim_expires_at_ms"] >= self._clock()

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
    # Typed read snapshots — SQL stays private to this module and reads.py
    # ------------------------------------------------------------------

    # Every method below acquires the write coordinator too, not just
    # writes: it is the single point of synchronized access to a
    # connection shared across threads (open-pr-review-2026-08-05.md P1
    # "Use a committed-read path for command GETs"; the reasoning
    # generalizes to every public read here, not only get_command).

    def control_meta_snapshot(self) -> ControlMetaSnapshot:
        with self._write_lock:
            return reads.control_meta_snapshot(self._conn)

    def custody_transitions(self) -> list[dict]:
        with self._write_lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions ORDER BY sequence ASC"
            ).fetchall()
            return [writes.row_to_payload(row) for row in rows]

    def transitions_for_order(self, order_ref: str) -> list[dict]:
        """Every transition recorded against one order, sequence-ordered —
        e.g. #1377's ``enter.py`` uses this to find when an order first
        became uncertain, for the R4 grace-window comparison."""
        with self._write_lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions "
                "WHERE order_ref = ? ORDER BY sequence ASC",
                (order_ref,),
            ).fetchall()
            return [writes.row_to_payload(row) for row in rows]

    def has_order_transition(self, *, order_ref: str, transition_kind: str) -> bool:
        with self._write_lock:
            row = self._conn.execute(
                "SELECT 1 FROM custody_transitions WHERE order_ref = ? "
                "AND transition_kind = ? LIMIT 1",
                (order_ref, transition_kind),
            ).fetchone()
            return row is not None

    def strategy_instances(self) -> list[dict]:
        with self._write_lock:
            return reads.strategy_instances(self._conn)

    def strategy_instance(self, strategy_instance_id: str) -> dict | None:
        with self._write_lock:
            return reads.strategy_instance(self._conn, strategy_instance_id)

    def effect_operation(self, effect_operation_id: str) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.effect_operation(self._conn, effect_operation_id)

    def order(self, order_ref: str) -> OrderResource | None:
        with self._write_lock:
            return reads.order(self._conn, order_ref)

    def order_for_effect_operation(self, effect_operation_id: str) -> OrderResource | None:
        with self._write_lock:
            return reads.order_for_effect_operation(self._conn, effect_operation_id)

    def orders_for_effect_operation(self, effect_operation_id: str) -> list[OrderResource]:
        with self._write_lock:
            return reads.orders_for_effect_operation(self._conn, effect_operation_id)

    def entry_orders_for_strategy(self, strategy_instance_id: str) -> list[OrderResource]:
        with self._write_lock:
            return reads.entry_orders_for_strategy(self._conn, strategy_instance_id)

    def active_exit_for_order(self, order_ref: str) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.active_exit_for_order(self._conn, order_ref)

    def active_exit_for_strategy(
        self, strategy_instance_id: str
    ) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.active_exit_for_strategy(self._conn, strategy_instance_id)

    def reconcilable_effect_operations(self) -> list[EffectOperationResource]:
        with self._write_lock:
            return reads.reconcilable_effect_operations(self._conn)

    def position(self, strategy_instance_id: str, symbol: str) -> float:
        with self._write_lock:
            return reads.position(self._conn, strategy_instance_id, symbol)

    def fills_for_order(self, order_ref: str) -> list[dict]:
        with self._write_lock:
            return reads.fills_for_order(self._conn, order_ref)

    def uncertain_orders(self) -> list[OrderResource]:
        with self._write_lock:
            return reads.uncertain_orders(self._conn)

    def attributed_positions_by_symbol(self) -> dict[str, float]:
        with self._write_lock:
            return reads.attributed_positions_by_symbol(self._conn)

    def active_hold(self, *, scope: str, reason_code: str) -> dict | None:
        """No ``strategy_instance_id`` filter — safe today because every
        current caller hardcodes ``scope='ACCOUNT_CLERK'``
        (``reconcile._raise_account_hold``); a future ``BOT``-scoped hold
        raiser must add that filter first (see #1380's
        ``active_uncertainty``, which needs and has it), or two different
        bots sharing a ``reason_code`` would silently read each other's
        hold."""
        with self._write_lock:
            return reads.active_hold(self._conn, scope=scope, reason_code=reason_code)

    def raise_hold_if_none_active(
        self, *, scope: str, reason_code: str, build_transition: Callable[[], TransitionInput]
    ) -> bool:
        """Atomic check-then-raise for a hold (#1378): the ``active_hold``
        check and the ``ACCOUNT_HOLD_RAISED`` append happen under one
        continuous hold of the write lock, so two genuinely concurrent
        callers (an automatic sweep pass and an operator's "Reconcile now"
        landing at the same instant) can never both observe "no active
        hold" and both append one — the same class of TOCTOU gap
        :meth:`commit_first_transition` closes for commands, applied here
        for holds. Returns ``True`` if a new hold was appended, ``False`` if
        one was already ``ACTIVE`` (idempotent no-op, matching the bounded
        growth policy the pre-SQLite Alpaca clerk's ``reconcile.py`` used).
        """
        with self._write_lock:
            if reads.active_hold(self._conn, scope=scope, reason_code=reason_code) is not None:
                return False
            self.append_transition(build_transition())
            return True

    def observe_account_hold(
        self,
        *,
        reason_code: str,
        evidence_refs_json: str,
        build_raise: Callable[[], TransitionInput],
        build_refresh: Callable[[], TransitionInput],
    ) -> str:
        """Atomically raise, refresh, or leave one account-hold episode."""
        with self._write_lock:
            active = reads.active_hold(
                self._conn, scope="ACCOUNT_CLERK", reason_code=reason_code
            )
            if active is None:
                self.append_transition(build_raise())
                return "raised"
            if active["evidence_refs_json"] == evidence_refs_json:
                return "unchanged"
            self.append_transition(build_refresh())
            return "refreshed"

    def resolve_account_hold_if_active(
        self, *, reason_code: str, build_transition: Callable[[], TransitionInput]
    ) -> bool:
        """Append a typed resolution only while the hold episode is active."""
        with self._write_lock:
            if (
                reads.active_hold(
                    self._conn, scope="ACCOUNT_CLERK", reason_code=reason_code
                )
                is None
            ):
                return False
            self.append_transition(build_transition())
            return True

    def active_uncertainty(
        self, *, scope: str, reason_code: str, strategy_instance_id: str | None
    ) -> dict | None:
        with self._write_lock:
            return reads.active_uncertainty(
                self._conn,
                scope=scope,
                reason_code=reason_code,
                strategy_instance_id=strategy_instance_id,
            )

    def active_uncertainties_for_admission(self, *, strategy_instance_id: str) -> list[dict]:
        with self._write_lock:
            return reads.active_uncertainties_for_admission(
                self._conn, strategy_instance_id=strategy_instance_id
            )

    def active_holds_for_admission(self, *, strategy_instance_id: str) -> list[dict]:
        with self._write_lock:
            return reads.active_holds_for_admission(
                self._conn, strategy_instance_id=strategy_instance_id
            )

    def raise_uncertainty_if_none_active(
        self,
        *,
        scope: str,
        reason_code: str,
        strategy_instance_id: str | None,
        build_transition: Callable[[], TransitionInput],
    ) -> bool:
        """Atomic check-then-raise for an uncertainty (#1380) — the same
        check-then-append-under-one-lock shape
        :meth:`raise_hold_if_none_active` already uses for holds, applied
        here so two genuinely concurrent callers can never both observe "no
        active uncertainty" for the same ``(scope, reason_code,
        strategy_instance_id)`` and both append one. Returns ``True`` if a
        new uncertainty was appended, ``False`` if one was already
        ``ACTIVE`` (idempotent no-op — bounded growth, matching
        :meth:`raise_hold_if_none_active`'s policy).
        """
        with self._write_lock:
            if (
                reads.active_uncertainty(
                    self._conn,
                    scope=scope,
                    reason_code=reason_code,
                    strategy_instance_id=strategy_instance_id,
                )
                is not None
            ):
                return False
            self.append_transition(build_transition())
            return True

    def observe_uncertainty(
        self,
        *,
        scope: str,
        reason_code: str,
        strategy_instance_id: str | None,
        facts_json: str,
        build_raise: Callable[[], TransitionInput],
        build_refresh: Callable[[], TransitionInput],
    ) -> str:
        """Atomically raise, refresh, or leave one uncertainty episode."""
        with self._write_lock:
            active = reads.active_uncertainty(
                self._conn,
                scope=scope,
                reason_code=reason_code,
                strategy_instance_id=strategy_instance_id,
            )
            if active is None:
                self.append_transition(build_raise())
                return "raised"
            if active["facts_json"] == facts_json:
                return "unchanged"
            self.append_transition(build_refresh())
            return "refreshed"

    def resolve_uncertainty_if_active(
        self,
        *,
        scope: str,
        reason_code: str,
        strategy_instance_id: str | None,
        build_transition: Callable[[str], TransitionInput],
    ) -> bool:
        """Resolve by typed cause only; callers cannot issue a generic clear."""
        with self._write_lock:
            active = reads.active_uncertainty(
                self._conn,
                scope=scope,
                reason_code=reason_code,
                strategy_instance_id=strategy_instance_id,
            )
            if active is None:
                return False
            self.append_transition(build_transition(active["uncertainty_id"]))
            return True

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
        from app.broker.alpaca.clerk.sqlite.rebuild import (
            rebuild_repository_from_mirror,
        )

        return rebuild_repository_from_mirror(
            repository_type=cls,
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            lease_owner=lease_owner,
            fold_registry=fold_registry,
            db_filename=DB_FILENAME,
            mirror_filename=MIRROR_FILENAME,
        )
