"""Exceptional-operation coordination for one :class:`AccountClerk`.

The Clerk owns durable intake and the broker-write fence.  This module owns
emergency registration, cancellation, recovery, and reconciliation operations.
Keeping those state machines here makes their dependency boundary explicit
without creating an import cycle back to the Clerk façade.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_clerk_emergency_sequence import (
    AccountClerkEmergencySequenceError,
    emergency_operation_phases,
    pre_liquidation_transition_is_new,
    require_liquidation_sequence,
)
from app.engine.live.account_clerk_journal import (
    AccountClerkBrokerAckReceipt,
    AccountClerkCancelNamespaceReceipt,
    AccountClerkEmergencyAuthorization,
    AccountClerkEmergencyFlattenReceipt,
    AccountClerkEmergencyOperationEvent,
    AccountClerkJournal,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
    read_account_clerk_journal,
)
from app.engine.live.account_clerk_journal_models import AccountClerkPositionEvidence
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
)
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    emergency_flatten_strategy_instance_id,
    mint_intent_id,
)

logger = logging.getLogger(__name__)

ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S = 25.0
_RECOVERY_EXPOSURE_QUANTITY_ABS_TOLERANCE = 1e-9
_EMERGENCY_REOBSERVE_TIMEOUT_S = 30.0
_EMERGENCY_REOBSERVE_POLL_S = 0.25
_EMERGENCY_AUTHORIZATION_MAX_AGE_MS = 120_000


class AccountClerkGenerationFencedError(RuntimeError):
    """A Clerk observed that its durable write generation is no longer active."""

    def __init__(
        self,
        *,
        account_id: str,
        expected_generation: int,
        observed_generation: int | None,
        boundary: str,
    ) -> None:
        super().__init__("CLERK_GENERATION_STALE")
        self.account_id = account_id
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        self.boundary = boundary


class AccountClerkCancelNamespaceUncertainError(RuntimeError):
    """The Clerk cannot prove that a namespace cancellation reached terminal state."""


class AccountClerkEmergencyFlattenIncompleteError(RuntimeError):
    """The Clerk made its bounded emergency attempt but cannot prove the account flat."""


@dataclass(frozen=True)
class AccountClerkReconciliationOutcome:
    """One stable, Clerk-owned reconciliation decision."""

    intent_id: str
    order_ref: str
    verdict: Literal["RECOVER_ADOPT", "RETRY_ONCE", "HALT"]
    reason: str


@dataclass
class AccountClerkOperationState:
    """Volatile operation ownership shared with the Clerk intake façade."""

    cancel_namespace_in_progress: bool = False
    recovery_flatten_namespace: str | None = None
    emergency_flatten_operation_id: str | None = None


@dataclass(frozen=True)
class AccountClerkStartupRecoveryResult:
    """The admission consequence of a broker-evidence-only startup recovery."""

    freezes_admission: bool


@dataclass(frozen=True)
class AccountClerkDanglingOrderRecovery:
    """Whether an exact-order cure stayed flat after its cancellation boundary."""

    completed_flat: bool
    post_cancel_result: AccountClerkStartupRecoveryResult | None = None


@dataclass(frozen=True)
class AccountClerkOperationDependencies:
    """Typed boundary supplied by the Clerk's durable intake and fence layers."""

    artifacts_root: Path
    account_id: str
    broker: object | None
    journal: AccountClerkJournal
    intake_lock: asyncio.Lock
    cancel_operation_lock: asyncio.Lock
    callback_drain: Callable[[], Callable[[], Awaitable[None]] | None]
    record_intent_locked: Callable[[AccountOwnerSubmitIntent], AccountClerkRecordedReceipt]
    reject: Callable[[AccountOwnerSubmitIntent, str], NoReturn]
    require_paper_broker: Callable[[], None]
    require_rpc_write_intake: Callable[[AccountOwnerSubmitIntent], None]
    cancel_namespace_open_orders: Callable[
        [str, Callable[[], Awaitable[None]] | None], Awaitable[list[int]]
    ]
    cancel_order_refs_open_orders: Callable[
        [tuple[str, ...], Callable[[], Awaitable[None]] | None], Awaitable[list[int]]
    ]
    place_under_clerk_grant: Callable[[IbkrOrderSpec, bool], Awaitable[Any]]
    retry_recorded_intent_locked: Callable[
        [AccountOwnerSubmitIntent], Awaitable[AccountClerkBrokerAckReceipt]
    ]
    now_ms: Callable[[], int]


