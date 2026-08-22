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

import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from app.broker.alpaca.clerk.sqlite import reads, writes
from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    AtomicDecisionReceipt,
    append_atomic_decision_receipt_row,
    append_competing_decision_receipt_row,
    append_decision_receipt_row,
    atomic_decision_receipt_conflicts_with_existing,
    update_decision_receipt_for_bar,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    active_execution_coverage_conflicts,
    execution_is_quarantined,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage_evidence import (
    unreadable_quarantine_source_ids_for_order,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionCoverageQuarantinedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
    validate_execution_corrected_facts,
    validate_execution_coverage_quarantined_facts,
    validate_execution_slice_facts,
)
from app.broker.alpaca.clerk.sqlite.folds import (
    DEFAULT_FOLD_REGISTRY,
    FoldRegistry,
)
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
    CommittedTransition,
    DecisionReceiptResource,
    EffectOperationResource,
    OperationClaim,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.repository_execution_coverage_api import (
    ClerkSqliteRepositoryExecutionCoverageApi,
)
from app.broker.alpaca.clerk.sqlite.repository_external_order_api import (
    ClerkSqliteRepositoryExternalOrderApi,
)
from app.broker.alpaca.clerk.sqlite.repository_read_api import ClerkSqliteRepositoryReadApi
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

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


class UnsupportedWalFilesystem(ClerkSqliteError):
    """The authority is on a filesystem that cannot honor SQLite WAL semantics."""


class HashChainBroken(ClerkSqliteError):
    """Startup check 8 (§9): a stored row's hash disagrees with its recomputation."""


class ExecutionLeaseHeld(ClerkSqliteError):
    """Topology fence: another live process already holds this account's lease."""


class ExecutionLeaseLost(ClerkSqliteError):
    """§9a: this handle's lease expired or was reassigned; it can no longer write."""


class RecoveryInProgress(ClerkSqliteError):
    """Startup fence: an exclusive offline recovery currently owns the account."""


class RepositoryPoisoned(ClerkSqliteError):
    """§9a: a transition committed but its mirror finalize was unconfirmed.

    No further mutation or operation claim is permitted until
    :meth:`ClerkSqliteRepository.reconcile_poison` (or a fresh ``open()``)
    re-runs the exact fence reconciliation and finds it consistent.
    """


class OperationClaimError(ClerkSqliteError):
    """Scope D: an ``effect_operations`` claim attempt found a live owner."""


