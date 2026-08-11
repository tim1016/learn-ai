"""Authority-boundary tests for Alpaca trade-update evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

ACCOUNT_ID = "PA-TEST"
STRATEGY_INSTANCE_ID = "spy-bot"
RUN_ID = "run-1"


class _ReadWithoutLegacyActivities:
    def __init__(self) -> None:
        self.activity_reads = 0

    async def list_activities(self, **_kwargs: Any) -> list:
        self.activity_reads += 1
        raise AssertionError("activated SQLite must not write legacy activity evidence")


class _NoBrokerMutation:
    async def submit(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("evidence recovery must not submit")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("evidence recovery must not cancel")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


class _EvidenceSink:
    def __init__(self) -> None:
        self.consumer: TradeUpdatesConsumer | None = None
        self.gap_connection_states: list[bool] = []
        self.activity_recoveries = 0

    async def record_lifecycle_event(self, **_kwargs: Any) -> ClerkEntryKind:
        return ClerkEntryKind.ORDER_EVENT

    async def recover_activity_window(self, **_kwargs: Any) -> int:
        self.activity_recoveries += 1
        return 0

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
        read=cast(BrokerReadPort, _ReadWithoutLegacyActivities()),
        trade=cast(BrokerTradePort, _NoBrokerMutation()),
        intake=ReentrantAsyncLock(),
    )


def _authorization_source() -> AsyncIterator[bytes | str]:
    async def _frames() -> AsyncIterator[bytes | str]:
        yield '{"stream":"authorization","data":{"status":"authorized"}}'

    return _frames()


async def test_sqlite_activity_recovery_never_reads_or_writes_legacy_window(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    read = _ReadWithoutLegacyActivities()
    read_port = cast(BrokerReadPort, read)
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo,
        read=read_port,
        trade=cast(BrokerTradePort, _NoBrokerMutation()),
        intake=ReentrantAsyncLock(),
    )

    recorded = await sink.recover_activity_window(read=read_port, limit=100)

    assert recorded == 0
    assert read.activity_reads == 0
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


async def test_sqlite_late_exact_execution_after_recovery_is_fenced_across_restart(
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
        assert fills[0]["evidence_source"] == "cumulative_recovery"
        assert repo.position(STRATEGY_INSTANCE_ID, "SPY") == 5.0
        transition_kinds = [
            transition["transition_kind"] for transition in repo.transitions_for_order(order_ref)
        ]
        assert "EXECUTION_SLICE_FILLED" not in transition_kinds
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
        admission = decide_capability(
            restarted,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id=STRATEGY_INSTANCE_ID,
        )
        assert admission.allowed is False
        assert admission.reason_code == EXECUTION_COVERAGE_CONFLICT_REASON_CODE
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
    assert sink.activity_recoveries == 1


async def _no_backoff() -> None:
    return
