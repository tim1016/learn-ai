"""Authority-boundary tests for Alpaca trade-update evidence."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.broker.alpaca import adapter
from app.broker.alpaca.clerk.sqlite import reads, schema
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.external_orders import (
    UNIDENTIFIED_BROKER_ORDER_ID,
    acknowledge_unfoldable_broker_order,
    record_unfoldable_broker_order,
    unfoldable_broker_orders_active_since,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_external_order_api import (
    ExternalOrderNotFoundError,
)
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    decide_capability,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXECUTION_PRICE_CONFLICT_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.broker.contract.ports import BrokerReadPort
from app.services.sqlite_clerk_compat import sqlite_clerk_status

ACCOUNT_ID = "PA-TEST"
STRATEGY_INSTANCE_ID = "spy-bot"
RUN_ID = "run-1"


class _NoBrokerMutation:
    async def submit(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("evidence recovery must not submit")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("evidence recovery must not cancel")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


class _NoReconciler:
    async def reconcile_account(self, *, trigger: str) -> SimpleNamespace:
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


class _EvidenceSink:
    def __init__(self) -> None:
        self.consumer: TradeUpdatesConsumer | None = None
        self.gap_connection_states: list[bool] = []
        self.reconnect_read_guarded = False

    def guard_reconnect_read(self, read: BrokerReadPort) -> BrokerReadPort:
        self.reconnect_read_guarded = True
        return read

    async def record_lifecycle_event(self, **_kwargs: Any) -> str:
        return "order_event"

    async def reconcile_gap(self) -> None:
        assert self.consumer is not None
        self.gap_connection_states.append(self.consumer.connected)


class _ClosedOrderRead:
    async def list_orders(self, **_kwargs: Any) -> list:
        return []


class _Capture:
    def record(self, **_kwargs: Any) -> bool:
        return True


def _owned_order(order_ref: str, *, status: str = "partially_filled") -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id="broker-order-1",
        client_order_id=order_ref,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=5.0,
        # This aggregate number must not become an additional fill when the
        # matching websocket frame carries an exact execution slice.
        filled_quantity=5.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=101.0,
        status=status,
        submitted_at_ms=1_700_000_000_000,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_100,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_100,
    )


def _initialize_owned_order(tmp_path: Path) -> tuple[ClerkSqliteRepository, str]:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id=STRATEGY_INSTANCE_ID,
        symbol="SPY",
        config_hash="config-hash",
    )
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=STRATEGY_INSTANCE_ID,
        lifecycle_run_id=RUN_ID,
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=STRATEGY_INSTANCE_ID,
        decision_id="execution-slice",
        lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=5.0),
    )
    assert accepted.order_ref is not None
    return repo, accepted.order_ref


def _sqlite_sink(repo: ClerkSqliteRepository) -> SqliteTradeUpdateEvidenceSink:
    return SqliteTradeUpdateEvidenceSink(
        repo=repo,
        intake=ReentrantAsyncLock(),
        reconciler=_NoReconciler(),
    )


def _authorization_source() -> AsyncIterator[bytes | str]:
    async def _frames() -> AsyncIterator[bytes | str]:
        yield '{"stream":"authorization","data":{"status":"authorized"}}'

    return _frames()


async def test_reconcile_gap_uses_authority_facade(tmp_path: Path) -> None:
    class _Reconciler:
        def __init__(self) -> None:
            self.triggers: list[str] = []

        async def reconcile_account(self, *, trigger: str) -> SimpleNamespace:
            self.triggers.append(trigger)
            return SimpleNamespace(verdict="clean")

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    reconciler = _Reconciler()
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo,
        intake=ReentrantAsyncLock(),
        reconciler=reconciler,
    )

    await sink.reconcile_gap()

    assert reconciler.triggers == ["AUTOMATIC"]
    repo.close()


async def test_sqlite_websocket_fill_records_exact_execution_and_separate_ack(
    tmp_path: Path,
) -> None:
    repo, order_ref = _initialize_owned_order(tmp_path)
    try:
        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=order_ref,
            event=BrokerOrderEvent(
                event_type="partial_fill",
                occurred_at_ms=1_700_000_000_050,
                price=100.25,
                quantity=2.0,
                execution_id="exec-001",
            ),
            event_key="exec:exec-001",
            order=_owned_order(order_ref),
            recovery_source=None,
            recovery_window_limit=None,
        )

        fills = repo.fills_for_order(order_ref)
        assert len(fills) == 1
        fill = fills[0]
        assert {
            key: fill[key]
            for key in (
                "fill_id",
                "order_ref",
                "qty",
                "price",
                "side",
                "is_correction",
                "execution_id",
                "evidence_source",
                "event_kind",
                "superseded_execution_ref",
                "fee",
                "fee_fidelity",
                "source_event_at_ms",
            )
        } == {
            "fill_id": "exec-001",
            "order_ref": order_ref,
            "qty": 2.0,
            "price": 100.25,
            "side": "BUY",
            "is_correction": 0,
            "execution_id": "exec-001",
            "evidence_source": "websocket",
            "event_kind": "fill",
            "superseded_execution_ref": None,
            "fee": None,
            "fee_fidelity": "not_reported",
            "source_event_at_ms": 1_700_000_000_050,
        }
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 2.0
        transition_kinds = [
            transition["transition_kind"] for transition in repo.transitions_for_order(order_ref)
        ]
        assert "EXECUTION_SLICE_FILLED" in transition_kinds
        assert "ORDER_SUBMIT_ACKED" in transition_kinds
        assert "ORDER_FILL_OBSERVED" not in transition_kinds
    finally:
        repo.close()


def _active_price_conflict_count(repo: ClerkSqliteRepository) -> int:
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
        EXECUTION_PRICE_CONFLICT_REASON_CODE,
    )

    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM uncertainties WHERE reason_code = ? AND resolved_at_ms IS NULL",
        (EXECUTION_PRICE_CONFLICT_REASON_CODE,),
    ).fetchone()
    return int(row["n"])


async def test_a_websocket_aggregate_clears_a_price_conflict_opened_by_a_snapshot(
    tmp_path: Path,
) -> None:
    """#2460 review: a REST partial-order snapshot can open an
    ``EXECUTION_PRICE_CONFLICT``; when a later websocket fill's exact slice
    advances the recorded fills and its aggregate now agrees, that frame must
    clear the conflict -- otherwise the stored reported average goes stale the
    moment the order terminalizes out of the open-order snapshots and the
    sweep compares against it for ever."""
    repo, order_ref = _initialize_owned_order(tmp_path)
    try:
        # Websocket fill #1: exact 2 @ 100 with a matching aggregate.
        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=order_ref,
            event=BrokerOrderEvent(
                event_type="partial_fill",
                occurred_at_ms=1_700_000_000_050,
                price=100.0,
                quantity=2.0,
                execution_id="exec-001",
            ),
            event_key="exec:exec-001",
            order=_owned_order(order_ref).model_copy(
                update={"filled_quantity": 2.0, "filled_avg_price": 100.0}
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )
        # A REST partial-order snapshot restates the same quantity at 90.
        fold_order_evidence(
            repo,
            effect_operation_id=repo.order(order_ref).effect_operation_id,
            order=_owned_order(order_ref).model_copy(
                update={
                    "filled_quantity": 2.0,
                    "filled_avg_price": 90.0,
                    "updated_at_ms": 1_700_000_000_200,
                }
            ),
        )
        assert _active_price_conflict_count(repo) == 1

        # Websocket fill #2: exact 3 @ 100; the frame's aggregate (5 @ 100)
        # now agrees with the recorded fills and must clear the conflict.
        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=order_ref,
            event=BrokerOrderEvent(
                event_type="fill",
                occurred_at_ms=1_700_000_000_300,
                price=100.0,
                quantity=3.0,
                execution_id="exec-002",
            ),
            event_key="exec:exec-002",
            order=_owned_order(order_ref, status="filled").model_copy(
                update={
                    "filled_quantity": 5.0,
                    "filled_avg_price": 100.0,
                    "updated_at_ms": 1_700_000_000_400,
                }
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert _active_price_conflict_count(repo) == 0
    finally:
        repo.close()


async def test_sqlite_websocket_fill_completes_manual_ticket_with_exact_coverage(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    accepted = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="operator",
        ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
        leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1.0),
    )
    assert accepted.leg.effect_operation_id is not None
    assert accepted.leg.order_ref is not None
    order = _owned_order(accepted.leg.order_ref, status="filled").model_copy(
        update={"quantity": 1.0, "filled_quantity": 1.0, "filled_avg_price": 100.25}
    )
    try:
        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=accepted.leg.order_ref,
            event=BrokerOrderEvent(
                event_type="fill",
                occurred_at_ms=1_700_000_000_050,
                price=100.25,
                quantity=1.0,
                execution_id="manual-exec-001",
            ),
            event_key="exec:manual-exec-001",
            order=order,
            recovery_source=None,
            recovery_window_limit=None,
        )

        ticket = repo.manual_order_ticket(accepted.ticket.ticket_id)
        assert ticket is not None
        assert ticket.state == "COMPLETED"
        assert ticket.legs[0].state == "SUCCEEDED"
        command = repo.get_command(accepted.command.command_id)
        assert command is not None and command.state == "succeeded"
        assert command.receipt_id == f"receipt:{accepted.leg.effect_operation_id}"
        assert repo.attributed_positions_for_subject(ticket.subject_id) == {"SPY": 1.0}
        assert repo.has_order_transition(
            order_ref=accepted.leg.order_ref,
            transition_kind="MANUAL_ORDER_FILLED",
        )
    finally:
        repo.close()


async def test_sqlite_unexplained_trade_update_records_external_order_without_a_bot_fill(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    foreign = _owned_order("alpaca-console:operator-order-1").model_copy(
        update={"order_id": "external-order-1", "symbol": "AAPL", "quantity": 2.0}
    )
    try:
        kind = await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=foreign.client_order_id,
            event=BrokerOrderEvent(
                event_type="new",
                occurred_at_ms=foreign.observed_at_ms,
                price=None,
                quantity=None,
            ),
            event_key="external-order-1|new",
            order=foreign,
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert kind == "unexplained_order"
        assert repo.external_orders()[0]["broker_order_id"] == "external-order-1"
        assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER_HOLD") is not None
        assert repo.attributed_positions_by_symbol() == {}
        assert repo.fills_for_order("external-order-1") == []
    finally:
        repo.close()


async def test_sqlite_websocket_execution_redelivery_and_restart_append_no_duplicate_transition(
    tmp_path: Path,
) -> None:
    repo, order_ref = _initialize_owned_order(tmp_path)
    event = BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=1_700_000_000_050,
        price=100.25,
        quantity=2.0,
        execution_id="exec-redelivery",
    )
    order = _owned_order(order_ref, status="filled")
    try:
        sink = _sqlite_sink(repo)
        await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=event,
            event_key="exec:exec-redelivery",
            order=order,
            recovery_source=None,
            recovery_window_limit=None,
        )
        await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=event,
            event_key="exec:exec-redelivery",
            order=order,
            recovery_source=None,
            recovery_window_limit=None,
        )
        assert len(repo.fills_for_order(order_ref)) == 1
        assert [
            transition["transition_kind"] for transition in repo.transitions_for_order(order_ref)
        ].count("EXECUTION_SLICE_FILLED") == 1
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 2.0
    finally:
        repo.close()

    restarted = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        await _sqlite_sink(restarted).record_lifecycle_event(
            client_order_id=order_ref,
            event=event,
            event_key="exec:exec-redelivery",
            order=order,
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert len(restarted.fills_for_order(order_ref)) == 1
        assert [
            transition["transition_kind"]
            for transition in restarted.transitions_for_order(order_ref)
        ].count("EXECUTION_SLICE_FILLED") == 1
        assert restarted.position(STRATEGY_INSTANCE_ID, "SPY") == 2.0
    finally:
        restarted.close()


async def test_sqlite_rest_recovery_folds_cumulative_fill_without_fabricating_an_execution_id(
    tmp_path: Path,
) -> None:
    repo, order_ref = _initialize_owned_order(tmp_path)
    recovered_order = _owned_order(order_ref, status="filled")
    try:
        kind = await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=order_ref,
            event=BrokerOrderEvent(
                event_type="fill",
                occurred_at_ms=1_700_000_000_100,
                price=recovered_order.filled_avg_price,
                quantity=recovered_order.filled_quantity,
                execution_id=None,
            ),
            event_key="recovery:closed-order-1",
            order=recovered_order,
            recovery_source="closed_orders_window",
            recovery_window_limit=50,
        )

        assert kind == "order_event"
        fills = repo.fills_for_order(order_ref)
        assert len(fills) == 1
        assert fills[0]["execution_id"] is None
        assert fills[0]["evidence_source"] == "cumulative_recovery"
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 5.0
    finally:
        repo.close()


async def test_manual_rest_recovery_and_later_exact_evidence_auto_supersede_subject_scoped(
    tmp_path: Path,
) -> None:
    """A direct manual exact replacement preserves the manual subject boundary."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    accepted = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="operator",
        ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
        leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1.0),
    )
    assert accepted.leg.effect_operation_id is not None
    assert accepted.leg.order_ref is not None
    recovered = _owned_order(accepted.leg.order_ref).model_copy(
        update={"quantity": 1.0, "filled_quantity": 1.0, "filled_avg_price": 101.0}
    )
    try:
        fold_order_evidence(
            repo,
            effect_operation_id=accepted.leg.effect_operation_id,
            order=recovered,
        )
        assert repo.attributed_positions_for_subject(accepted.ticket.subject_id) == {"SPY": 1.0}

        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=accepted.leg.order_ref,
            event=BrokerOrderEvent(
                event_type="fill",
                occurred_at_ms=1_700_000_000_150,
                price=101.0,
                quantity=1.0,
                execution_id="manual-exact-after-recovery",
            ),
            event_key="exec:manual-exact-after-recovery",
            order=recovered,
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert len(repo.fills_for_order(accepted.leg.order_ref)) == 1
        assert repo.fills_for_order(accepted.leg.order_ref)[0]["execution_id"] == "manual-exact-after-recovery"
        uncertainty = repo.active_uncertainties_for_admission(subject_id=accepted.ticket.subject_id)
        assert uncertainty == []
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            subject_id=accepted.ticket.subject_id,
        )
        assert admission.allowed is False
        assert admission.reason_code == "MANUAL_ORDER_OUTSTANDING"
    finally:
        repo.close()


