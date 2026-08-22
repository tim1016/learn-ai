"""Frame-seam and reconnect scenarios for the synthetic Clerk rehearsal."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from app.broker.alpaca.clerk.sqlite.enter import accept_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    admit_new_exposure,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.fault_injection import (
    ArmedFault,
    FrameFaultKind,
    craft_frame,
    get_fault_injection_registry,
)
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer, _inject_frame_faults
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.services.account_custody_synthetic_scenarios import SyntheticScenarioId
from app.services.alpaca_sqlite_synthetic_drill_support import (
    PARTIAL_FILL_LIMITATION,
    REDELIVERY_LIMITATION,
    FaultSeamLimitation,
    SyntheticBroker,
    SyntheticScenarioObservation,
    TickClock,
    fault_seam_permitted,
    fault_seam_status,
    new_repo,
    seam_not_permitted,
    synthetic_leg,
    worksheet,
)
from app.services.alpaca_sqlite_synthetic_drill_support import (
    require_invariant as _require,
)

_AUTHORIZATION_FRAME = '{"stream":"authorization","data":{"status":"authorized"}}'


class _SnapshotBlockingSyntheticBroker(SyntheticBroker):
    """Pause the first open-order snapshot so admission can be checked in-flight."""

    def __init__(self, clock: TickClock) -> None:
        super().__init__(clock)
        self.block_next_open_snapshot = False
        self.snapshot_started = asyncio.Event()
        self.snapshot_release = asyncio.Event()

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        if status == "open" and self.block_next_open_snapshot:
            self.block_next_open_snapshot = False
            self.snapshot_started.set()
            await self.snapshot_release.wait()
        return await super().list_orders(status=status, limit=limit, after_ms=after_ms)


def _frame_source(frames: tuple[str, ...]) -> AsyncIterator[bytes | str]:
    async def _frames() -> AsyncIterator[bytes | str]:
        for frame in frames:
            yield frame

    return _frames()


async def _no_backoff(_attempt: int) -> None:
    return


async def _run_injected_frames(
    *,
    repo: ClerkSqliteRepository,
    broker: SyntheticBroker,
    artifacts_root: Path,
    frames: tuple[str, ...],
) -> tuple[TradeUpdatesConsumer, tuple[str, ...]]:
    frame_broker_order_ids: list[str] = []

    async def _record_injected_source() -> AsyncIterator[bytes | str]:
        async for frame in _inject_frame_faults(_frame_source(frames)):
            text = frame.decode("utf-8") if isinstance(frame, bytes) else frame
            try:
                message = json.loads(text)
            except (TypeError, ValueError):
                message = None
            if isinstance(message, dict) and message.get("stream") == "trade_updates":
                data = message.get("data")
                order = data.get("order") if isinstance(data, dict) else None
                if isinstance(order, dict):
                    broker_order_id = order.get("id")
                    if isinstance(broker_order_id, str):
                        frame_broker_order_ids.append(broker_order_id)
            yield frame

    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo,
        intake=facade.intake,
        reconciler=facade,
    )
    journal = CaptureJournal(capture_dir=artifacts_root / "broker-captures", clock=broker.clock)
    consumer = TradeUpdatesConsumer(
        read=broker,
        evidence_sink=sink,
        frame_source=_record_injected_source,
        journal=journal,
        clock=broker.clock,
        backoff=_no_backoff,
        max_reconnects=0,
    )
    await consumer.run()
    return consumer, tuple(frame_broker_order_ids)


async def duplicate_redelivery(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, account_id, runs = new_repo(artifacts_root, suffix="duplicate")
    broker = SyntheticBroker(clock)
    try:
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        first = await submit_enter(
            repo,
            account_id=account_id,
            strategy_instance_id="spy-bot",
            decision_id="duplicate",
            lifecycle_run_id=runs["spy-bot"],
            leg=synthetic_leg(quantity=10),
            trade=broker,
        )
        _require(first.order_ref is not None, "duplicate drill must mint an order_ref")
        initial_order = repo.order(first.order_ref)
        _require(
            initial_order is not None and initial_order.broker_order_id is not None,
            "duplicate drill requires the Clerk broker-order identity",
        )
        initial_broker_order_id = broker.orders[first.order_ref].order_id
        _require(
            initial_order.broker_order_id == initial_broker_order_id,
            "duplicate drill broker-order identities must agree",
        )
        transitions_before = len(repo.custody_transitions())
        registry = get_fault_injection_registry()
        seam_permitted = fault_seam_permitted(registry)
        seam_exercised = False
        limitations: tuple[FaultSeamLimitation, ...] = ()
        if seam_permitted:
            real_frame = craft_frame(
                ArmedFault(
                    kind=FrameFaultKind.PARTIAL_FILL.value,
                    target=first.order_ref,
                    remaining=1,
                    params={
                        "broker_order_id": initial_broker_order_id,
                        "qty": "1",
                        "order_qty": "10",
                        "execution_id": "duplicate-1",
                    },
                )
            )
            frame_order = json.loads(real_frame)["data"]["order"]
            _require(
                frame_order["id"] == initial_broker_order_id,
                "duplicate frame must preserve the broker-order identity",
            )
            registry.arm(FrameFaultKind.REDELIVER_LAST.value)
            consumer, frame_broker_order_ids = await _run_injected_frames(
                repo=repo,
                broker=broker,
                artifacts_root=artifacts_root,
                frames=(real_frame,),
            )
            broker.seed_order(
                first.order_ref,
                quantity=10,
                status="partially_filled",
                filled_quantity=1,
                broker_order_id=initial_broker_order_id,
            )
            clerk_order = repo.order(first.order_ref)
            broker_order = broker.orders[first.order_ref]
            identity_stable = (
                frame_broker_order_ids == (initial_broker_order_id, initial_broker_order_id)
                and clerk_order is not None
                and clerk_order.broker_order_id == initial_broker_order_id
                and broker_order.order_id == initial_broker_order_id
                and len(
                    {
                        *frame_broker_order_ids,
                        clerk_order.broker_order_id,
                        broker_order.order_id,
                    }
                )
                == 1
            )
            seam_exercised = (
                consumer.counters.events_applied == 1
                and consumer.counters.skipped_duplicate == 1
                and registry.status()["frame"] == []
                and identity_stable
            )
            passed = (
                seam_exercised and len(repo.fills_for_order(first.order_ref)) == 1 and len(broker.submit_calls) == 1
            )
            observed = (
                f"events_applied={consumer.counters.events_applied}; "
                f"skipped_duplicate={consumer.counters.skipped_duplicate}; "
                f"fill_rows={len(repo.fills_for_order(first.order_ref))}; "
                f"frame_broker_order_ids={frame_broker_order_ids}; "
                f"clerk_broker_order_id={clerk_order.broker_order_id if clerk_order else None}; "
                f"broker_order_id={broker_order.order_id}; "
                f"identity_stable={identity_stable}"
            )
        else:
            second = await submit_enter(
                repo,
                account_id=account_id,
                strategy_instance_id="spy-bot",
                decision_id="duplicate",
                lifecycle_run_id=runs["spy-bot"],
                leg=synthetic_leg(quantity=10),
                trade=broker,
            )
            passed = (
                len(broker.submit_calls) == 1
                and len(repo.custody_transitions()) == transitions_before
                and first.order_ref == second.order_ref
            )
            observed = (
                f"seam_permitted=False; submit_count={len(broker.submit_calls)}; transition_count={transitions_before}"
            )
            limitations = (
                REDELIVERY_LIMITATION,
                seam_not_permitted(FrameFaultKind.REDELIVER_LAST.value),
            )
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.DUPLICATE_TRADE_UPDATE,
            status=fault_seam_status(passed=passed, seam_exercised=seam_exercised),
            assertion=(
                "A registry redelivery crossed the production frame wrapper and SQLite "
                "evidence sink without a second fill or broker intent."
            ),
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=broker.proof(),
                expected="one applied frame, one idempotent skip, one durable fill",
                observed=observed,
                action="TRADE_UPDATE_REDELIVERY",
                receipt_id=first.command.receipt_id,
            ),
            limitations=limitations,
            fault_seam_required=True,
            fault_seam_exercised=seam_exercised,
        )
    finally:
        repo.close()


async def cancel_fill_race(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, account_id, runs = new_repo(artifacts_root, suffix="cancel-fill")
    broker = SyntheticBroker(clock)
    try:
        entry = await submit_enter(
            repo,
            account_id=account_id,
            strategy_instance_id="spy-bot",
            decision_id="entry",
            lifecycle_run_id=runs["spy-bot"],
            leg=synthetic_leg(quantity=10),
            trade=broker,
        )
        _require(entry.order_ref is not None, "cancel-fill entry must mint an order_ref")
        initial_entry = repo.order(entry.order_ref)
        _require(
            initial_entry is not None and initial_entry.broker_order_id is not None,
            "cancel-fill drill requires the Clerk broker-order identity",
        )
        initial_broker_order_id = broker.orders[entry.order_ref].order_id
        _require(
            initial_entry.broker_order_id == initial_broker_order_id,
            "cancel-fill broker-order identities must agree",
        )
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        registry = get_fault_injection_registry()
        seam_permitted = fault_seam_permitted(registry)
        seam_exercised = False
        limitations: tuple[FaultSeamLimitation, ...] = ()
        frame_events_applied = 0
        frame_broker_order_ids: tuple[str, ...] = ()
        accepted = accept_exit(
            repo,
            account_id=account_id,
            strategy_instance_id="spy-bot",
            decision_id="exit",
            lifecycle_run_id=runs["spy-bot"],
            entry_order_ref=entry.order_ref,
        )
        _require(
            accepted.effect_operation_id is not None,
            "cancel-fill exit must mint an effect_operation_id",
        )
        if seam_permitted:
            broker.cancel_started = asyncio.Event()
            broker.cancel_release = asyncio.Event()
            resolve_task = asyncio.create_task(
                resolve_exit(
                    repo,
                    effect_operation_id=accepted.effect_operation_id,
                    trade=broker,
                )
            )
            try:
                await asyncio.wait_for(broker.cancel_started.wait(), timeout=2.0)
                registry.arm(
                    FrameFaultKind.PARTIAL_FILL.value,
                    target=entry.order_ref,
                    params={
                        "broker_order_id": initial_broker_order_id,
                        "qty": "4",
                        "order_qty": "10",
                        "price": "100.00",
                        "execution_id": "cancel-fill-race-1",
                    },
                )
                consumer, frame_broker_order_ids = await _run_injected_frames(
                    repo=repo,
                    broker=broker,
                    artifacts_root=artifacts_root,
                    frames=(_AUTHORIZATION_FRAME,),
                )
                frame_events_applied = consumer.counters.events_applied
                broker.seed_order(
                    entry.order_ref,
                    quantity=10,
                    status="partially_filled",
                    filled_quantity=4,
                    broker_order_id=initial_broker_order_id,
                )
            except BaseException:
                broker.cancel_release.set()
                resolve_task.cancel()
                with suppress(asyncio.CancelledError):
                    await resolve_task
                raise
            else:
                broker.cancel_release.set()
                result = await resolve_task
        else:
            broker.fill_during_cancel_quantity = 4.0
            limitations = (
                PARTIAL_FILL_LIMITATION,
                seam_not_permitted(FrameFaultKind.PARTIAL_FILL.value),
            )
            result = await resolve_exit(
                repo,
                effect_operation_id=accepted.effect_operation_id,
                trade=broker,
            )
        entry_transitions = repo.transitions_for_order(entry.order_ref)
        cancel_requested_sequence = next(
            item["sequence"] for item in entry_transitions if item["transition_kind"] == "ORDER_CANCEL_REQUESTED"
        )
        partial_fill_sequence = next(
            (
                item["sequence"]
                for item in entry_transitions
                if item["transition_kind"] == "EXECUTION_SLICE_FILLED"
            ),
            None,
        )
        terminal_sequence = next(
            item["sequence"] for item in entry_transitions if item["transition_kind"] == "ENTRY_TERMINAL_CONFIRMED"
        )
        race_ordered = (
            partial_fill_sequence is not None
            and cancel_requested_sequence < partial_fill_sequence < terminal_sequence
        )
        seam_exercised = (
            seam_permitted
            and frame_events_applied == 1
            and len(repo.fills_for_order(entry.order_ref)) == 1
            and registry.status()["frame"] == []
            and race_ordered
        )
        reducing = repo.order(result.reducing_order_ref) if result.reducing_order_ref else None
        submitted_close = broker.orders.get(result.reducing_order_ref or "")
        current_entry = repo.order(entry.order_ref)
        broker_entry = broker.orders.get(entry.order_ref)
        identity_stable = not seam_permitted or (
            frame_broker_order_ids == (initial_broker_order_id,)
            and current_entry is not None
            and current_entry.broker_order_id == initial_broker_order_id
            and broker_entry is not None
            and broker_entry.order_id == initial_broker_order_id
        )
        passed = (
            reducing is not None
            and len(broker.submit_calls) == 2
            and submitted_close is not None
            and submitted_close.quantity == 4.0
            and identity_stable
            and (not seam_permitted or (race_ordered and seam_exercised))
        )
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.CANCEL_FILL_RACE,
            status=fault_seam_status(passed=passed, seam_exercised=seam_exercised),
            assertion=(
                "While cancel was in flight, a registry partial-fill crossed the "
                "production consumer; EXIT then proved terminal state and reduced "
                "exactly the four durably attributed shares."
            ),
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=broker.proof(),
                expected=("cancel requested < execution slice recorded < terminal proven; submit one sell for 4"),
                observed=(
                    f"seam_permitted={seam_permitted}; seam_exercised={seam_exercised}; "
                    f"frame_events_applied={frame_events_applied}; "
                    f"frame_broker_order_ids={frame_broker_order_ids}; "
                    f"clerk_broker_order_id={current_entry.broker_order_id if current_entry else None}; "
                    f"broker_order_id={broker_entry.order_id if broker_entry else None}; "
                    f"identity_stable={identity_stable}; "
                    f"race_ordered={race_ordered}; "
                    f"cancel_requested_sequence={cancel_requested_sequence}; "
                    f"partial_fill_sequence={partial_fill_sequence}; "
                    f"terminal_sequence={terminal_sequence}; "
                    f"cancel_calls={broker.cancel_calls}; "
                    f"reducing_qty={submitted_close.quantity if submitted_close else None}"
                ),
                action="RESOLVE_EXIT_CANCEL_FIRST",
                receipt_id=result.command.receipt_id,
            ),
            limitations=limitations,
            fault_seam_required=True,
            fault_seam_exercised=seam_exercised,
        )
    finally:
        repo.close()


async def gap_reconcile(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, account_id, runs = new_repo(artifacts_root, suffix="gap")
    broker = _SnapshotBlockingSyntheticBroker(clock)
    try:
        accepted = accept_enter(
            repo,
            account_id=account_id,
            strategy_instance_id="spy-bot",
            decision_id="gap-order",
            lifecycle_run_id=runs["spy-bot"],
            leg=synthetic_leg(),
        )
        _require(accepted.order_ref is not None, "gap drill must mint an order_ref")
        broker.seed_order(accepted.order_ref, status="filled", filled_quantity=1.0)
        now = clock()
        broker.positions = [
            BrokerPosition(
                broker="alpaca",
                symbol="SPY",
                asset_id=None,
                asset_class="us_equity",
                quantity=1.0,
                side="long",
                average_entry_price=100.0,
                market_value=100.0,
                cost_basis=100.0,
                current_price=100.0,
                unrealized_pl=0.0,
                unrealized_plpc=0.0,
                observed_at_ms=now,
            )
        ]
        raise_uncertainty(
            repo,
            strategy_instance_id=None,
            reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
            headline="Synthetic trade-update gap",
            explanation="Admission waits for a REST reconciliation.",
            operator_impact="New exposure is blocked.",
            next_step="Reconcile.",
            cause_facts={"snapshot": "open_orders_and_positions"},
        )
        # The seeded SPY ENTER is deliberately filled by the reconciliation
        # snapshot. Probe an otherwise unrelated strategy so this drill
        # isolates the account-wide stale-snapshot hold rather than asking a
        # second ENTER to ignore the first strategy's real attributed exposure.
        before_admission = admit_new_exposure(repo, strategy_instance_id="qqq-bot")
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        registry = get_fault_injection_registry()
        seam_permitted = fault_seam_permitted(registry)
        if seam_permitted:
            registry.arm(FrameFaultKind.DISCONNECT.value)
        facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
        sink = SqliteTradeUpdateEvidenceSink(
            repo=repo,
            intake=facade.intake,
            reconciler=facade,
        )
        consumer = TradeUpdatesConsumer(
            read=broker,
            evidence_sink=sink,
            frame_source=lambda: _inject_frame_faults(_frame_source((_AUTHORIZATION_FRAME,))),
            journal=CaptureJournal(
                capture_dir=artifacts_root / "broker-captures",
                clock=clock,
            ),
            clock=clock,
            backoff=_no_backoff,
            max_reconnects=1,
        )
        broker.block_next_open_snapshot = True
        disconnect_source_event_at_ms = clock()
        consumer_task = asyncio.create_task(consumer.run())
        try:
            await asyncio.wait_for(broker.snapshot_started.wait(), timeout=2.0)
            during_admission = admit_new_exposure(repo, strategy_instance_id="qqq-bot")
        except BaseException:
            broker.snapshot_release.set()
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task
            raise
        else:
            broker.snapshot_release.set()
            await consumer_task
        after_admission = admit_new_exposure(repo, strategy_instance_id="qqq-bot")
        stale_hold_after = repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
            strategy_instance_id=None,
        )
        seam_exercised = seam_permitted and registry.status()["frame"] == []
        passed = (
            not before_admission.allowed
            and not during_admission.allowed
            and during_admission.reason_code == "RECONCILIATION_IN_PROGRESS"
            and after_admission.allowed
            and stale_hold_after is None
            and consumer.counters.reconnects == 2
            and (not seam_permitted or seam_exercised)
        )
        limitations: tuple[FaultSeamLimitation, ...] = ()
        if not seam_permitted:
            limitations = (seam_not_permitted(FrameFaultKind.DISCONNECT.value),)
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.TRADE_UPDATE_GAP_RECONCILIATION,
            status=fault_seam_status(passed=passed, seam_exercised=seam_exercised),
            assertion=(
                "Admission remained closed until the production consumer's reconnect "
                "path folded the missed fill and proved the REST snapshot clean."
            ),
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=broker.proof(),
                expected=(
                    "blocked before and during REST reconciliation; reconnect resolves stale hold; admitted after"
                ),
                observed=(
                    f"before={before_admission.allowed}; "
                    f"during={during_admission.allowed}; "
                    f"during_reason={during_admission.reason_code}; "
                    f"after={after_admission.allowed}; "
                    f"stale_hold_after={stale_hold_after is not None}; "
                    f"reconnects={consumer.counters.reconnects}; "
                    f"seam_exercised={seam_exercised}"
                ),
                action="TRADE_UPDATE_RECONNECT_RECONCILE",
                source_event_at_ms=disconnect_source_event_at_ms,
            ),
            limitations=limitations,
            fault_seam_required=True,
            fault_seam_exercised=seam_exercised,
        )
    finally:
        repo.close()


__all__ = ["cancel_fill_race", "duplicate_redelivery", "gap_reconcile"]