class ClerkSqliteRepository(
    ClerkSqliteRepositoryReadApi,
    ClerkSqliteRepositoryExternalOrderApi,
    ClerkSqliteRepositoryExecutionCoverageApi,
):
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
        self._reconciliation_in_progress = False
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

    @property
    def lease_ttl_ms(self) -> int:
        """Return the configured execution-lease lifetime for heartbeats."""
        return self._lease_ttl_ms

    def close(self) -> None:
        """Release the execution lease and close the connection.

        Acquire the same write coordinator every mutation uses so a
        shutdown cannot close the connection out from under an in-flight
        lease heartbeat: cancelling the ``asyncio.to_thread`` renewal
        wrapper does not stop the worker thread already inside
        :meth:`renew_execution_lease`, so ``close`` waits for that worker to
        release the lock before touching the connection.
        """
        with self._write_lock:
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
        """Create initial authority or a developer-reset-authorized next generation.

        Refuses if ``clerk.db`` already exists (use :meth:`open`), or if the
        established-accounts registry already has an entry for this account
        with no matching database on disk. The only exception is a verified
        paper developer-reset authorization for that exact prior generation;
        every other missing established database remains PRD §15.4's
        fail-closed case.
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

    def renew_execution_lease(self) -> None:
        """Keep an idle live writer's lease current without mutating custody.

        The same write coordinator used by transitions prevents a heartbeat
        from sharing this connection with another mutating call. Ownership
        and expiry still use the strict compare-and-swap check above, so a
        stale handle cannot resurrect itself after missing its deadline.
        """
        with self._write_lock:
            self._renew_execution_lease()

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

    def append_transition(
        self,
        transition: TransitionInput,
        *,
        decision_receipt: AtomicDecisionReceipt | None = None,
    ) -> CommittedTransition:
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
                decision_receipt=decision_receipt,
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

    def append_execution_slice_if_absent(
        self,
        *,
        execution_id: str,
        order_ref: str,
        build_transition: Callable[[], TransitionInput],
        build_coverage_conflict: Callable[[], TransitionInput],
    ) -> str:
        """Append an exact execution or durably fence ambiguous coverage.

        The database check and canonical transition append share the repository
        write lock and execution lease.  A process-restarted trade-updates
        consumer therefore cannot add a redundant custody transition after its
        in-memory dedup map has been lost.  If aggregate cumulative-recovery
        evidence already exists for the order, its coverage cannot be matched
        safely to the later exact slice. A direct exact-to-one-cumulative
        match may be replaced automatically after the canonical set proof is
        revalidated; every other shape is withheld behind a typed uncertainty.

        Outcomes are ``"appended"``, ``"duplicate"``, ``"coverage_superseded"``,
        ``"coverage_conflict_raised"``, ``"coverage_conflict_quarantined"``,
        or ``"coverage_conflict_already_raised"``. A changed delivery that
        reuses an immutable broker execution ID cannot be a truthful new
        execution or correction (a correction needs its own broker identity),
        so it raises the same fail-closed coverage uncertainty instead of
        silently discarding changed economics.
        """
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            existing_execution = reads.effective_execution_slice(self._conn, execution_id)
            if existing_execution is not None:
                candidate = build_transition()
                facts = self._validated_execution_slice_transition(
                    candidate,
                    execution_id=execution_id,
                    order_ref=order_ref,
                )
                if self._same_execution_slice(existing_execution, facts=facts, order_ref=order_ref):
                    return "duplicate"
                if reads.correction_uncertainty_exists(self._conn, execution_id):
                    return "coverage_conflict_already_raised"
                uncertainty = build_coverage_conflict()
                self._validate_execution_coverage_conflict(
                    uncertainty=uncertainty,
                    execution_id=execution_id,
                    order_ref=order_ref,
                )
                self.append_transition(uncertainty)
                return "coverage_conflict_raised"
            if reads.execution_exists(self._conn, execution_id):
                return "duplicate"
            if reads.cumulative_recovery_fill_exists_for_order(self._conn, order_ref):
                candidate = build_transition()
                facts = self._validated_execution_slice_transition(
                    candidate,
                    execution_id=execution_id,
                    order_ref=order_ref,
                )
                supersession = self._execution_coverage_supersession_transition(
                    exact_transition=candidate,
                    facts=facts,
                )
                if supersession is not None:
                    # The plan is deliberately rebuilt before the append. A
                    # re-entrant caller or future admission hook that changed
                    # custody between proof and commit must take the existing
                    # fail-closed path, never append a stale replacement.
                    current_supersession = self._execution_coverage_supersession_transition(
                        exact_transition=candidate,
                        facts=facts,
                    )
                    if (
                        current_supersession is not None
                        and current_supersession.facts_json == supersession.facts_json
                    ):
                        self.append_transition(supersession)
                        return "coverage_superseded"
                active_conflicts = active_execution_coverage_conflicts(
                    self._conn,
                    order_ref=order_ref,
                )
                if active_conflicts:
                    if len(active_conflicts) != 1:
                        # A corrupted or otherwise ambiguous episode set has
                        # no safe ownership target for the incoming exact.
                        # Keep the account fail-closed without fabricating a
                        # quarantine attachment to whichever row sorted first.
                        return "coverage_conflict_already_raised"
                    if unreadable_quarantine_source_ids_for_order(
                        self._conn,
                        order_ref=order_ref,
                    ):
                        return "coverage_conflict_already_raised"
                    conflict = active_conflicts[0]
                    if execution_is_quarantined(
                        self._conn,
                        conflict=conflict,
                        execution_id=execution_id,
                    ):
                        return "coverage_conflict_already_raised"
                    self.append_transition(
                        self._execution_coverage_quarantine_transition(
                            exact_transition=candidate,
                            facts=facts,
                            conflict_execution_id=conflict.conflict_execution_id,
                            uncertainty=None,
                        )
                    )
                    return "coverage_conflict_quarantined"
                uncertainty = build_coverage_conflict()
                self._validate_execution_coverage_conflict(
                    uncertainty=uncertainty,
                    execution_id=execution_id,
                    order_ref=order_ref,
                )
                self.append_transition(
                    self._execution_coverage_quarantine_transition(
                        exact_transition=candidate,
                        facts=facts,
                        conflict_execution_id=facts.execution_id,
                        uncertainty=uncertainty,
                    )
                )
                return "coverage_conflict_raised"
            transition = build_transition()
            self._validated_execution_slice_transition(
                transition,
                execution_id=execution_id,
                order_ref=order_ref,
            )
            self.append_transition(transition)
            return "appended"

    def _validated_execution_slice_transition(
        self,
        transition: TransitionInput,
        *,
        execution_id: str,
        order_ref: str,
    ) -> ExecutionSliceFilledFacts:
        """Return validated exact-execution facts with immutable identities aligned."""
        if transition.transition_kind != "EXECUTION_SLICE_FILLED":
            raise ValueError("exact execution append requires EXECUTION_SLICE_FILLED")
        facts = ExecutionSliceFilledFacts.from_facts_json(transition.facts_json)
        validate_execution_slice_facts(facts)
        if facts.execution_id != execution_id:
            raise ValueError("execution_id must match the transition facts")
        if transition.order_ref != order_ref:
            raise ValueError("order_ref must match the exact execution transition")
        self._validate_order_effect_ownership(transition, order_ref=order_ref)
        return facts

    def _validate_order_effect_ownership(
        self,
        transition: TransitionInput,
        *,
        order_ref: str,
    ) -> dict:
        """Bind execution evidence to its order's immutable effect owner.

        Manual custody has no strategy instance, so an effect's subject is the
        canonical identity.  The optional strategy field remains a
        compatibility assertion for bot-owned effects only.
        """
        if transition.effect_operation_id is None or transition.command_id is None:
            raise ValueError("execution evidence requires its durable owning effect and command")
        owner = self._conn.execute(
            "SELECT command_id, subject_id, strategy_instance_id FROM effect_operations "
            "WHERE effect_operation_id = ?",
            (transition.effect_operation_id,),
        ).fetchone()
        if owner is None:
            raise ValueError("execution evidence requires an existing durable owning effect")
        if (
            transition.command_id != owner["command_id"]
            or transition.strategy_instance_id != owner["strategy_instance_id"]
        ):
            raise ValueError("execution evidence ownership does not match its durable effect")
        linked = self._conn.execute(
            "SELECT 1 FROM orders o LEFT JOIN operation_order_links l "
            "ON l.order_ref = o.order_ref AND l.effect_operation_id = ? "
            "WHERE o.order_ref = ? AND (o.effect_operation_id = ? OR l.effect_operation_id IS NOT NULL)",
            (transition.effect_operation_id, order_ref, transition.effect_operation_id),
        ).fetchone()
        if linked is None:
            raise ValueError("execution evidence effect does not own the exact order")
        return dict(owner)

    def _execution_coverage_quarantine_transition(
        self,
        *,
        exact_transition: TransitionInput,
        facts: ExecutionSliceFilledFacts,
        conflict_execution_id: str,
        uncertainty: TransitionInput | None,
    ) -> TransitionInput:
        """Build a no-economic fold carrying the exact rejected evidence."""
        if exact_transition.order_ref is None:
            raise ValueError("quarantined coverage requires an order reference")
        quarantined = ExecutionCoverageQuarantinedFacts(
            order_ref=exact_transition.order_ref,
            conflict_execution_id=conflict_execution_id,
            exact_execution=facts,
            conflicting_cumulative_fill_ids=reads.cumulative_recovery_fill_ids_for_order(
                self._conn,
                exact_transition.order_ref,
            ),
            uncertainty=(
                None
                if uncertainty is None
                else UncertaintyRaisedFacts.from_facts_json(uncertainty.facts_json)
            ),
        )
        validate_execution_coverage_quarantined_facts(quarantined)
        return TransitionInput(
            strategy_instance_id=exact_transition.strategy_instance_id,
            run_id=exact_transition.run_id,
            command_id=exact_transition.command_id,
            effect_operation_id=exact_transition.effect_operation_id,
            order_ref=exact_transition.order_ref,
            broker_order_id=exact_transition.broker_order_id,
            transition_kind="EXECUTION_COVERAGE_QUARANTINED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            proof_reference=exact_transition.proof_reference,
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=self._clock(),
            summary_code="EXECUTION_COVERAGE_QUARANTINED",
            facts_json=quarantined.to_facts_json(),
        )

    @staticmethod
    def _same_execution_slice(
        existing: dict,
        *,
        facts: ExecutionSliceFilledFacts,
        order_ref: str,
    ) -> bool:
        """Whether an execution-ID replay preserves immutable economics."""
        return (
            existing["order_ref"] == order_ref
            and existing["symbol"].upper() == facts.symbol.upper()
            and existing["side"] == facts.side
            and float(existing["qty"]) == facts.slice_qty
            and float(existing["price"]) == facts.slice_price
        )

    def _validate_execution_coverage_conflict(
        self,
        *,
        uncertainty: TransitionInput,
        execution_id: str,
        order_ref: str,
    ) -> None:
        if uncertainty.transition_kind != "UNCERTAINTY_RAISED":
            raise ValueError("coverage conflict must raise UNCERTAINTY_RAISED")
        if uncertainty.order_ref != order_ref or uncertainty.effect_operation_id is None:
            raise ValueError("coverage conflict must identify the order and owning effect")
        owner = self._conn.execute(
            "SELECT command_id, subject_id, strategy_instance_id FROM effect_operations "
            "WHERE effect_operation_id = ?",
            (uncertainty.effect_operation_id,),
        ).fetchone()
        if owner is None:
            raise ValueError("coverage conflict requires its durable owning effect")
        if (
            uncertainty.command_id != owner["command_id"]
            or uncertainty.strategy_instance_id != owner["strategy_instance_id"]
        ):
            raise ValueError("coverage conflict ownership does not match its durable effect")
        self._validate_order_effect_ownership(uncertainty, order_ref=order_ref)
        facts = UncertaintyRaisedFacts.from_facts_json(uncertainty.facts_json)
        if facts.reason_code != EXECUTION_COVERAGE_CONFLICT_REASON_CODE:
            raise ValueError("coverage conflict must use the execution coverage reason code")
        if not facts.blocks_new_exposure:
            raise ValueError("coverage conflict must block new exposure")
        if facts.allows_reduction:
            raise ValueError("coverage conflict must not authorize reduction")
        cause = ExecutionCoverageConflictCause.from_mapping(facts.cause_facts)
        if cause.order_ref != order_ref or cause.execution_id != execution_id:
            raise ValueError("coverage conflict must identify the order and exact execution")

    def append_execution_correction_or_raise(
        self,
        *,
        correction: TransitionInput,
        build_uncertainty: Callable[[str], TransitionInput],
    ) -> str:
        """Validate and append one correction, or durably raise uncertainty.

        An invalid correction must not enter the hash chain as a correction
        that did not change the economic fold.  The same atomic lock instead
        appends a typed ``UNCERTAINTY_RAISED`` transition, leaving admission
        fail-closed until the broker evidence is reconciled.
        """
        if correction.transition_kind != "EXECUTION_CORRECTED":
            raise ValueError("correction append requires EXECUTION_CORRECTED")
        facts = ExecutionCorrectedFacts.from_facts_json(correction.facts_json)
        validate_execution_corrected_facts(facts)
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            if reads.execution_exists(self._conn, facts.execution_id):
                return "duplicate"
            if reads.correction_uncertainty_exists(self._conn, facts.execution_id):
                return "duplicate"
            invalid_reason = self._execution_correction_invalid_reason(
                correction=correction,
                facts=facts,
            )
            if invalid_reason is not None:
                uncertainty = build_uncertainty(invalid_reason)
                if uncertainty.transition_kind != "UNCERTAINTY_RAISED":
                    raise ValueError("invalid correction must raise UNCERTAINTY_RAISED")
                uncertainty_facts = UncertaintyRaisedFacts.from_facts_json(uncertainty.facts_json)
                if uncertainty_facts.cause_facts.get("execution_id") != facts.execution_id:
                    raise ValueError("correction uncertainty must identify the broker execution")
                self.append_transition(uncertainty)
                return "invalid"
            self.append_transition(correction)
            return "appended"

    def _execution_correction_invalid_reason(
        self,
        *,
        correction: TransitionInput,
        facts: ExecutionCorrectedFacts,
    ) -> str | None:
        if correction.order_ref is None:
            return "correction transition is missing order identity"
        try:
            owner = self._validate_order_effect_ownership(
                correction,
                order_ref=correction.order_ref,
            )
        except ValueError as exc:
            return str(exc)
        target = reads.effective_execution_slice(self._conn, facts.superseded_execution_ref)
        if target is None:
            return (
                f"superseded execution {facts.superseded_execution_ref!r} is missing "
                "or no longer effective"
            )
        if target["order_ref"] != correction.order_ref:
            return "superseded execution belongs to a different order"
        if target["subject_id"] != owner["subject_id"]:
            return "superseded execution belongs to a different custody subject"
        if target["strategy_instance_id"] != owner["strategy_instance_id"]:
            return "superseded execution belongs to a different strategy instance"
        if not isinstance(target["symbol"], str) or not target["symbol"]:
            return "superseded execution is missing owned symbol evidence"
        if target["symbol"].upper() != facts.symbol.upper():
            return "superseded execution has a different symbol"
        if target["side"] != facts.side:
            return "superseded execution has a different side"
        return None

    def _commit_transition_row(
        self,
        *,
        sequence: int,
        prev_hash: str,
        row_hash: str,
        payload: dict,
        decision_receipt: AtomicDecisionReceipt | None = None,
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
            if decision_receipt is not None:
                strategy_instance_id = payload["strategy_instance_id"]
                run_id = payload["run_id"]
                effect_operation_id = payload["effect_operation_id"]
                if not strategy_instance_id or not run_id or not effect_operation_id:
                    raise ValueError(
                        "atomic decision capture requires strategy, run, and effect identity"
                    )
                append_atomic_decision_receipt_row(
                    self._conn,
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                    effect_operation_id=effect_operation_id,
                    order_ref=payload["order_ref"],
                    receipt=decision_receipt,
                )
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
        decision_receipt: AtomicDecisionReceipt | None = None,
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
                    if decision_receipt is not None and (
                        existing.strategy_instance_id is None
                        or atomic_decision_receipt_conflicts_with_existing(
                            self._conn,
                            strategy_instance_id=existing.strategy_instance_id,
                            run_id=existing.run_id,
                            effect_operation_id=existing.effect_operation_id,
                            receipt=decision_receipt,
                        )
                    ):
                        # R2's idempotency contract covers the command
                        # payload only -- `payload_hash` never included
                        # `decision_receipt` (open-pr-review finding: a
                        # replay with the same key/payload but different
                        # decision evidence was returning here as
                        # ``CommandExistingSame`` and silently discarding
                        # the new receipt, since this shortcut never calls
                        # ``append_transition`` and so never reaches
                        # ``append_atomic_decision_receipt_row``'s own
                        # conflict detection). Surfacing it as a durable
                        # conflict, exactly like a diverging payload_hash
                        # already does, closes that gap without widening
                        # what counts as "the same command".
                        logger.warning(
                            "commit_first_transition replay carried decision "
                            "evidence diverging from the durably captured "
                            "receipt; refusing to silently discard it",
                            extra={
                                "action": "decision_receipt_replay_conflict",
                                "command_id": command_id,
                                "idempotency_key": idempotency_key,
                                "decision_id": decision_receipt.decision_id,
                            },
                        )
                        return CommandExistingConflict(existing)
                    return CommandExistingSame(existing)
                return CommandExistingConflict(existing)

            transition = build_transition()
            committed = self.append_transition(
                transition,
                decision_receipt=decision_receipt,
            )
            command = reads.command(self._conn, command_id)
            assert command is not None, (
                f"fold for {transition.transition_kind!r} did not create "
                f"commands row {command_id!r}"
            )
            return CommandCreated(command=command, committed=committed)

    def begin_reconciliation(self) -> None:
        """Install the process-local account gate before reading broker truth."""
        with self._write_lock:
            if self._reconciliation_in_progress:
                raise ClerkSqliteError(
                    f"account {self._account_id!r} reconciliation is already in progress"
                )
            self._reconciliation_in_progress = True

    def end_reconciliation(self) -> None:
        """Release the account gate only after the final safety verdict is durable."""
        with self._write_lock:
            self._reconciliation_in_progress = False

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

    # ------------------------------------------------------------------
    # Concrete business use of the spine this slice owns
    # ------------------------------------------------------------------

    def register_strategy_instance(
        self,
        *,
        strategy_instance_id: str,
        symbol: str,
        config_hash: str,
        strategy_key: str = "repository_direct_registration",
        display_name: str = "Repository direct registration",
        config_json: str | None = None,
    ) -> CommittedTransition:
        """Insert-once bot registration — needs no command/effect lifecycle.

        The active runtime always supplies all immutable configuration fields.
        The named defaults preserve the repository's narrow fixture and
        qualification seam while avoiding a product-visible ``unknown``
        strategy key for direct registrations.
        """
        if not strategy_key:
            raise ValueError("strategy_key must be non-empty")
        if not display_name:
            raise ValueError("display_name must be non-empty")
        if config_json is None:
            config_json = canonicalize(
                {
                    "strategy_key": strategy_key,
                    "symbol": symbol,
                }
            )
        transition = TransitionInput(
            strategy_instance_id=strategy_instance_id,
            transition_kind="STRATEGY_INSTANCE_REGISTERED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=self._clock(),
            summary_code="STRATEGY_INSTANCE_REGISTERED",
            facts_json=canonicalize(
                {
                    "symbol": symbol,
                    "config_hash": config_hash,
                    "strategy_key": strategy_key,
                    "display_name": display_name,
                    "config_json": config_json,
                }
            ),
        )
        return self.append_transition(transition)

    def append_decision_receipt(
        self,
        *,
        strategy_instance_id: str,
        outcome: str,
        symbol: str | None,
        intent_id: str | None,
        order_ref: str | None,
        observed_at_ms: int,
        facts_json: str,
    ) -> DecisionReceiptResource:
        """Append one SQLite-only strategy decision receipt with the next seq.

        Decision receipts are durable product evidence, not custody
        transitions: they deliberately do not participate in the transition
        hash chain, mirror, or control revision. The repository retains the
        lease and write coordinator while the receipt store owns allocation
        and insertion.
        """
        if not strategy_instance_id:
            raise ValueError("strategy_instance_id must be non-empty")
        if not outcome:
            raise ValueError("outcome must be non-empty")
        if observed_at_ms < 0:
            raise ValueError("observed_at_ms must be non-negative")
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            return append_decision_receipt_row(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                outcome=outcome,
                symbol=symbol,
                intent_id=intent_id,
                order_ref=order_ref,
                observed_at_ms=observed_at_ms,
                facts_json=facts_json,
            )

    def update_decision_receipt_for_bar(
        self,
        *,
        strategy_instance_id: str,
        bar_ref: str,
        outcome: str,
        order_ref: str | None,
        facts_json: str,
    ) -> DecisionReceiptResource:
        """Replace one closed bar's provisional receipt with its final outcome."""
        if not strategy_instance_id:
            raise ValueError("strategy_instance_id must be non-empty")
        if not bar_ref:
            raise ValueError("bar_ref must be non-empty")
        if not outcome:
            raise ValueError("outcome must be non-empty")
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            return update_decision_receipt_for_bar(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                bar_ref=bar_ref,
                outcome=outcome,
                order_ref=order_ref,
                facts_json=facts_json,
            )

    def capture_decision_against_active_exit(
        self,
        *,
        strategy_instance_id: str,
        run_id: str | None,
        decision_receipt: AtomicDecisionReceipt,
    ) -> EffectOperationResource | None:
        """Durably capture a decision that a competing, already-active EXIT
        will answer with typed existing custody (FR-022) instead of a fresh
        effect of its own.

        Callers gate the "return existing custody" short-circuit on
        :meth:`active_exit_for_strategy`; FR-018 requires that short-circuit
        to atomically capture the decision receipt too, exactly like a
        decision that goes on to create its own effect does, so the losing
        evaluation is not silently invisible to a later causal read. The
        check and the capture share one hold of the write lock: the active
        EXIT this returns is the same one the receipt gets durably linked
        to, with nothing able to intervene between the two. A decision that
        turns out to be its own EXIT's replay (not a competing one) is still
        safe to pass through here -- the receipt insert is the same
        idempotent/conflict-detecting write every other receipt append uses,
        so it becomes a no-op rather than a duplicate row.

        Returns the active EXIT effect this decision resolved to, or
        ``None`` if no EXIT is currently active for this strategy (nothing
        to capture against; the caller should proceed with the normal
        accept path instead).
        """
        with self._write_lock:
            self._assert_not_poisoned()
            self._renew_execution_lease()
            active = reads.active_exit_for_strategy(self._conn, strategy_instance_id)
            if active is None:
                return None
            append_competing_decision_receipt_row(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                resolved_effect_operation_id=active.effect_operation_id,
                receipt=decision_receipt,
            )
            return active

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
        refresh_unchanged: bool = False,
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
            if active["facts_json"] == facts_json and not refresh_unchanged:
                return "unchanged"
            self.append_transition(
                replace(build_refresh(), proof_reference=active["uncertainty_id"])
            )
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