async def test_manual_changed_execution_redelivery_raises_one_subject_conflict(
    tmp_path: Path,
) -> None:
    """A changed manual execution ID is never silently discarded as a replay."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    accepted = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="operator",
        ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
        leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1.0),
    )
    assert accepted.leg.order_ref is not None
    original = BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=1_700_000_000_050,
        price=100.0,
        quantity=1.0,
        execution_id="manual-changed-redelivery",
    )
    changed = original.model_copy(update={"occurred_at_ms": 1_700_000_000_051, "price": 101.0})
    try:
        sink = _sqlite_sink(repo)
        await sink.record_lifecycle_event(
            client_order_id=accepted.leg.order_ref,
            event=original,
            event_key="exec:manual-changed-redelivery:original",
            order=_owned_order(accepted.leg.order_ref, status="filled").model_copy(
                update={"quantity": 1.0, "filled_quantity": 1.0, "filled_avg_price": 100.0}
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )
        await sink.record_lifecycle_event(
            client_order_id=accepted.leg.order_ref,
            event=changed,
            event_key="exec:manual-changed-redelivery:changed",
            order=_owned_order(accepted.leg.order_ref, status="filled").model_copy(
                update={"quantity": 1.0, "filled_quantity": 1.0, "filled_avg_price": 101.0}
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert len(repo.fills_for_order(accepted.leg.order_ref)) == 1
        assert repo.attributed_positions_for_subject(accepted.ticket.subject_id) == {"SPY": 1.0}
        uncertainty = repo.active_uncertainties_for_admission(subject_id=accepted.ticket.subject_id)
        # Two distinct truths, one episode each: the identity replay raises the
        # coverage conflict, and the changed frame's aggregate (1 @ 101 against
        # the recorded 1 @ 100) is the same-quantity price restatement #2460
        # records from websocket totals too -- subject-scoped, never a second
        # coverage conflict per redelivery.
        assert {row["reason_code"] for row in uncertainty} == {
            EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
            EXECUTION_PRICE_CONFLICT_REASON_CODE,
        }
    finally:
        repo.close()


async def test_sqlite_changed_execution_redelivery_raises_one_coverage_conflict(
    tmp_path: Path,
) -> None:
    repo, order_ref = _initialize_owned_order(tmp_path)
    original = BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=1_700_000_000_050,
        price=100.25,
        quantity=2.0,
        execution_id="exec-changed-redelivery",
    )
    changed = BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=1_700_000_000_051,
        price=101.25,
        quantity=3.0,
        execution_id="exec-changed-redelivery",
    )
    try:
        sink = _sqlite_sink(repo)
        await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=original,
            event_key="exec:exec-changed-redelivery:original",
            order=_owned_order(order_ref, status="filled"),
            recovery_source=None,
            recovery_window_limit=None,
        )
        await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=changed,
            event_key="exec:exec-changed-redelivery:changed",
            order=_owned_order(order_ref, status="filled"),
            recovery_source=None,
            recovery_window_limit=None,
        )
        await sink.record_lifecycle_event(
            client_order_id=order_ref,
            event=changed,
            event_key="exec:exec-changed-redelivery:changed",
            order=_owned_order(order_ref, status="filled"),
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert len(repo.fills_for_order(order_ref)) == 1
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 2.0
        transition_kinds = [
            transition["transition_kind"] for transition in repo.transitions_for_order(order_ref)
        ]
        assert transition_kinds.count("EXECUTION_SLICE_FILLED") == 1
        assert transition_kinds.count("UNCERTAINTY_RAISED") == 1
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id=STRATEGY_INSTANCE_ID,
        )
        assert admission.allowed is False
        assert admission.reason_code == EXECUTION_COVERAGE_CONFLICT_REASON_CODE
    finally:
        repo.close()


async def test_sqlite_late_exact_execution_after_recovery_auto_supersedes_across_restart(
    tmp_path: Path,
) -> None:
    repo, order_ref = _initialize_owned_order(tmp_path)
    recovery_order = _owned_order(order_ref)
    late_event = BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=1_700_000_000_150,
        price=101.0,
        quantity=5.0,
        execution_id="exec-late-after-recovery",
    )
    try:
        local_order = repo.order(order_ref)
        assert local_order is not None
        fold_order_evidence(
            repo,
            effect_operation_id=local_order.effect_operation_id,
            order=recovery_order,
        )
        assert len(repo.fills_for_order(order_ref)) == 1
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 5.0

        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id=order_ref,
            event=late_event,
            event_key="exec:exec-late-after-recovery",
            order=_owned_order(order_ref, status="filled"),
            recovery_source=None,
            recovery_window_limit=None,
        )

        fills = repo.fills_for_order(order_ref)
        assert len(fills) == 1
        assert fills[0]["execution_id"] == "exec-late-after-recovery"
        assert fills[0]["evidence_source"] == "websocket"
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 5.0
        transition_kinds = [
            transition["transition_kind"] for transition in repo.transitions_for_order(order_ref)
        ]
        assert "EXECUTION_SLICE_FILLED" not in transition_kinds
        assert transition_kinds.count("EXECUTION_COVERAGE_SUPERSEDED") == 1
        assert "EXECUTION_COVERAGE_QUARANTINED" not in transition_kinds
        # The coverage supersession is clean, but the position is still 5.0
        # (this ENTER was never exited) — #1722's ENTER fence (ADR 0042, PRD
        # FR-020) refuses a fresh ENTER whenever attributed exposure exists,
        # independent of how cleanly the underlying evidence reconciled.
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id=STRATEGY_INSTANCE_ID,
        )
        assert admission.allowed is False
        assert admission.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"
    finally:
        repo.close()

    restarted = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        transitions_before_redelivery = restarted.transitions_for_order(order_ref)
        await _sqlite_sink(restarted).record_lifecycle_event(
            client_order_id=order_ref,
            event=late_event,
            event_key="exec:exec-late-after-recovery",
            order=_owned_order(order_ref, status="filled"),
            recovery_source=None,
            recovery_window_limit=None,
        )

        assert len(restarted.fills_for_order(order_ref)) == 1
        assert restarted.position(STRATEGY_INSTANCE_ID, "SPY") == 5.0
        assert restarted.transitions_for_order(order_ref) == transitions_before_redelivery
        # Same #1722 ENTER fence (ADR 0042, PRD FR-020): the position is
        # still 5.0 across the restart, so a fresh ENTER stays refused.
        admission = decide_capability(
            restarted,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id=STRATEGY_INSTANCE_ID,
        )
        assert admission.allowed is False
        assert admission.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"
    finally:
        restarted.close()


async def test_reconnect_reconciles_selected_sink_before_connection_reopens() -> None:
    sink = _EvidenceSink()
    consumer = TradeUpdatesConsumer(
        evidence_sink=sink,
        read=cast(BrokerReadPort, _ClosedOrderRead()),
        frame_source=_authorization_source,
        journal=cast(CaptureJournal, _Capture()),
        backoff=lambda _attempt: _no_backoff(),
        max_reconnects=1,
    )
    sink.consumer = consumer

    await consumer.run()

    assert sink.gap_connection_states == [False]
    assert sink.reconnect_read_guarded is True


async def _no_backoff() -> None:
    return


async def test_unexplained_hold_refresh_retains_its_broker_event_and_names_its_episode(
    tmp_path: Path,
) -> None:
    """A hold refresh keeps the causing event *and* gains its episode id.

    ``observe_uncertainty`` replaces the caller's ``proof_reference`` on the
    refresh branch with the active uncertainty id. That looks like the broker
    event key being dropped, and it is worth pinning why it is not: the key is
    on the same row in ``facts_json.evidence_refs``, put there by this caller,
    so the refresh row carries strictly more than the pre-v12
    ``observe_account_hold`` path wrote (ADR 0048 Decision 2).

    The substitution is load-bearing in the other direction — a refresh's only
    join back to its episode is ``proof_reference = uncertainty_id`` (see
    ``timeline_query._append_uncertainty_filter`` and
    ``test_timeline_uncertainty_filter_includes_refreshed_episode``). Restoring
    the event key here would silently drop every hold refresh out of its
    episode timeline.
    """
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        sink = _sqlite_sink(repo)
        for event_key in ("broker-evt-1", "broker-evt-2"):
            # ``order=None`` is the trade update the Clerk cannot explain at
            # all: no local order and no broker snapshot to file separately.
            assert (
                await sink.record_lifecycle_event(
                    client_order_id=f"coid-{event_key}",
                    event=BrokerOrderEvent(
                        event_type="new",
                        occurred_at_ms=1_700_000_000_000,
                        price=None,
                        quantity=None,
                    ),
                    event_key=event_key,
                    order=None,
                    recovery_source=None,
                    recovery_window_limit=None,
                )
                == "unexplained_order"
            )

        rows = [
            dict(row)
            for row in repo._conn.execute(
                "SELECT transition_kind, proof_reference, facts_json FROM custody_transitions "
                "WHERE transition_kind IN ('UNCERTAINTY_RAISED', 'UNCERTAINTY_REFRESHED') "
                "ORDER BY sequence"
            )
        ]
        active = repo.active_hold(
            scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE
        )
    finally:
        repo.close()

    assert [row["transition_kind"] for row in rows] == [
        "UNCERTAINTY_RAISED",
        "UNCERTAINTY_REFRESHED",
    ]
    raised, refreshed = rows

    # The raise carries the event key on the column; the refresh yields it to
    # the episode id, which is what joins it to its own timeline.
    assert raised["proof_reference"] == "broker-evt-1"
    assert active is not None
    assert refreshed["proof_reference"] == active["hold_id"]

    # Nothing is lost: the event that caused each append is in that append's
    # own evidence, and the episode's evidence is the latest one.
    assert "broker-evt-1" in json.loads(raised["facts_json"])["evidence_refs"]
    assert "broker-evt-2" in json.loads(refreshed["facts_json"])["evidence_refs"]
    assert "broker-evt-2" in json.loads(active["evidence_refs_json"])


async def test_null_sink_records_nothing_and_answers_order_event() -> None:
    from app.broker.alpaca.clerk.trade_evidence import NullTradeUpdateEvidenceSink

    sink = NullTradeUpdateEvidenceSink()
    read = object()

    assert sink.guard_reconnect_read(read) is read  # type: ignore[arg-type]
    disposition = await sink.record_lifecycle_event(
        client_order_id="x",
        event=BrokerOrderEvent(event_type="fill", occurred_at_ms=1, price=1.0, quantity=1.0),
        event_key="k",
        order=None,
        recovery_source=None,
        recovery_window_limit=None,
    )
    assert disposition == "order_event"
    assert await sink.reconcile_gap() is None


async def test_gap_replay_contains_an_unfoldable_closed_order_and_keeps_folding_the_rest(
    tmp_path: Path,
) -> None:
    """#2363: one closed order the sink cannot fold must not wedge the stream.

    A multi-leg parent arrives with ``side: null``; the external-order fold
    cannot state it truthfully. Before the fix that raise aborted every
    reconnect replay before the connection watermark, so the channel never
    reconnected, the orders after it were never replayed, and the stream-health
    hold froze exits account-wide. The poisoned order is now contained to
    itself and surfaced durably; every other order still folds, and a REDUCE
    elsewhere on the account is still admitted.
    """

    class _Reconciler:
        async def reconcile_account(self, *, trigger: str) -> SimpleNamespace:
            return SimpleNamespace(verdict="clean")

    class _ReplayRead:
        def __init__(self, orders: list[BrokerOrder]) -> None:
            self._orders = orders

        async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
            return list(self._orders)

    repo, order_ref = _initialize_owned_order(tmp_path)
    poisoned = adapter.from_alpaca_order(
        {
            "id": "mleg-parent-1",
            "client_order_id": "alpaca-console:mleg-1",
            "symbol": "AAPL",
            "side": None,
            "type": "limit",
            "time_in_force": "day",
            "qty": "1",
            "filled_qty": "1",
            "limit_price": "1.25",
            "filled_avg_price": "1.20",
            "status": "filled",
            "submitted_at": "2023-11-14T22:13:20Z",
            "created_at": "2023-11-14T22:13:20Z",
            "updated_at": "2023-11-14T22:13:21Z",
            "filled_at": "2023-11-14T22:13:21Z",
        },
        observed_at_ms=1_700_000_001_000,
    )
    owned = _owned_order(order_ref, status="filled")
    consumer = TradeUpdatesConsumer(
        evidence_sink=SqliteTradeUpdateEvidenceSink(
            repo=repo, intake=ReentrantAsyncLock(), reconciler=_Reconciler()
        ),
        # The poison comes FIRST: before the fix it cut off every later order.
        read=cast(BrokerReadPort, _ReplayRead([poisoned, owned])),
        frame_source=_authorization_source,
        journal=cast(CaptureJournal, _Capture()),
        backoff=lambda _attempt: _no_backoff(),
        max_reconnects=2,
    )
    try:
        await consumer.run()

        # Both reconnect cycles replayed the gap and reached the watermark.
        assert consumer.counters.connects == 3
        # The order after the poison folded.
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 5.0
        # The poisoned order is surfaced, never silently skipped.
        assert consumer.counters.unfoldable_orders >= 1
        episode = repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="UNFOLDABLE_BROKER_ORDER",
            strategy_instance_id=None,
        )
        assert episode is not None
        (recorded,) = json.loads(episode["facts_json"])["cause_facts"]["orders"]
        # The replay re-maps the REST snapshot, so its observation instant is
        # the consumer's clock; only its presence is pinned here.
        observed_at_ms = recorded.pop("observed_at_ms")
        assert isinstance(observed_at_ms, int)
        assert recorded.pop("last_activity_at_ms") == observed_at_ms
        assert recorded == {
            "broker_order_id": "mleg-parent-1",
            "client_order_id": "alpaca-console:mleg-1",
            "reason": "external order side must be buy or sell",
            "broker_state": "filled filled=1.0",
        }
        assert json.loads(episode["evidence_refs_json"]) == ["mleg-parent-1"]
        # A repeated replay of the same poison does not grow the hash chain.
        appended = repo._conn.execute(
            "SELECT COUNT(*) FROM custody_transitions WHERE transition_kind IN "
            "('UNCERTAINTY_RAISED', 'UNCERTAINTY_REFRESHED')"
        ).fetchone()[0]
        assert appended == 1
        # Nothing about the poison is written as an external-order row.
        assert repo.external_orders() == []
        # The surfaced fact does not freeze exits: REDUCE elsewhere is admitted.
        reduce = decide_capability(
            repo,
            capability=Capability.REDUCE,
            strategy_instance_id=STRATEGY_INSTANCE_ID,
            reduction_intent=ReductionIntent(symbol="SPY", side="sell", quantity=5.0),
        )
        assert reduce.allowed is True, reduce
    finally:
        repo.close()


async def test_second_unfoldable_order_joins_the_episode_without_dropping_the_first(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    sink = _sqlite_sink(repo)
    try:
        for broker_order_id, update in (
            ("mleg-b", {"side": "None"}),
            ("mleg-a", {"side": "None", "client_order_id": None}),
        ):
            order = _owned_order("alpaca-console:x").model_copy(
                update={"order_id": broker_order_id, **update}
            )
            kind = await sink.record_lifecycle_event(
                client_order_id=order.client_order_id,
                event=BrokerOrderEvent(
                    event_type="fill", occurred_at_ms=1, price=None, quantity=None
                ),
                event_key=f"{broker_order_id}|fill",
                order=order,
                recovery_source=None,
                recovery_window_limit=None,
            )
            assert kind == "unfoldable_order"

        episode = repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="UNFOLDABLE_BROKER_ORDER",
            strategy_instance_id=None,
        )
        assert episode is not None
        orders = json.loads(episode["facts_json"])["cause_facts"]["orders"]
        assert [(o["broker_order_id"], o["client_order_id"]) for o in orders] == [
            ("mleg-a", None),
            ("mleg-b", "alpaca-console:x"),
        ]
        # Entries are fenced until each order is acknowledged, one at a time.
        entry = decide_capability(repo, capability=Capability.NEW_EXPOSURE, subject_id="s")
        assert entry.allowed is False and entry.reason_code == "UNFOLDABLE_BROKER_ORDER"
        acknowledge_unfoldable_broker_order(repo, broker_order_id="mleg-a", operator="op-1")
        still = repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="UNFOLDABLE_BROKER_ORDER",
            strategy_instance_id=None,
        )
        assert still is not None and json.loads(still["evidence_refs_json"]) == ["mleg-b"]
        assert decide_capability(
            repo, capability=Capability.NEW_EXPOSURE, subject_id="s"
        ).allowed is False
        acknowledged = acknowledge_unfoldable_broker_order(
            repo, broker_order_id="mleg-b", operator="op-2"
        )
        assert acknowledged.ack_operator == "op-2"
        assert repo.active_uncertainties() == []
        assert decide_capability(
            repo, capability=Capability.NEW_EXPOSURE, subject_id="s"
        ).allowed is True
        # A later replay of a reviewed order never re-fences it, and a repeat
        # acknowledgement returns the first review.
        replay = await sink.record_lifecycle_event(
            client_order_id="alpaca-console:x",
            event=BrokerOrderEvent(event_type="fill", occurred_at_ms=1, price=None, quantity=None),
            event_key="mleg-b|fill",
            order=_owned_order("alpaca-console:x").model_copy(
                update={"order_id": "mleg-b", "side": "None"}
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )
        assert replay == "unfoldable_order"
        assert repo.active_uncertainties() == []
        assert acknowledge_unfoldable_broker_order(
            repo, broker_order_id="mleg-b", operator="someone-else"
        ) == acknowledged
        with pytest.raises(ExternalOrderNotFoundError):
            acknowledge_unfoldable_broker_order(repo, broker_order_id="never-seen", operator="op")
    finally:
        repo.close()


async def test_released_unfoldable_order_returns_the_operator_posture_to_normal(
    tmp_path: Path,
) -> None:
    """The panel/verdict/posture derivations all read active uncertainties;
    once the operator reviews the order they must stop reporting it."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)

    def posture_condition() -> str | None:
        reader = SqliteClerkProjectionReader.from_repository(repo)
        try:
            projection = reader.account_snapshot()
        finally:
            reader.close()
        assert projection is not None
        posture = sqlite_clerk_status(projection).operator_posture
        return None if posture is None else posture.condition.id

    try:
        baseline = posture_condition()
        await _sqlite_sink(repo).record_lifecycle_event(
            client_order_id="alpaca-console:mleg-1",
            event=BrokerOrderEvent(event_type="new", occurred_at_ms=1, price=None, quantity=None),
            event_key="mleg-1|new",
            order=_owned_order("alpaca-console:mleg-1").model_copy(
                update={"order_id": "mleg-1", "side": "None"}
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )
        fenced = posture_condition()
        assert fenced != baseline and fenced is not None and fenced.startswith("alpaca_clerk")

        acknowledge_unfoldable_broker_order(repo, broker_order_id="mleg-1", operator="op-1")

        assert posture_condition() == baseline
    finally:
        repo.close()


def _unfoldable(order_id: str, **update: Any) -> BrokerOrder:
    return _owned_order("alpaca-console:x").model_copy(
        update={"order_id": order_id, "side": "None", **update}
    )


def _unfoldable_evidence_refs(repo: ClerkSqliteRepository) -> list[str] | None:
    episode = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="UNFOLDABLE_BROKER_ORDER", strategy_instance_id=None
    )
    return None if episode is None else json.loads(episode["evidence_refs_json"])


