"""Exceptional-operation coordination for one :class:`AccountClerk`.

The Clerk owns durable intake and the broker-write fence.  This module owns
emergency registration, cancellation, recovery, and reconciliation operations.
Keeping those state machines here makes their dependency boundary explicit
without creating an import cycle back to the Clerk façade.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_clerk_journal import (
    AccountClerkBrokerAckReceipt,
    AccountClerkCancelNamespaceReceipt,
    AccountClerkJournal,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
    read_account_clerk_journal,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
)
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    emergency_flatten_strategy_instance_id,
)

logger = logging.getLogger(__name__)

ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S = 25.0
_RECOVERY_EXPOSURE_QUANTITY_ABS_TOLERANCE = 1e-9


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
    place_under_clerk_grant: Callable[[IbkrOrderSpec, bool], Awaitable[Any]]
    retry_recorded_intent_locked: Callable[
        [AccountOwnerSubmitIntent], Awaitable[AccountClerkBrokerAckReceipt]
    ]


class AccountClerkOperationCoordinator:
    """Coordinate exceptional Clerk intake, cancellation, recovery, and retry operations."""

    def __init__(self, dependencies: AccountClerkOperationDependencies) -> None:
        self._deps = dependencies
        self.state = AccountClerkOperationState()

    @property
    def recovery_flatten_in_progress(self) -> bool:
        return self.state.recovery_flatten_namespace is not None

    async def register_emergency_flatten_intent(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkRecordedReceipt:
        """Record an external emergency order without letting the Clerk submit it."""

        deps = self._deps
        if intent.intent_kind != "EMERGENCY_FLATTEN":
            deps.reject(intent, "CLERK_EMERGENCY_FLATTEN_INTENT_KIND_REQUIRED")
        expected_strategy_instance_id = emergency_flatten_strategy_instance_id(deps.account_id)
        if intent.strategy_instance_id != expected_strategy_instance_id:
            deps.reject(intent, "CLERK_EMERGENCY_FLATTEN_INSTANCE_MISMATCH")
        expected_namespace = build_bot_order_namespace(expected_strategy_instance_id)
        if intent.bot_order_namespace != expected_namespace:
            deps.reject(intent, "CLERK_EMERGENCY_FLATTEN_NAMESPACE_MISMATCH")
        async with deps.intake_lock:
            deps.require_rpc_write_intake(intent)
            return await asyncio.to_thread(deps.record_intent_locked, intent)

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