class AccountClerkOperationCoordinator:
    """Coordinate exceptional Clerk intake, cancellation, recovery, and retry operations."""

    def __init__(self, dependencies: AccountClerkOperationDependencies) -> None:
        self._deps = dependencies
        self.state = AccountClerkOperationState()

    @property
    def recovery_flatten_in_progress(self) -> bool:
        return self.state.recovery_flatten_namespace is not None

    @property
    def emergency_flatten_in_progress(self) -> bool:
        return self.state.emergency_flatten_operation_id is not None

    async def authorize_emergency_flatten(
        self,
        *,
        operation_id: str,
        confirmation_token: Literal["FLATTEN"],
        reconciliation_evidence_version: str,
        no_exact_recovery_candidate: Literal[True],
    ) -> AccountClerkEmergencyAuthorization:
        """Durably bind a fresh reconciliation verdict to one flatten operation.

        The HTTP layer can describe reconciliation, but only this method issues
        the receipt consumed by the broker-write path.  In particular, a host
        daemon cannot turn a bare boolean into an emergency broker write.
        """

        if not operation_id:
            raise ValueError("CLERK_EMERGENCY_OPERATION_ID_REQUIRED")
        if not reconciliation_evidence_version:
            raise ValueError("CLERK_EMERGENCY_EVIDENCE_VERSION_REQUIRED")
        now_ms = self._deps.now_ms()
        if confirmation_token != "FLATTEN" or no_exact_recovery_candidate is not True:
            raise RuntimeError("CLERK_EMERGENCY_AUTHORIZATION_SHAPE_INVALID")

        deps = self._deps
        if deps.broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        async with deps.intake_lock:
            deps.require_paper_broker()
            receipt = self._verify_emergency_reconciliation_receipt(
                reconciliation_evidence_version=reconciliation_evidence_version,
                now_ms=now_ms,
            )
            snapshot = await self._fetch_paper_positions()
            if snapshot.account_id != deps.account_id or not snapshot.is_paper:
                raise RuntimeError("CLERK_EMERGENCY_ACCOUNT_OR_PAPER_MISMATCH")
            existing = await asyncio.to_thread(deps.journal.emergency_authorization, operation_id)
            if existing is not None:
                if (
                    existing.reconciliation_evidence_version != reconciliation_evidence_version
                    or existing.evidence_observed_at_ms != receipt.account_truth_generated_at_ms
                    or existing.expires_at_ms < now_ms
                ):
                    raise RuntimeError("CLERK_EMERGENCY_OPERATION_ID_CONFLICT")
                return existing
            authorization = AccountClerkEmergencyAuthorization(
                authorization_id=hashlib.sha256(
                    (
                        f"{deps.account_id}:{operation_id}:{reconciliation_evidence_version}:"
                        f"{receipt.account_truth_generated_at_ms}"
                    ).encode()
                ).hexdigest(),
                account_id=deps.account_id,
                operation_id=operation_id,
                confirmation_token=confirmation_token,
                reconciliation_evidence_version=reconciliation_evidence_version,
                evidence_observed_at_ms=receipt.account_truth_generated_at_ms,
                expires_at_ms=min(receipt.expires_at_ms, now_ms + _EMERGENCY_AUTHORIZATION_MAX_AGE_MS),
                no_exact_recovery_candidate=True,
            )
            await asyncio.to_thread(
                deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="authorization_issued",
                    authorization=authorization,
                ),
            )
            return authorization

    def _verify_emergency_reconciliation_receipt(
        self,
        *,
        reconciliation_evidence_version: str,
        now_ms: int,
    ) -> Any:
        """Read and validate the canonical reconciliation artifact ourselves.

        The RPC caller may name a receipt but may not assert its contents.  In
        particular, the Clerk checks the exact latest durable receipt, its
        expiry, paper-account identity, and the authoritative triage decision
        that no narrower recovery move exists.  This is deliberately local to
        the Clerk process so a forged RPC payload cannot mint a broker-write
        authorization.
        """

        from app.services.account_reconciliation import AccountReconciliationService

        reconciliation = AccountReconciliationService(artifacts_root=self._deps.artifacts_root)
        receipt = reconciliation.read_latest_receipt(self._deps.account_id)
        if (
            receipt is None
            or receipt.receipt_id != reconciliation_evidence_version
            or receipt.account_id != self._deps.account_id
            or receipt.connected_account_id != self._deps.account_id
            or receipt.expires_at_ms <= now_ms
            or not receipt.account_truth.account.is_paper
        ):
            raise RuntimeError("CLERK_EMERGENCY_RECONCILIATION_EVIDENCE_INVALID")
        triage = reconciliation.triage(account_id=self._deps.account_id, now_ms=now_ms)
        if (
            triage.account_reconciliation_receipt is None
            or triage.account_reconciliation_receipt.receipt_id != receipt.receipt_id
            or triage.emergency_flatten_confirmation is None
            or triage.recovery_flatten_candidates
        ):
            raise RuntimeError("CLERK_EMERGENCY_RECONCILIATION_NOT_AUTHORIZED")
        return receipt

    async def recover_account_state(self) -> AccountClerkStartupRecoveryResult:
        """Fold interrupted emergency evidence before the Clerk admits new work.

        Recovery deliberately does not manufacture an emergency liquidation.
        An existing exact order reference is never resubmitted until an adapter
        can prove it absent.  Attributable position evidence is held for its
        bot; exposure without a unique durable namespace freezes admission.
        """

        deps = self._deps
        if deps.broker is None:
            return await self._freeze_startup_recovery("CLERK_BROKER_UNAVAILABLE")
        if not callable(getattr(deps.broker, "fetch_positions", None)):
            return await self._freeze_startup_recovery("CLERK_POSITION_SNAPSHOT_UNAVAILABLE")
        async with deps.cancel_operation_lock, deps.intake_lock:
            entries = await asyncio.to_thread(deps.journal.snapshot)
            latest_by_operation: dict[str, AccountClerkEmergencyOperationEvent] = {}
            for entry in entries:
                if entry.emergency_operation is not None:
                    latest_by_operation[entry.emergency_operation.operation_id] = entry.emergency_operation
            terminal_phases = {
                "observed_flat",
                "flag_and_hold",
                "foreign_exposure_freeze",
                "cancel_only_confirmed",
                "requires_reconciliation",
            }
            unresolved = {
                operation_id: event
                for operation_id, event in latest_by_operation.items()
                if event.phase not in terminal_phases
            }
            try:
                snapshot = await self._fetch_paper_positions()
            except Exception:
                return await self._freeze_startup_recovery("CLERK_POSITION_SNAPSHOT_UNAVAILABLE")
            if snapshot.account_id != deps.account_id or not snapshot.is_paper:
                return await self._freeze_startup_recovery("CLERK_RECOVERY_ACCOUNT_OR_PAPER_MISMATCH")
            evidence = tuple(
                AccountClerkPositionEvidence(
                    symbol=str(position.symbol),
                    signed_quantity=float(position.quantity),
                    evidence_observed_at_ms=int(snapshot.fetched_at_ms),
                )
                for position in snapshot.positions
                if float(position.quantity) != 0
            )
            # A prior explicit reconciliation requirement is a durable stop,
            # not a hint that a later generic startup scan may clear it.
            if any(event.phase == "requires_reconciliation" for event in latest_by_operation.values()):
                return AccountClerkStartupRecoveryResult(freezes_admission=True)

            if unresolved:
                return await self._fold_interrupted_emergency_operations(
                    entries=entries,
                    unresolved=unresolved,
                    positions=evidence,
                )

            if not evidence:
                return await self._recover_flat_dangling_orders(entries)

            return await self._classify_nonflat_positions(
                entries=entries,
                positions=evidence,
                operation_id="startup-recovery",
            )

    async def _freeze_startup_recovery(self, reason: str) -> AccountClerkStartupRecoveryResult:
        """Durably retain a missing/unreadable broker proof as an admission stop."""

        await asyncio.to_thread(
            self._deps.journal.append_emergency_operation,
            AccountClerkEmergencyOperationEvent(
                operation_id="startup-recovery",
                phase="requires_reconciliation",
                reason=reason,
            ),
        )
        return AccountClerkStartupRecoveryResult(freezes_admission=True)

    async def _classify_nonflat_positions(
        self,
        *,
        entries: list[Any],
        positions: tuple[AccountClerkPositionEvidence, ...],
        operation_id: str,
    ) -> AccountClerkStartupRecoveryResult:
        """Classify a fresh non-flat snapshot using broker execution proof only."""

        execution_evidence = await self._fetch_execution_evidence()
        recorded_by_ref = {
            entry.intent.order_ref: entry.intent
            for entry in entries
            if entry.entry_kind == "recorded" and entry.intent is not None
        }
        freezes_admission = False
        for position in positions:
            attribution = self._prove_position_attribution(
                position=position,
                execution_evidence=execution_evidence,
                recorded_by_ref=recorded_by_ref,
            )
            if attribution is not None:
                namespace, order_refs = attribution
                await asyncio.to_thread(
                    self._deps.journal.append_emergency_operation,
                    AccountClerkEmergencyOperationEvent(
                        operation_id=operation_id,
                        phase="flag_and_hold",
                        reason="CLERK_EXACT_BROKER_EVIDENCE_DEFERRED_RECOVERY",
                        positions=(position,),
                        bot_order_namespace=namespace,
                        recorded_order_refs=order_refs,
                    ),
                )
                continue
            freezes_admission = True
            await asyncio.to_thread(
                self._deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="foreign_exposure_freeze",
                    reason="CLERK_BROKER_EVIDENCE_ONLY_EXPOSURE",
                    positions=(position,),
                ),
            )
        return AccountClerkStartupRecoveryResult(freezes_admission=freezes_admission)

    async def _recover_flat_dangling_orders(
        self,
        entries: list[Any],
    ) -> AccountClerkStartupRecoveryResult:
        """Cure only exact Clerk-known open orders after a fresh flat snapshot."""

        list_open_orders = getattr(self._deps.broker, "list_open_orders", None)
        if not callable(list_open_orders):
            return await self._freeze_startup_recovery("CLERK_OPEN_ORDER_SNAPSHOT_UNAVAILABLE")
        try:
            open_orders = await list_open_orders()
        except Exception:
            return await self._freeze_startup_recovery("CLERK_OPEN_ORDER_SNAPSHOT_UNAVAILABLE")
        if not open_orders:
            return AccountClerkStartupRecoveryResult(freezes_admission=False)
        recorded_by_ref = {
            entry.intent.order_ref: entry.intent
            for entry in entries
            if entry.entry_kind == "recorded" and entry.intent is not None
        }
        open_refs = {
            order_ref
            for order in open_orders
            if isinstance(order_ref := getattr(order, "order_ref", None), str)
        }
        if len(open_refs) != len(open_orders) or not open_refs.issubset(recorded_by_ref):
            return await self._freeze_startup_recovery("CLERK_FLAT_FOREIGN_OR_UNPROVEN_OPEN_ORDER")
        namespaces = {recorded_by_ref[order_ref].bot_order_namespace for order_ref in open_refs}
        await asyncio.to_thread(
            self._deps.journal.append_emergency_operation,
            AccountClerkEmergencyOperationEvent(
                operation_id="startup-recovery",
                phase="cancel_only_proposed",
                bot_order_namespace=next(iter(namespaces)) if len(namespaces) == 1 else None,
                recorded_order_refs=tuple(sorted(open_refs)),
                reason="CLERK_FLAT_EXACT_OPEN_ORDER_CURE",
            ),
        )
        try:
            self._deps.require_paper_broker()
        except RuntimeError:
            return await self._freeze_startup_recovery("CLERK_RECOVERY_PAPER_MODE_REQUIRED")
        try:
            cancelled = await self._deps.cancel_order_refs_open_orders(tuple(sorted(open_refs)), None)
            remaining = await list_open_orders()
        except Exception:
            return await self._freeze_startup_recovery("CLERK_FLAT_OPEN_ORDER_CANCEL_UNPROVEN")
        remaining_refs = {getattr(order, "order_ref", None) for order in remaining}
        if remaining_refs:
            return await self._freeze_startup_recovery("CLERK_FLAT_OPEN_ORDER_CANCEL_UNPROVEN")
        try:
            post_cancel_snapshot = await self._fetch_paper_positions()
        except Exception:
            return await self._freeze_startup_recovery("CLERK_POST_CANCEL_POSITION_SNAPSHOT_UNAVAILABLE")
        if post_cancel_snapshot.account_id != self._deps.account_id or not post_cancel_snapshot.is_paper:
            return await self._freeze_startup_recovery("CLERK_POST_CANCEL_ACCOUNT_OR_PAPER_MISMATCH")
        post_cancel_positions = tuple(
            AccountClerkPositionEvidence(
                symbol=str(position.symbol),
                signed_quantity=float(position.quantity),
                evidence_observed_at_ms=int(post_cancel_snapshot.fetched_at_ms),
            )
            for position in post_cancel_snapshot.positions
            if float(position.quantity) != 0
        )
        if post_cancel_positions:
            return await self._classify_nonflat_positions(
                entries=entries,
                positions=post_cancel_positions,
                operation_id="startup-recovery",
            )
        await asyncio.to_thread(
            self._deps.journal.append_emergency_operation,
            AccountClerkEmergencyOperationEvent(
                operation_id="startup-recovery",
                phase="cancel_only_confirmed",
                bot_order_namespace=next(iter(namespaces)) if len(namespaces) == 1 else None,
                recorded_order_refs=tuple(sorted(open_refs)),
                cancelled_order_ids=tuple(sorted(cancelled)),
            ),
        )
        return AccountClerkStartupRecoveryResult(freezes_admission=False)

    async def _fold_interrupted_emergency_operations(
        self,
        *,
        entries: list[Any],
        unresolved: dict[str, AccountClerkEmergencyOperationEvent],
        positions: tuple[AccountClerkPositionEvidence, ...],
    ) -> AccountClerkStartupRecoveryResult:
        """Fold a pre-crash emergency by its original operation identity.

        This path intentionally makes no new liquidation decision.  It probes
        every durable exact reference, performs cancellation only for a fresh
        flat paper account and Clerk-known orders, then either records the
        original operation as flat or leaves its intake closed for explicit
        reconciliation.  A generic ``startup-recovery`` record must never
        erase the identity of a half-completed emergency operation.
        """

        unresolved_order_refs = {
            intent.order_ref
            for unresolved_operation_id in unresolved
            for intent in self._emergency_intents_for_operation(entries, unresolved_operation_id)
        }
        for operation_id, event in unresolved.items():
            intents = self._emergency_intents_for_operation(entries, operation_id)
            probe_statuses = await self._probe_recorded_refs(intents)
            if positions:
                await self._append_emergency_requires_reconciliation(
                    operation_id=operation_id,
                    positions=positions,
                    reason=(
                        "CLERK_EMERGENCY_RESTART_NONFLAT:"
                        f"phase={event.phase};probes={probe_statuses}"
                    ),
                    recorded_order_refs=tuple(intent.order_ref for intent in intents),
                )
                continue
            dangling_recovery = await self._recover_known_dangling_orders(
                operation_id=operation_id,
                intents=intents,
                unresolved_order_refs=unresolved_order_refs,
                entries=entries,
            )
            if dangling_recovery.post_cancel_result is not None:
                continue
            if not dangling_recovery.completed_flat:
                await self._append_emergency_requires_reconciliation(
                    operation_id=operation_id,
                    positions=(),
                    reason=(
                        "CLERK_EMERGENCY_RESTART_OPEN_ORDER_UNPROVEN:"
                        f"phase={event.phase};probes={probe_statuses}"
                    ),
                    recorded_order_refs=tuple(intent.order_ref for intent in intents),
                )
                continue
            await asyncio.to_thread(
                self._deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="observed_flat",
                    reason="CLERK_STARTUP_FRESH_FLAT",
                ),
            )
        latest = await asyncio.to_thread(self._deps.journal.snapshot)
        return AccountClerkStartupRecoveryResult(
            freezes_admission=any(
                entry.emergency_operation is not None
                and entry.emergency_operation.operation_id in unresolved
                and entry.emergency_operation.phase
                in {"requires_reconciliation", "foreign_exposure_freeze"}
                for entry in latest
            )
        )

    def _emergency_intents_for_operation(
        self,
        entries: list[Any],
        operation_id: str,
    ) -> tuple[AccountOwnerSubmitIntent, ...]:
        return tuple(
            entry.intent
            for entry in entries
            if (
                entry.entry_kind == "recorded"
                and entry.intent is not None
                and entry.intent.intent_kind == "EMERGENCY_FLATTEN"
                and entry.intent.run_id == f"clerk-emergency-{operation_id}"
            )
        )

    async def _probe_recorded_refs(
        self,
        intents: tuple[AccountOwnerSubmitIntent, ...],
    ) -> str:
        if not intents:
            return "NO_RECORDED_REFS"
        probe = getattr(self._deps.broker, "probe_intent_status", None)
        if not callable(probe):
            return "PROBE_UNAVAILABLE"
        statuses: list[str] = []
        for intent in intents:
            try:
                statuses.append(str(await probe(intent.intent_id, intent.order_ref)))
            except Exception as exc:
                statuses.append(f"PROBE_ERROR_{type(exc).__name__}")
        return ",".join(statuses)

    async def _append_emergency_requires_reconciliation(
        self,
        *,
        operation_id: str,
        positions: tuple[AccountClerkPositionEvidence, ...],
        reason: str,
        recorded_order_refs: tuple[str, ...],
    ) -> None:
        await asyncio.to_thread(
            self._deps.journal.append_emergency_operation,
            AccountClerkEmergencyOperationEvent(
                operation_id=operation_id,
                phase="requires_reconciliation",
                reason=reason,
                positions=positions,
                recorded_order_refs=recorded_order_refs,
            ),
        )

    async def _fetch_execution_evidence(self) -> tuple[Any, ...] | None:
        fetch = getattr(self._deps.broker, "fetch_execution_evidence", None)
        if not callable(fetch):
            return None
        try:
            return tuple(await fetch())
        except Exception:
            logger.warning(
                "Clerk startup could not retrieve broker execution evidence",
                exc_info=True,
                extra={"account_id": self._deps.account_id},
            )
            return None

    def _prove_position_attribution(
        self,
        *,
        position: AccountClerkPositionEvidence,
        execution_evidence: tuple[Any, ...] | None,
        recorded_by_ref: dict[str, AccountOwnerSubmitIntent],
    ) -> tuple[str, tuple[str, ...]] | None:
        """Return an owner only for a complete, exact broker execution match."""

        if execution_evidence is None:
            return None
        symbol = position.symbol.upper()
        relevant = [
            event
            for event in execution_evidence
            if (
                getattr(event, "event_type", None) == "fill"
                and str(getattr(event, "symbol", "")).upper() == symbol
                and getattr(event, "account_id", None) == self._deps.account_id
            )
        ]
        if not relevant:
            return None
        intents: list[AccountOwnerSubmitIntent] = []
        signed_quantity = 0.0
        for event in relevant:
            order_ref = getattr(event, "order_ref", None)
            exec_id = getattr(event, "exec_id", None)
            fill_quantity = getattr(event, "fill_quantity", None)
            side = getattr(event, "side", None)
            intent = recorded_by_ref.get(order_ref) if isinstance(order_ref, str) else None
            if (
                intent is None
                or not exec_id
                or not isinstance(fill_quantity, (int, float))
                or not math.isfinite(float(fill_quantity))
                or side not in {"BUY", "SELL"}
            ):
                return None
            signed_quantity += float(fill_quantity) if side == "BUY" else -float(fill_quantity)
            intents.append(intent)
        namespaces = {intent.bot_order_namespace for intent in intents}
        if (
            len(namespaces) != 1
            or not math.isclose(
                signed_quantity,
                position.signed_quantity,
                abs_tol=_RECOVERY_EXPOSURE_QUANTITY_ABS_TOLERANCE,
            )
        ):
            return None
        return next(iter(namespaces)), tuple(sorted({intent.order_ref for intent in intents}))

    async def _recover_known_dangling_orders(
        self,
        *,
        operation_id: str,
        intents: tuple[AccountOwnerSubmitIntent, ...],
        unresolved_order_refs: set[str],
        entries: list[Any],
    ) -> AccountClerkDanglingOrderRecovery:
        """Cancel only original-operation refs after a fresh flat paper snapshot."""

        list_open_orders = getattr(self._deps.broker, "list_open_orders", None)
        if not callable(list_open_orders):
            # An interrupted operation with no recorded liquidation intent may
            # still have been pre-empted by an ordinary or foreign open order.
            # Missing the broker's open-order view is never proof of flatness.
            return AccountClerkDanglingOrderRecovery(completed_flat=False)
        try:
            open_orders = await list_open_orders()
        except Exception:
            return AccountClerkDanglingOrderRecovery(completed_flat=False)
        known_refs = {intent.order_ref: intent.bot_order_namespace for intent in intents}
        foreign_or_terminal = [
            order
            for order in open_orders
            if getattr(order, "order_ref", None) not in unresolved_order_refs
        ]
        if foreign_or_terminal:
            return AccountClerkDanglingOrderRecovery(completed_flat=False)
        targeted_refs = tuple(sorted(known_refs))
        try:
            await asyncio.to_thread(
                self._deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="cancel_only_proposed",
                    bot_order_namespace=next(iter(known_refs.values())),
                    recorded_order_refs=targeted_refs,
                    reason="CLERK_ZERO_POSITION_EXACT_OPEN_ORDER",
                ),
            )
            self._deps.require_paper_broker()
            cancelled = await self._deps.cancel_order_refs_open_orders(targeted_refs, None)
            await asyncio.to_thread(
                self._deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="cancel_only_confirmed",
                    bot_order_namespace=next(iter(known_refs.values())),
                    recorded_order_refs=targeted_refs,
                    cancelled_order_ids=tuple(sorted(cancelled)),
                ),
            )
        except Exception:
            return AccountClerkDanglingOrderRecovery(completed_flat=False)
        try:
            remaining = await list_open_orders()
            if any(getattr(order, "order_ref", None) in known_refs for order in remaining) or any(
                getattr(order, "order_ref", None) not in unresolved_order_refs
                for order in remaining
            ):
                return AccountClerkDanglingOrderRecovery(completed_flat=False)
            post_cancel_snapshot = await self._fetch_paper_positions()
            if (
                post_cancel_snapshot.account_id != self._deps.account_id
                or not post_cancel_snapshot.is_paper
            ):
                return AccountClerkDanglingOrderRecovery(completed_flat=False)
            post_cancel_positions = tuple(
                AccountClerkPositionEvidence(
                    symbol=str(position.symbol),
                    signed_quantity=float(position.quantity),
                    evidence_observed_at_ms=int(post_cancel_snapshot.fetched_at_ms),
                )
                for position in post_cancel_snapshot.positions
                if float(position.quantity) != 0
            )
            if not post_cancel_positions:
                return AccountClerkDanglingOrderRecovery(completed_flat=True)
            return AccountClerkDanglingOrderRecovery(
                completed_flat=False,
                post_cancel_result=await self._classify_nonflat_positions(
                    entries=entries,
                    positions=post_cancel_positions,
                    operation_id=operation_id,
                ),
            )
        except Exception:
            return AccountClerkDanglingOrderRecovery(completed_flat=False)

    async def prepare_emergency_flatten(self, *, operation_id: str, authorization_id: str) -> None:
        """Record the intake closure before the daemon signals on-duty bots."""

        async with self._deps.intake_lock:
            await self._require_emergency_authorization(operation_id, authorization_id)
            if not await self._pre_liquidation_transition_is_new(operation_id, "intake_closed"):
                return
            await asyncio.to_thread(
                self._deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="intake_closed",
                ),
            )

    async def mark_emergency_bots_paused(self, *, operation_id: str, authorization_id: str) -> None:
        """Persist the host's proved terminal pause before any broker write."""

        async with self._deps.intake_lock:
            await self._require_emergency_authorization(operation_id, authorization_id)
            if not await self._pre_liquidation_transition_is_new(operation_id, "bots_paused"):
                return
            await asyncio.to_thread(
                self._deps.journal.append_emergency_operation,
                AccountClerkEmergencyOperationEvent(
                    operation_id=operation_id,
                    phase="bots_paused",
                ),
            )

    async def mark_emergency_requires_reconciliation(
        self,
        *,
        operation_id: str,
        reason: str,
    ) -> None:
        """Keep an intake-closed emergency visible when host pause cannot prove stop."""

        await self._append_emergency_requires_reconciliation(
            operation_id=operation_id,
            positions=(),
            reason=reason,
            recorded_order_refs=(),
        )

    async def emergency_flatten_account(
        self,
        *,
        operation_id: str,
        authorization_id: str,
    ) -> AccountClerkEmergencyFlattenReceipt:
        """Cancel Clerk-proven orders and flatten the paper account from this Clerk session.

        This is intentionally account-scoped rather than a bot recovery.  It
        never opens another broker connection: all writes pass through the
        same generation fence and callback sink as ordinary Clerk work.
        """

        deps = self._deps
        if deps.broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if not operation_id:
            raise ValueError("CLERK_EMERGENCY_OPERATION_ID_REQUIRED")
        async with deps.cancel_operation_lock:
            async with deps.intake_lock:
                if self.emergency_flatten_in_progress:
                    raise AccountClerkEmergencyFlattenIncompleteError(
                        "CLERK_EMERGENCY_FLATTEN_IN_PROGRESS"
                    )
                self.state.emergency_flatten_operation_id = operation_id
                deps.require_paper_broker()
                await self._require_emergency_authorization(operation_id, authorization_id)
                entries = await asyncio.to_thread(deps.journal.snapshot)
                self._require_liquidation_sequence(entries, operation_id=operation_id)
                namespaces = tuple(
                    sorted(
                        {
                            entry.intent.bot_order_namespace
                            for entry in entries
                            if entry.intent is not None
                        }
                    )
                )
            cancelled_order_ids: set[int] = set()
            unconfirmed_cancels: list[str] = []
            try:
                for namespace in namespaces:
                    try:
                        cancelled_order_ids.update(
                            await deps.cancel_namespace_open_orders(namespace, None)
                        )
                    except AccountClerkGenerationFencedError:
                        raise
                    except Exception as exc:
                        # Emergency flatten is deliberately the sole paper-only
                        # force path.  Preserve the uncertainty before moving
                        # to liquidation; a later Desk reconciliation sees it.
                        unconfirmed_cancels.append(f"{namespace}:{type(exc).__name__}")
                if unconfirmed_cancels:
                    async with deps.intake_lock:
                        await asyncio.to_thread(
                            deps.journal.append_emergency_operation,
                            AccountClerkEmergencyOperationEvent(
                                operation_id=operation_id,
                                phase="cancel_unconfirmed",
                                cancelled_order_ids=tuple(sorted(cancelled_order_ids)),
                                reason=";".join(unconfirmed_cancels),
                            ),
                        )

                snapshot = await self._fetch_paper_positions()
                if snapshot.account_id != deps.account_id or not snapshot.is_paper:
                    raise RuntimeError("CLERK_EMERGENCY_ACCOUNT_OR_PAPER_MISMATCH")
                broker_acks: list[AccountClerkBrokerAckReceipt] = []
                for index, position in enumerate(snapshot.positions, start=1):
                    if not math.isfinite(float(position.quantity)) or position.quantity == 0:
                        raise RuntimeError("CLERK_EMERGENCY_POSITION_INVALID")
                    existing_intent = await asyncio.to_thread(
                        self._existing_emergency_intent, operation_id, str(position.symbol)
                    )
                    intent, spec = (
                        (existing_intent, IbkrOrderSpec.model_validate(existing_intent.order_spec))
                        if existing_intent is not None
                        else self._emergency_intent_for_position(
                            operation_id=operation_id,
                            position=position,
                            index=index,
                        )
                    )
                    async with deps.intake_lock:
                        recorded = await asyncio.to_thread(deps.record_intent_locked, intent)
                        existing_ack = await asyncio.to_thread(deps.journal.ack_for_intent, intent)
                        if existing_ack is not None:
                            broker_acks.append(existing_ack)
                            continue
                        if existing_intent is not None:
                            await self._require_exact_ref_absent(intent.intent_id, intent.order_ref)
                        await asyncio.to_thread(
                            deps.journal.append_emergency_operation,
                            AccountClerkEmergencyOperationEvent(
                                operation_id=operation_id,
                                phase="liquidation_planned",
                                recorded_order_refs=(intent.order_ref,),
                            ),
                        )
                        await asyncio.to_thread(deps.journal.append_broker_submitting, intent)
                        await asyncio.to_thread(
                            deps.journal.append_emergency_operation,
                            AccountClerkEmergencyOperationEvent(
                                operation_id=operation_id,
                                phase="liquidation_submitting",
                                recorded_order_refs=(intent.order_ref,),
                            ),
                        )
                        try:
                            ack = await deps.place_under_clerk_grant(spec, True)
                        except Exception as exc:
                            await asyncio.to_thread(deps.journal.append_broker_uncertain, intent, exc)
                            raise
                        broker_acks.append(
                            await asyncio.to_thread(deps.journal.append_broker_ack, intent, ack)
                        )
                        if recorded.intent_id != intent.intent_id:
                            raise RuntimeError("CLERK_EMERGENCY_RECORDING_IDENTITY_MISMATCH")

                observed = await self._wait_for_fresh_flat()
                async with deps.intake_lock:
                    await asyncio.to_thread(
                        deps.journal.append_emergency_operation,
                        AccountClerkEmergencyOperationEvent(
                            operation_id=operation_id,
                            phase="observed_flat",
                            cancelled_order_ids=tuple(sorted(cancelled_order_ids)),
                        ),
                    )
                return AccountClerkEmergencyFlattenReceipt(
                    account_id=deps.account_id,
                    operation_id=operation_id,
                    cancelled_order_ids=tuple(sorted(cancelled_order_ids)),
                    broker_acked=tuple(broker_acks),
                    observed_at_ms=int(observed.fetched_at_ms),
                )
            except Exception as exc:
                positions = await self._safe_positions_for_receipt()
                async with deps.intake_lock:
                    await asyncio.to_thread(
                        deps.journal.append_emergency_operation,
                        AccountClerkEmergencyOperationEvent(
                            operation_id=operation_id,
                            phase="requires_reconciliation",
                            cancelled_order_ids=tuple(sorted(cancelled_order_ids)),
                            reason=f"{type(exc).__name__}: {exc}",
                            positions=positions,
                        ),
                    )
                if isinstance(exc, AccountClerkEmergencyFlattenIncompleteError):
                    raise
                raise AccountClerkEmergencyFlattenIncompleteError(
                    "CLERK_EMERGENCY_FLATTEN_REQUIRES_RECONCILIATION"
                ) from exc
            finally:
                async with deps.intake_lock:
                    if self.state.emergency_flatten_operation_id == operation_id:
                        self.state.emergency_flatten_operation_id = None

    async def _fetch_paper_positions(self) -> Any:
        fetch_positions = getattr(self._deps.broker, "fetch_positions", None)
        if not callable(fetch_positions):
            raise RuntimeError("ACCOUNT_CLERK_POSITION_SNAPSHOT_UNAVAILABLE")
        return await fetch_positions()

    async def _wait_for_fresh_flat(self) -> Any:
        deadline = asyncio.get_running_loop().time() + _EMERGENCY_REOBSERVE_TIMEOUT_S
        last_snapshot: Any | None = None
        while True:
            snapshot = await self._fetch_paper_positions()
            if snapshot.account_id != self._deps.account_id or not snapshot.is_paper:
                raise RuntimeError("CLERK_EMERGENCY_ACCOUNT_OR_PAPER_MISMATCH")
            last_snapshot = snapshot
            if not snapshot.positions:
                return snapshot
            if asyncio.get_running_loop().time() >= deadline:
                positions = ",".join(
                    f"{position.symbol}:{position.quantity}" for position in last_snapshot.positions
                )
                raise AccountClerkEmergencyFlattenIncompleteError(
                    f"CLERK_EMERGENCY_FLATTEN_NOT_FLAT:{positions}"
                )
            await asyncio.sleep(_EMERGENCY_REOBSERVE_POLL_S)

    async def _safe_positions_for_receipt(self) -> tuple[AccountClerkPositionEvidence, ...]:
        try:
            snapshot = await self._fetch_paper_positions()
        except Exception:
            return ()
        return tuple(
            AccountClerkPositionEvidence(
                symbol=str(position.symbol),
                signed_quantity=float(position.quantity),
                evidence_observed_at_ms=int(snapshot.fetched_at_ms),
            )
            for position in snapshot.positions
        )

    async def _require_emergency_authorization(self, operation_id: str, authorization_id: str) -> None:
        authorization = await asyncio.to_thread(
            self._deps.journal.emergency_authorization, operation_id
        )
        if (
            authorization is None
            or authorization.authorization_id != authorization_id
            or authorization.account_id != self._deps.account_id
            or authorization.confirmation_token != "FLATTEN"
            or authorization.no_exact_recovery_candidate is not True
            or authorization.expires_at_ms < self._deps.now_ms()
        ):
            raise RuntimeError("CLERK_EMERGENCY_AUTHORIZATION_REQUIRED")

    async def _pre_liquidation_transition_is_new(
        self,
        operation_id: str,
        next_phase: Literal["intake_closed", "bots_paused"],
    ) -> bool:
        """Require the immediately preceding durable emergency state."""

        entries = await asyncio.to_thread(self._deps.journal.snapshot)
        try:
            return pre_liquidation_transition_is_new(
                emergency_operation_phases(entries, operation_id=operation_id),
                next_phase=next_phase,
            )
        except AccountClerkEmergencySequenceError as exc:
            raise AccountClerkEmergencyFlattenIncompleteError(str(exc)) from exc

    def _require_liquidation_sequence(self, entries: list[Any], *, operation_id: str) -> None:
        """Refuse a broker write unless the account emergency fence is complete."""

        try:
            require_liquidation_sequence(emergency_operation_phases(entries, operation_id=operation_id))
        except AccountClerkEmergencySequenceError as exc:
            raise AccountClerkEmergencyFlattenIncompleteError(str(exc)) from exc

    def _existing_emergency_intent(
        self, operation_id: str, symbol: str
    ) -> AccountOwnerSubmitIntent | None:
        for entry in reversed(self._deps.journal.snapshot()):
            intent = entry.intent
            if (
                entry.entry_kind == "recorded"
                and intent is not None
                and intent.intent_kind == "EMERGENCY_FLATTEN"
                and intent.run_id == f"clerk-emergency-{operation_id}"
                and str(intent.order_spec.get("symbol", "")) == symbol
            ):
                return intent
        return None

    async def _require_exact_ref_absent(self, intent_id: str, order_ref: str) -> None:
        probe = getattr(self._deps.broker, "probe_intent_status", None)
        if not callable(probe):
            raise RuntimeError("CLERK_EMERGENCY_EXACT_REF_PROBE_UNAVAILABLE")
        status = await probe(intent_id, order_ref)
        if status != "ABSENT":
            raise AccountClerkEmergencyFlattenIncompleteError(
                "CLERK_EMERGENCY_REQUIRES_RECONCILIATION"
            )

    def _emergency_intent_for_position(
        self,
        *,
        operation_id: str,
        position: Any,
        index: int,
    ) -> tuple[AccountOwnerSubmitIntent, IbkrOrderSpec]:
        deps = self._deps
        strategy_instance_id = emergency_flatten_strategy_instance_id(deps.account_id)
        namespace = build_bot_order_namespace(strategy_instance_id)
        intent_id = mint_intent_id()
        order_ref = build_order_ref(namespace, intent_id)
        quantity = abs(float(position.quantity))
        spec = IbkrOrderSpec(
            symbol=str(position.symbol),
            sec_type=position.sec_type,
            action="SELL" if position.quantity > 0 else "BUY",
            quantity=quantity,
            order_type="MKT",
            time_in_force="DAY",
            expiry_ms=getattr(position, "expiry_ms", None),
            strike=getattr(position, "strike", None),
            right=getattr(position, "right", None),
            multiplier=getattr(position, "multiplier", 100),
            confirm_paper=True,
            client_order_id=f"clerk-emergency-{operation_id[:32]}-{index}",
            order_ref=order_ref,
        )
        return (
            AccountOwnerSubmitIntent(
                trace_id=f"clerk-emergency:{operation_id}:{index}",
                account_id=deps.account_id,
                strategy_instance_id=strategy_instance_id,
                run_id=f"clerk-emergency-{operation_id}",
                bot_order_namespace=namespace,
                intent_id=intent_id,
                order_ref=order_ref,
                intent_kind="EMERGENCY_FLATTEN",
                order_spec=spec.model_dump(mode="json"),
                owner_generation=0,
                created_at_ms=time.time_ns() // 1_000_000,
            ),
            spec,
        )

    async def submit_recovery_flatten(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None = None,
        actor_run_id: str | None = None,
        actor_bot_order_namespace: str | None = None,
    ) -> AccountClerkRecoveryFlattenReceipt:
        """Cancel this namespace's open orders then place one liquidation."""

        [receipt] = await self.submit_recovery_flatten_batch(
            (intent,),
            actor=actor,
            actor_strategy_instance_id=actor_strategy_instance_id,
            actor_run_id=actor_run_id,
            actor_bot_order_namespace=actor_bot_order_namespace,
        )
        return receipt

    async def submit_recovery_flatten_batch(
        self,
        intents: tuple[AccountOwnerSubmitIntent, ...],
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None = None,
        actor_run_id: str | None = None,
        actor_bot_order_namespace: str | None = None,
    ) -> tuple[AccountClerkRecoveryFlattenReceipt, ...]:
        """Cancel and drain one namespace before serially placing all recoveries."""

        deps = self._deps
        if deps.broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if not intents:
            raise ValueError("CLERK_RECOVERY_BATCH_EMPTY")
        first = intents[0]
        for intent in intents:
            if intent.intent_kind != "RECOVERY_FLATTEN":
                deps.reject(intent, "CLERK_RECOVERY_INTENT_KIND_REQUIRED")
            if (
                intent.account_id != first.account_id
                or intent.strategy_instance_id != first.strategy_instance_id
                or intent.run_id != first.run_id
                or intent.bot_order_namespace != first.bot_order_namespace
            ):
                deps.reject(intent, "CLERK_RECOVERY_BATCH_BINDING_MISMATCH")
            self.validate_recovery_actor(
                intent,
                actor=actor,
                actor_strategy_instance_id=actor_strategy_instance_id,
                actor_run_id=actor_run_id,
                actor_bot_order_namespace=actor_bot_order_namespace,
            )
            self.validate_recovery_order(intent)

        async with deps.cancel_operation_lock:
            async with deps.intake_lock:
                deps.require_rpc_write_intake(first)
                recorded = tuple(
                    [await asyncio.to_thread(deps.record_intent_locked, intent) for intent in intents]
                )
                existing_acks = tuple(
                    [await asyncio.to_thread(deps.journal.ack_for_intent, intent) for intent in intents]
                )
                if all(existing_acks):
                    cancelled = await asyncio.to_thread(deps.journal.recovery_cancelled_for_intent, first)
                    return tuple(
                        AccountClerkRecoveryFlattenReceipt(
                            recorded=record,
                            broker_acked=ack,
                            cancelled_order_ids=cancelled,
                        )
                        for record, ack in zip(recorded, existing_acks, strict=True)
                    )
                if any(existing_acks):
                    deps.reject(first, "CLERK_RECOVERY_BATCH_PARTIALLY_ACKNOWLEDGED")
                if self.recovery_flatten_in_progress:
                    deps.reject(first, "CLERK_RECOVERY_FLATTEN_IN_PROGRESS")
                if await asyncio.to_thread(deps.journal.recovery_operation_started_for_namespace, first):
                    deps.reject(first, "CLERK_RECOVERY_REQUIRES_OPERATOR_RECONCILIATION")
                self.state.recovery_flatten_namespace = first.bot_order_namespace
                deps.require_paper_broker()
                await asyncio.to_thread(deps.journal.append_recovery_cancelling, first)
            try:
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    cancelled = await deps.cancel_namespace_open_orders(first.bot_order_namespace, None)
                    await asyncio.to_thread(deps.journal.append_recovery_cancelled, first, cancelled)
                    if drain := deps.callback_drain():
                        await drain()
            except Exception as exc:
                async with deps.intake_lock:
                    await asyncio.to_thread(deps.journal.append_broker_uncertain, first, exc)
                    self.state.recovery_flatten_namespace = None
                raise
            try:
                async with deps.intake_lock:
                    specs = tuple(IbkrOrderSpec.model_validate(intent.order_spec) for intent in intents)
                    await asyncio.to_thread(self.validate_recovery_batch_exposure, intents, specs)
                    broker_acks: list[AccountClerkBrokerAckReceipt] = []
                    for intent, spec in zip(intents, specs, strict=True):
                        deps.require_rpc_write_intake(intent)
                        await asyncio.to_thread(deps.journal.append_broker_submitting, intent)
                        try:
                            ack = await deps.place_under_clerk_grant(spec, True)
                        except Exception as exc:
                            await asyncio.to_thread(deps.journal.append_broker_uncertain, intent, exc)
                            raise
                        broker_acks.append(
                            await asyncio.to_thread(deps.journal.append_broker_ack, intent, ack)
                        )
                    return tuple(
                        AccountClerkRecoveryFlattenReceipt(
                            recorded=record,
                            broker_acked=broker_ack,
                            cancelled_order_ids=tuple(cancelled),
                        )
                        for record, broker_ack in zip(recorded, broker_acks, strict=True)
                    )
            finally:
                async with deps.intake_lock:
                    if self.state.recovery_flatten_namespace == first.bot_order_namespace:
                        self.state.recovery_flatten_namespace = None

    async def cancel_namespace(
        self, intent: AccountOwnerSubmitIntent
    ) -> AccountClerkCancelNamespaceReceipt:
        """Durably cancel one active run's namespace through the Clerk."""

        deps = self._deps
        if deps.broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind != "CANCEL_NAMESPACE":
            deps.reject(intent, "CLERK_CANCEL_NAMESPACE_INTENT_KIND_REQUIRED")
        async with deps.cancel_operation_lock:
            try:
                async with deps.intake_lock:
                    deps.require_rpc_write_intake(intent)
                    recorded = await asyncio.to_thread(deps.record_intent_locked, intent)
                    existing = await asyncio.to_thread(deps.journal.cancel_confirmed_for_intent, intent)
                    if existing is not None:
                        return existing
                    if await asyncio.to_thread(deps.journal.cancel_submitting_for_intent, intent):
                        uncertainty = RuntimeError("prior cancel attempt may have reached broker")
                        await asyncio.to_thread(deps.journal.append_cancel_uncertain, intent, uncertainty)
                        raise AccountClerkCancelNamespaceUncertainError(
                            "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN"
                        )
                    deps.require_paper_broker()
                    self.state.cancel_namespace_in_progress = True
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    cancelled = await deps.cancel_namespace_open_orders(
                        intent.bot_order_namespace,
                        lambda: asyncio.to_thread(deps.journal.append_cancel_submitting, intent),
                    )
                    if drain := deps.callback_drain():
                        await drain()
            except AccountClerkGenerationFencedError:
                raise
            except AccountClerkCancelNamespaceUncertainError:
                raise
            except Exception as exc:
                async with deps.intake_lock:
                    await asyncio.to_thread(deps.journal.append_cancel_uncertain, intent, exc)
                raise AccountClerkCancelNamespaceUncertainError(
                    "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN"
                ) from exc
            else:
                async with deps.intake_lock:
                    receipt = await asyncio.to_thread(
                        deps.journal.append_cancel_confirmed, intent, cancelled
                    )
                    if receipt.recorded != recorded:
                        raise RuntimeError("cancel receipt recorded identity mismatch")
                    return receipt
            finally:
                self.state.cancel_namespace_in_progress = False

    async def resolve_uncertain_cancel_namespace(
        self, intent: AccountOwnerSubmitIntent
    ) -> AccountClerkCancelNamespaceReceipt:
        """Retry a fenced cancel after reconciliation observes surviving orders."""

        deps = self._deps
        if deps.broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind != "CANCEL_NAMESPACE":
            deps.reject(intent, "CLERK_CANCEL_NAMESPACE_INTENT_KIND_REQUIRED")
        async with deps.cancel_operation_lock:
            async with deps.intake_lock:
                deps.require_rpc_write_intake(intent)
                existing = await asyncio.to_thread(deps.journal.cancel_confirmed_for_intent, intent)
                if existing is not None:
                    return existing
                deps.require_paper_broker()
                self.state.cancel_namespace_in_progress = True
            try:
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    cancelled = await deps.cancel_namespace_open_orders(intent.bot_order_namespace, None)
                    if drain := deps.callback_drain():
                        await drain()
            except AccountClerkGenerationFencedError:
                raise
            except Exception as exc:
                async with deps.intake_lock:
                    await asyncio.to_thread(deps.journal.append_cancel_uncertain, intent, exc)
                raise
            else:
                async with deps.intake_lock:
                    return await asyncio.to_thread(
                        deps.journal.append_cancel_confirmed, intent, cancelled
                    )
            finally:
                self.state.cancel_namespace_in_progress = False

    async def finalize_adopted_cancel_namespace(
        self, intent: AccountOwnerSubmitIntent
    ) -> AccountClerkCancelNamespaceReceipt:
        """Persist a terminal cancel receipt after reconciliation proves no order remains."""

        deps = self._deps
        async with deps.cancel_operation_lock:
            async with deps.intake_lock:
                deps.require_rpc_write_intake(intent)
                existing = await asyncio.to_thread(deps.journal.cancel_confirmed_for_intent, intent)
                if existing is not None:
                    return existing
                self.state.cancel_namespace_in_progress = True
            try:
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    if drain := deps.callback_drain():
                        await drain()
                async with deps.intake_lock:
                    return await asyncio.to_thread(deps.journal.append_cancel_confirmed, intent, ())
            finally:
                self.state.cancel_namespace_in_progress = False

    async def reconcile_uncertain_intent(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        retry_count: int,
    ) -> AccountClerkReconciliationOutcome | None:
        """Probe, decide, record, and (only when safe) retry under one lock."""

        from app.engine.live.submit_state_machine import SubmitVerdict, next_action

        deps = self._deps
        async with deps.intake_lock:
            if self.recovery_flatten_in_progress or self._intent_is_terminal(intent):
                return None
            probe = await self._probe_intent_status(intent)
            verdict = next_action(
                current_status=_ack_failed_uncertain_status(),
                probe=probe,
                retry_count=retry_count,
            )
            reason = f"probe={probe.value}; retry_count={retry_count}"
            if intent.intent_kind == "RECOVERY_FLATTEN" and verdict is SubmitVerdict.RETRY_ONCE:
                verdict = SubmitVerdict.HALT
                reason = f"{reason}; recovery retry requires operator reconciliation"
            await asyncio.to_thread(
                deps.journal.append_reconciliation_resolution,
                intent,
                verdict=verdict.value,
                reason=reason,
            )
            if verdict is SubmitVerdict.RETRY_ONCE:
                try:
                    deps.require_rpc_write_intake(intent)
                    deps.require_paper_broker()
                    await deps.retry_recorded_intent_locked(intent)
                except AccountClerkGenerationFencedError:
                    raise
                except Exception as exc:
                    reason = f"{reason}; retry raised {type(exc).__name__}: {exc}"
            return AccountClerkReconciliationOutcome(
                intent_id=intent.intent_id,
                order_ref=intent.order_ref,
                verdict=verdict.value,
                reason=reason,
            )

    def validate_recovery_binding(
        self, intent: AccountOwnerSubmitIntent, binding: AccountInstanceBinding
    ) -> None:
        deps = self._deps
        if binding.account_id != intent.account_id:
            deps.reject(intent, "CLERK_ACCOUNT_MISMATCH")
        if binding.run_id != intent.run_id:
            deps.reject(intent, "CLERK_STALE_RUN")
        if binding.bot_order_namespace != intent.bot_order_namespace:
            deps.reject(intent, "CLERK_NAMESPACE_MISMATCH")
        if binding.lifecycle_state not in ("ACTIVE", "RETIRED"):
            deps.reject(intent, "CLERK_INACTIVE_BINDING")

    def validate_recovery_actor(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None,
        actor_run_id: str | None,
        actor_bot_order_namespace: str | None,
    ) -> None:
        deps = self._deps
        binding = latest_account_instance_binding(
            read_account_instance_registry(deps.artifacts_root, deps.account_id),
            account_id=deps.account_id,
            strategy_instance_id=intent.strategy_instance_id,
        )
        if binding is None:
            deps.reject(intent, "CLERK_UNKNOWN_INSTANCE")
        assert binding is not None
        self.validate_recovery_binding(intent, binding)
        if actor == "operator":
            if binding.lifecycle_state != "RETIRED":
                deps.reject(intent, "CLERK_OPERATOR_RECOVERY_REQUIRES_RETIRED_BINDING")
            return
        if (
            actor_strategy_instance_id != intent.strategy_instance_id
            or actor_run_id != intent.run_id
            or actor_bot_order_namespace != intent.bot_order_namespace
        ):
            deps.reject(intent, "CLERK_RECOVERY_ACTOR_MISMATCH")

    def validate_recovery_order(self, intent: AccountOwnerSubmitIntent) -> IbkrOrderSpec:
        try:
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
        except ValidationError as exc:
            self._deps.reject(intent, f"CLERK_INVALID_RECOVERY_ORDER:{exc}")
        if spec.order_ref != intent.order_ref or spec.order_type != "MKT" or not spec.confirm_paper:
            self._deps.reject(intent, "CLERK_INVALID_RECOVERY_ORDER")
        return spec

    def validate_recovery_batch_exposure(
        self,
        intents: tuple[AccountOwnerSubmitIntent, ...],
        specs: tuple[IbkrOrderSpec, ...],
    ) -> None:
        """Require the batch to cover exactly the post-cancel journal fold."""

        from app.engine.live.journal_exposure import project_journal_exposure

        deps = self._deps
        first = intents[0]
        expected_symbols = {
            row.symbol
            for row in project_journal_exposure(
                read_account_clerk_journal(deps.artifacts_root, deps.account_id),
                account_id=deps.account_id,
                group_by="namespace",
            )
            if row.group_id == first.bot_order_namespace and row.quantity != 0
        }
        submitted_symbols = {spec.symbol.upper() for spec in specs}
        for intent, spec in zip(intents, specs, strict=True):
            self._validate_recovery_exposure(intent, spec)
        if len(submitted_symbols) != len(specs) or submitted_symbols != expected_symbols:
            deps.reject(first, "CLERK_RECOVERY_BATCH_EXPOSURE_MISMATCH")

    def _validate_recovery_exposure(
        self,
        intent: AccountOwnerSubmitIntent,
        spec: IbkrOrderSpec,
    ) -> None:
        from app.engine.live.journal_exposure import project_journal_exposure

        deps = self._deps
        exposures = project_journal_exposure(
            read_account_clerk_journal(deps.artifacts_root, deps.account_id),
            account_id=deps.account_id,
            group_by="namespace",
        )
        exposure = next(
            (
                row
                for row in exposures
                if row.group_id == intent.bot_order_namespace and row.symbol == spec.symbol.upper()
            ),
            None,
        )
        if exposure is None:
            self._deps.reject(intent, "CLERK_RECOVERY_SYMBOL_NOT_OWNED")
        assert exposure is not None
        expected_action = "SELL" if exposure.quantity > 0 else "BUY"
        if spec.action != expected_action:
            self._deps.reject(intent, "CLERK_RECOVERY_DIRECTION_MISMATCH")
        if not math.isclose(
            float(spec.quantity),
            abs(exposure.quantity),
            rel_tol=0.0,
            abs_tol=_RECOVERY_EXPOSURE_QUANTITY_ABS_TOLERANCE,
        ):
            self._deps.reject(intent, "CLERK_RECOVERY_QUANTITY_MISMATCH")

    def _intent_is_terminal(self, intent: AccountOwnerSubmitIntent) -> bool:
        for entry in self._deps.journal.snapshot():
            if entry.intent != intent:
                continue
            if entry.entry_kind == "broker_acked":
                return True
            if entry.entry_kind == "reconciliation" and entry.reconciliation_verdict in {
                "RECOVER_ADOPT",
                "HALT",
            }:
                return True
        return False

    async def _probe_intent_status(self, intent: AccountOwnerSubmitIntent) -> Any:
        from app.engine.live.submit_state_machine import BrokerProbe

        probe_fn = getattr(self._deps.broker, "probe_intent_status", None)
        if not callable(probe_fn):
            return BrokerProbe.NOT_PROVABLE
        try:
            return BrokerProbe(await probe_fn(intent.intent_id, intent.order_ref))
        except Exception:
            logger.exception(
                "Account Clerk reconciliation probe failed",
                extra={"account_id": self._deps.account_id, "intent_id": intent.intent_id},
            )
            return BrokerProbe.NOT_PROVABLE


def _ack_failed_uncertain_status() -> Any:
    """Avoid importing the intent event graph during module import."""

    from app.engine.live.intent_events import IntentEventType

    return IntentEventType.ACK_FAILED_UNCERTAIN