def test_an_acknowledged_unidentified_order_never_whitelists_the_next_one(
    tmp_path: Path,
) -> None:
    """#2363 review: every id-less order collapses to one sentinel identity.

    Before the fix, reviewing the first anonymous order recorded the sentinel
    as reviewed, so every later id-less order -- a different order the
    operator never saw -- appended nothing, left entries open, and dropped
    out of the day-P&L unknown count.
    """
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        record_unfoldable_broker_order(
            repo,
            order=_unfoldable("", client_order_id="anon-1", observed_at_ms=1_000),
            reason="broker order id must be non-empty",
        )
        assert _unfoldable_evidence_refs(repo) == [UNIDENTIFIED_BROKER_ORDER_ID]
        acknowledge_unfoldable_broker_order(
            repo, broker_order_id=UNIDENTIFIED_BROKER_ORDER_ID, operator="op-1"
        )
        assert repo.active_uncertainties() == []

        outcome = record_unfoldable_broker_order(
            repo,
            order=_unfoldable("", client_order_id="anon-2", observed_at_ms=2_000),
            reason="broker order id must be non-empty",
        )

        assert outcome == "raised"
        assert _unfoldable_evidence_refs(repo) == [UNIDENTIFIED_BROKER_ORDER_ID]
        entry = decide_capability(repo, capability=Capability.NEW_EXPOSURE, subject_id="s")
        assert entry.allowed is False and entry.reason_code == "UNFOLDABLE_BROKER_ORDER"
        assert unfoldable_broker_orders_active_since(repo, since_ms=2_000) == 1
    finally:
        repo.close()


