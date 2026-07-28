"""Deterministic, paper-only adapters for custody qualification exercises.

These adapters deliberately speak to the real ``AccountClerk`` boundary.  They
replace only IB Gateway transport and wall-clock time so the campaign can prove
durable custody behaviour without a connected gateway.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec, IbkrPosition, IbkrPositionsSnapshot
from app.engine.live.account_artifacts import advance_account_clerk_generation
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkAsyncCustodyBoundaryHooks,
    AccountClerkIntentRejected,
    read_account_clerk_journal,
)
from app.engine.live.account_custody_topology import (
    SUPPORTED_ACCOUNT_CUSTODY_FLEET_SIZE,
    supported_account_custody_config,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.order_identity import build_order_ref


@dataclass(frozen=True)
class DeterministicFaultControls:
    """Independently controlled delays and callback delivery for one campaign."""

    queue_delay_ms: int = 40
    fsync_delay_ms: int = 25
    qualification_delay_ms: int = 60
    broker_write_delay_ms: int = 80
    broker_ack_delay_ms: int = 30
    callback_delay_ms: int = 20
    epoch_recovery_delay_ms: int = 240
    action_to_effect_delay_ms: int = 140

    def __post_init__(self) -> None:
        if any(value < 0 for value in self._delays()):
            raise ValueError("deterministic fault delays must be nonnegative")

    def _delays(self) -> tuple[int, ...]:
        return (
            self.queue_delay_ms,
            self.fsync_delay_ms,
            self.qualification_delay_ms,
            self.broker_write_delay_ms,
            self.broker_ack_delay_ms,
            self.callback_delay_ms,
            self.epoch_recovery_delay_ms,
            self.action_to_effect_delay_ms,
        )


@dataclass
class DeterministicClock:
    """A controlled monotonic UTC-ms clock supplied only to test seams."""

    value_ms: int

    def now_ms(self) -> int:
        return self.value_ms

    def advance(self, delay_ms: int) -> None:
        self.value_ms += delay_ms


class DeterministicPaperBroker:
    """Paper-mode callback transport for the real Clerk qualification seam."""

    def __init__(self, *, account_id: str, clock: DeterministicClock, controls: DeterministicFaultControls) -> None:
        self._client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))
        self._account_id = account_id
        self._clock = clock
        self._controls = controls
        self.calls: list[IbkrOrderSpec] = []
        self.positions: list[IbkrPosition] = []
        self.callback_handler = None
        self.socket_loss_after_a1 = False
        self.emit_fill_before_ack = False
        self.emit_duplicate_callback = False

    async def place_order(self, spec: IbkrOrderSpec) -> object:
        """Exercise production A1/callback/ack paths without opening IBKR."""

        self.calls.append(spec)
        self._clock.advance(self._controls.broker_write_delay_ms)
        if self.socket_loss_after_a1:
            raise ConnectionError("deterministic socket loss after A1")
        if self.emit_fill_before_ack:
            if self.callback_handler is None:
                raise RuntimeError("deterministic callback handler was not installed")
            self._clock.advance(self._controls.callback_delay_ms)
            callback = IbkrOrderEvent(
                account_id=self._account_id,
                order_id=101,
                event_type="fill",
                status="Filled",
                order_ref=spec.order_ref,
                symbol=spec.symbol,
                side=spec.action,
                fill_quantity=spec.quantity,
                exec_id=f"qualification:{spec.order_ref}",
                exec_time_ms=self._clock.now_ms(),
                ts_ms=self._clock.now_ms(),
            )
            await self.callback_handler(callback)
            if self.emit_duplicate_callback:
                await self.callback_handler(callback)
        self._clock.advance(self._controls.broker_ack_delay_ms)
        return SimpleNamespace(order_id=101, perm_id=201, exec_id=f"qualification:{spec.order_ref}")

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        return IbkrPositionsSnapshot(
            account_id=self._account_id,
            is_paper=True,
            positions=self.positions,
            fetched_at_ms=self._clock.now_ms(),
        )

    async def fetch_execution_evidence(self) -> list[object]:
        return []

    async def list_open_orders(self) -> list[object]:
        return []

    async def list_completed_orders(self) -> list[object]:
        return []

    async def cancel_open_orders_for_namespace(self, _namespace: str) -> list[int]:
        return []

    async def cancel_open_orders_for_refs(self, _order_refs: tuple[str, ...]) -> list[int]:
        return []

    async def probe_intent_status(self, _intent_id: str, _order_ref: str) -> str:
        return "NOT_PROVABLE"


def bind_qualification_bot(artifacts_root: Path, account_id: str, bot_id: str, recorded_at_ms: int) -> None:
    """Register one actual Clerk-owned bot binding for an ephemeral campaign."""

    write_account_instance_binding(
        artifacts_root,
        AccountInstanceBinding(
            account_id=account_id,
            strategy_instance_id=bot_id,
            run_id=f"qualification-{bot_id}",
            bot_order_namespace=bot_order_namespace_for_instance(bot_id),
            lifecycle_state="ACTIVE",
            recorded_at_ms=recorded_at_ms,
            source="account_custody_qualification",
        ),
    )


def qualification_intent(account_id: str, bot_id: str, intent_id: str, created_at_ms: int) -> AccountOwnerSubmitIntent:
    """Build one validated paper intent for the real Clerk acceptance seam."""

    namespace = bot_order_namespace_for_instance(bot_id)
    order_ref = build_order_ref(namespace, intent_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"qualification:{intent_id}",
        account_id=account_id,
        strategy_instance_id=bot_id,
        run_id=f"qualification-{bot_id}",
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=order_ref,
        intent_kind="STRATEGY",
        order_spec=IbkrOrderSpec(
            symbol="SPY",
            sec_type="STK",
            action="BUY",
            quantity=1,
            order_type="MKT",
            time_in_force="DAY",
            confirm_paper=True,
            client_order_id=f"qualification:{intent_id}",
            order_ref=order_ref,
        ).model_dump(),
        owner_generation=1,
        created_at_ms=created_at_ms,
    )


@dataclass(frozen=True)
class RealClerkTrace:
    """Durable facts captured from one ephemeral real-Clerk exercise."""

    receipts: tuple[str, ...]
    custody_stage: str
    lifecycle_state: str
    broker_call_count: int
    clerk_request_received_at_ms: int
    clerk_intake_admitted_at_ms: int
    inbox_fsynced_at_ms: int


@dataclass(frozen=True)
class RealFleetTrace:
    """Eight Clerk-owned submissions exercised under the deployment topology."""

    receipts: tuple[str, ...]
    queue_wait_samples_ms: tuple[int, ...]
    fsync_samples_ms: tuple[int, ...]
    entry_broker_call_count: int
    queue_high_water: int
    queue_refusal_count: int


def controlled_clerk_boundary_hooks(
    *,
    clock: DeterministicClock,
    controls: DeterministicFaultControls,
    before_a1_dispatch: Callable[[AccountOwnerSubmitIntent, Literal["entry", "risk_reducing"]], None] | None = None,
) -> AccountClerkAsyncCustodyBoundaryHooks:
    """Put each campaign delay at its actual Clerk durability boundary."""

    async def before_a0_admission(_intent: AccountOwnerSubmitIntent) -> None:
        clock.advance(controls.queue_delay_ms)

    def after_inbox_durable_append(_intent: AccountOwnerSubmitIntent) -> None:
        clock.advance(controls.fsync_delay_ms)

    async def before_a1(intent: AccountOwnerSubmitIntent, lane: Literal["entry", "risk_reducing"]) -> None:
        if before_a1_dispatch is not None:
            before_a1_dispatch(intent, lane)
        clock.advance(controls.qualification_delay_ms)

    return AccountClerkAsyncCustodyBoundaryHooks(
        before_a0_admission=before_a0_admission,
        before_a1_dispatch=before_a1,
        after_inbox_durable_append=after_inbox_durable_append,
    )


def journal_receipts(artifacts_root: Path, account_id: str) -> tuple[str, ...]:
    """Return opaque, sequence-preserving receipt references from the journal."""

    return tuple(
        f"{entry.seq}:{entry.entry_kind}:{entry.recorded_at_ms}"
        for entry in read_account_clerk_journal(artifacts_root, account_id)
    )


async def real_clerk_trace(
    *,
    controls: DeterministicFaultControls,
    emit_fill_before_ack: bool,
    emit_duplicate_callback: bool,
    socket_loss_after_a1: bool,
    expected_lifecycle_state: str,
) -> RealClerkTrace:
    """Run an actual Clerk fault boundary with controlled paper transport."""

    account_id = "DUQUALIFICATION"
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-qualification-") as temporary_root:
        artifacts_root = Path(temporary_root)
        bind_qualification_bot(artifacts_root, account_id, "bot-1", clock.now_ms())
        generation = advance_account_clerk_generation(
            artifacts_root,
            account_id,
            phase="accepting",
            recorded_at_ms=clock.now_ms(),
            source="account_custody_qualification",
        ).generation
        broker = DeterministicPaperBroker(account_id=account_id, clock=clock, controls=controls)
        broker.emit_fill_before_ack = emit_fill_before_ack
        broker.emit_duplicate_callback = emit_duplicate_callback
        broker.socket_loss_after_a1 = socket_loss_after_a1
        clerk = AccountClerk(
            artifacts_root=artifacts_root,
            account_id=account_id,
            broker=broker,
            clerk_generation=generation,
            now_ms=clock.now_ms,
            async_custody_config=supported_account_custody_config(),
            async_custody_boundary_hooks=controlled_clerk_boundary_hooks(clock=clock, controls=controls),
        )
        broker.callback_handler = clerk.record_broker_event
        intent = qualification_intent(account_id, "bot-1", "fill-before-ack", clock.now_ms())
        request_at_ms = clock.now_ms()
        await clerk.start_async_custody()
        try:
            await clerk.submit_async_custody(intent, clerk_request_received_at_ms=request_at_ms)
            status = None
            entries = ()
            for _ in range(200):
                status = await clerk.async_custody_status(intent)
                entries = read_account_clerk_journal(artifacts_root, account_id)
                if (
                    status is not None
                    and status.lifecycle_state == expected_lifecycle_state
                    and (
                        expected_lifecycle_state != "economic_terminal"
                        or any(entry.entry_kind == "broker_acked" for entry in entries)
                    )
                ):
                    break
                await asyncio.sleep(0.01)
            if status is None:
                raise AssertionError("real Clerk did not expose a custody status")
            recorded = next(entry for entry in entries if entry.entry_kind == "recorded")
            if (
                recorded.clerk_request_received_at_ms is None
                or recorded.clerk_intake_admitted_at_ms is None
                or recorded.inbox_fsynced_at_ms is None
            ):
                raise AssertionError("qualification A0 receipt is missing a boundary timestamp")
            return RealClerkTrace(
                receipts=journal_receipts(artifacts_root, account_id),
                custody_stage=status.custody_stage,
                lifecycle_state=status.lifecycle_state,
                broker_call_count=len(broker.calls),
                clerk_request_received_at_ms=recorded.clerk_request_received_at_ms,
                clerk_intake_admitted_at_ms=recorded.clerk_intake_admitted_at_ms,
                inbox_fsynced_at_ms=recorded.inbox_fsynced_at_ms,
            )
        finally:
            await clerk.stop_async_custody()


async def real_eight_bot_trace(controls: DeterministicFaultControls) -> RealFleetTrace:
    """Exercise all supported bot bindings through deployed Clerk configuration."""

    account_id = "DUQUALIFICATIONFLEET"
    clock = DeterministicClock(1_784_950_000_000)
    with tempfile.TemporaryDirectory(prefix="account-custody-fleet-") as temporary_root:
        artifacts_root = Path(temporary_root)
        bot_ids = tuple(f"bot-{index}" for index in range(1, SUPPORTED_ACCOUNT_CUSTODY_FLEET_SIZE + 1))
        for bot_id in (*bot_ids, "bot-overflow"):
            bind_qualification_bot(artifacts_root, account_id, bot_id, clock.now_ms())
        generation = advance_account_clerk_generation(
            artifacts_root,
            account_id,
            phase="accepting",
            recorded_at_ms=clock.now_ms(),
            source="account_custody_qualification",
        ).generation
        broker = DeterministicPaperBroker(account_id=account_id, clock=clock, controls=controls)
        broker.socket_loss_after_a1 = True
        clerk = AccountClerk(
            artifacts_root=artifacts_root,
            account_id=account_id,
            broker=broker,
            clerk_generation=generation,
            now_ms=clock.now_ms,
            async_custody_config=supported_account_custody_config(),
            async_custody_boundary_hooks=controlled_clerk_boundary_hooks(clock=clock, controls=controls),
        )
        broker.callback_handler = clerk.record_broker_event
        requests: dict[str, int] = {}
        await clerk.start_async_custody()
        try:
            for index, bot_id in enumerate(bot_ids, start=1):
                intent = qualification_intent(account_id, bot_id, f"fleet-{index}", clock.now_ms())
                requests[intent.intent_id] = clock.now_ms()
                await clerk.submit_async_custody(intent, clerk_request_received_at_ms=requests[intent.intent_id])
                for _ in range(200):
                    status = await clerk.async_custody_status(intent)
                    if status is not None and status.lifecycle_state == "uncertain_requires_reconciliation":
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError(f"fleet intent {intent.intent_id} did not become outcome-unknown")
            overflow = qualification_intent(account_id, "bot-overflow", "fleet-overflow", clock.now_ms())
            try:
                await clerk.submit_async_custody(overflow, clerk_request_received_at_ms=clock.now_ms())
            except AccountClerkIntentRejected as rejected:
                if rejected.reason != "CLERK_ASYNC_ENTRY_QUEUE_FULL":
                    raise AssertionError(f"unexpected fleet overflow reason: {rejected.reason}") from rejected
                queue_refusal_count = 1
            else:
                queue_refusal_count = 0
            entry_broker_call_count = len(broker.calls)
            entries = read_account_clerk_journal(artifacts_root, account_id)
            recorded = [entry for entry in entries if entry.entry_kind == "recorded" and entry.intent is not None]
            if len(recorded) != SUPPORTED_ACCOUNT_CUSTODY_FLEET_SIZE:
                raise AssertionError("real Clerk did not record one A0 receipt per supported bot")
            queue_wait_samples = tuple(
                (entry.clerk_intake_admitted_at_ms or 0) - requests[entry.intent.intent_id]
                for entry in recorded
            )
            fsync_samples = tuple(
                (entry.inbox_fsynced_at_ms or 0) - (entry.clerk_intake_admitted_at_ms or 0)
                for entry in recorded
            )
            health = await clerk.async_custody_health()
            return RealFleetTrace(
                receipts=journal_receipts(artifacts_root, account_id),
                queue_wait_samples_ms=queue_wait_samples,
                fsync_samples_ms=fsync_samples,
                entry_broker_call_count=entry_broker_call_count,
                queue_high_water=health.entry_depth,
                queue_refusal_count=queue_refusal_count,
            )
        finally:
            await clerk.stop_async_custody()