def test_new_activity_on_a_reviewed_order_fences_entries_again(tmp_path: Path) -> None:
    """A review covers the order's state the operator saw; a later fill is new evidence.

    An unchanged re-observation (a replay, a sweep of a resting order) stays
    reviewed and appends nothing -- mirroring an acknowledged external row.
    """
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        resting = _unfoldable("mleg-1", status="new", filled_quantity=0.0, observed_at_ms=1_000)
        record_unfoldable_broker_order(repo, order=resting, reason="side must be buy or sell")
        acknowledge_unfoldable_broker_order(repo, broker_order_id="mleg-1", operator="op-1")
        before = len(repo.custody_transitions())

        again = record_unfoldable_broker_order(
            repo, order=resting.model_copy(update={"observed_at_ms": 2_000}), reason="side"
        )
        assert again == "acknowledged" and len(repo.custody_transitions()) == before

        filled = record_unfoldable_broker_order(
            repo,
            order=resting.model_copy(
                update={"status": "filled", "filled_quantity": 1.0, "observed_at_ms": 3_000}
            ),
            reason="side must be buy or sell",
        )

        assert filled == "raised" and _unfoldable_evidence_refs(repo) == ["mleg-1"]
        # A second review of the new state releases it again.
        review = acknowledge_unfoldable_broker_order(
            repo, broker_order_id="mleg-1", operator="op-2"
        )
        assert review.ack_operator == "op-2" and repo.active_uncertainties() == []
    finally:
        repo.close()


_UNFOLDABLE_INDEXES = frozenset(
    {"ix_custody_transitions_resolution_summary", "ix_uncertainties_reason_code"}
)


def test_v16_migration_adds_the_unfoldable_order_indexes(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    conn = repo._conn
    try:
        for index in sorted(_UNFOLDABLE_INDEXES):
            conn.execute(f"DROP INDEX {index}")
        conn.execute("UPDATE control_meta SET schema_version = 15 WHERE id = 1")
        conn.commit()

        schema.migrate_schema(conn, from_version=15)

        assert conn.execute("SELECT schema_version FROM control_meta").fetchone()[0] == 16
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert names >= _UNFOLDABLE_INDEXES
    finally:
        repo.close()


def test_unfoldable_order_reads_probe_their_indexes_never_scan(tmp_path: Path) -> None:
    """#2363 review perf guard: a resting unfoldable order is re-seen every sweep
    under the write coordinator, so its review read must not scan the journal."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    conn = repo._conn
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        reads.unfoldable_broker_order_acknowledgements(conn)
        reads.unfoldable_broker_orders_active_since(
            conn, reason_code="UNFOLDABLE_BROKER_ORDER", since_ms=0
        )
    finally:
        conn.set_trace_callback(None)
    try:
        (review_read,) = [sql for sql in statements if "FROM custody_transitions t" in sql]
        (activity_read,) = [sql for sql in statements if "FROM uncertainties WHERE" in sql]
        for statement, index in (
            (review_read, "ix_custody_transitions_resolution_summary"),
            (activity_read, "ix_uncertainties_reason_code"),
        ):
            plan = " | ".join(
                row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + statement)
            )
            assert index in plan, plan
            assert "SCAN t" not in plan and "SCAN uncertainties" not in plan, plan
    finally:
        repo.close()
