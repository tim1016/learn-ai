"""Requirement traceability for #1045's durable Account Clerk cursor lane.

| Requirement | Test |
| --- | --- |
| Ordered real-socket journal drain | ``test_drain_events_real_unix_socket_returns_ordered_journal_rows`` |
| Full identity / stale-consumer rejection | ``test_drain_events_rejects_stale_account_namespace_and_run`` |
| Crash before cursor acknowledges at least once | ``test_crash_after_bot_wal_before_cursor_redelivers_without_duplicate_fill_effect`` |
| Restart resumes after the durable cursor | ``test_restarted_bot_resumes_after_durable_cursor`` |
| Empty drain and no inactive relay cache | ``test_empty_drain_does_not_mutate_cursor_or_existing_journal_or_cache_consumers`` |
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.execution.order import Direction, OrderEvent
from app.engine.live.account_artifacts import advance_account_clerk_generation
from app.engine.live.account_clerk import AccountClerk, AccountClerkRpcClient, AccountClerkRpcServer
from app.engine.live.account_clerk_cursor import (
    AccountClerkEventConsumerIdentity,
    AccountClerkEventCursorRepo,
)
from app.engine.live.account_clerk_rpc import AccountClerkRpcRejectedError
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.broker_callbacks import BrokerCallbackWal
from app.engine.live.live_portfolio import IbkrBrokerAdapter, LivePortfolio
from app.engine.live.order_identity import build_order_ref
from tests.engine.live.fixtures.fake_broker import FakeBroker

ACCOUNT = "DU1045"
RUN_ID = "run-1045"
INSTANCE_ID = "bot-1045"
NAMESPACE = bot_order_namespace_for_instance(INSTANCE_ID)
START_MS = 1_784_000_000_000


def _active_generation(tmp_path: Path) -> int:
    return advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation


def _active_binding(tmp_path: Path) -> None:
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id=INSTANCE_ID,
            run_id=RUN_ID,
            bot_order_namespace=NAMESPACE,
            lifecycle_state="ACTIVE",
            recorded_at_ms=START_MS,
            source="test",
        ),
    )


def _intent(intent_id: str) -> AccountOwnerSubmitIntent:
    order_ref = build_order_ref(NAMESPACE, intent_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id=ACCOUNT,
        strategy_instance_id=INSTANCE_ID,
        run_id=RUN_ID,
        bot_order_namespace=NAMESPACE,
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
            client_order_id=f"client-{intent_id}",
            order_ref=order_ref,
        ).model_dump(),
        owner_generation=1,
        created_at_ms=START_MS,
    )


def _fill(intent: AccountOwnerSubmitIntent, *, exec_id: str, ts_ms: int) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id=ACCOUNT,
        order_id=101,
        perm_id=201,
        event_type="fill",
        order_ref=intent.order_ref,
        symbol="SPY",
        side="BUY",
        fill_quantity=1,
        avg_fill_price=100,
        last_fill_price=100,
        exec_id=exec_id,
        exec_time_ms=ts_ms,
        ts_ms=ts_ms,
    )


def _consumer() -> AccountClerkEventConsumerIdentity:
    return AccountClerkEventConsumerIdentity(
        account_id=ACCOUNT,
        strategy_instance_id=INSTANCE_ID,
        run_id=RUN_ID,
        bot_order_namespace=NAMESPACE,
    )


class _CrashBeforeCursorDelivery:
    """Inject a process-stop boundary after the bot callback WAL fsync."""

    def __init__(self, *, journal_seq: int, event: IbkrOrderEvent) -> None:
        self.journal_seq = journal_seq
        self.event = event

    def acknowledge_after_durable_event_write(self) -> bool:
        raise RuntimeError("simulated process crash before cursor acknowledgement")


async def _server(tmp_path: Path) -> tuple[AccountClerk, AccountClerkRpcServer]:
    _active_binding(tmp_path)
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        clerk_generation=_active_generation(tmp_path),
    )
    server = AccountClerkRpcServer(clerk)
    await server.start()
    return clerk, server


@pytest.mark.asyncio
async def test_drain_events_real_unix_socket_returns_ordered_journal_rows(tmp_path: Path) -> None:
    clerk, server = await _server(tmp_path)
    first = _intent("intent-1")
    second = _intent("intent-2")
    try:
        await clerk.record_intent(first)
        await clerk.record_broker_event(_fill(first, exec_id="exec-1", ts_ms=START_MS + 1))
        await clerk.record_intent(second)
        await clerk.record_broker_event(_fill(second, exec_id="exec-2", ts_ms=START_MS + 2))
        journal_before = (tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl").read_bytes()
        cursor = AccountClerkEventCursorRepo(tmp_path / "run")
        deliveries = await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).drain_events(
            after_seq=0,
            consumer=_consumer(),
            cursor=cursor,
        )
    finally:
        await server.close()

    # S6 records the Clerk's broker-unavailable startup recovery at sequence 1.
    assert [delivery.journal_seq for delivery in deliveries] == [3, 5]
    assert [delivery.event.exec_id for delivery in deliveries] == ["exec-1", "exec-2"]
    assert cursor.last_journal_seq(_consumer()) == 0
    assert (tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl").read_bytes() == journal_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes, expected_reason",
    [
        ({"account_id": "DU999999"}, "EVENT_CONSUMER_ACCOUNT_MISMATCH"),
        ({"bot_order_namespace": "learn-ai/other/v1"}, "STALE_EVENT_CONSUMER"),
        ({"run_id": "stale-run"}, "STALE_EVENT_CONSUMER"),
    ],
)
async def test_drain_events_rejects_stale_account_namespace_and_run(
    tmp_path: Path,
    changes: dict[str, str],
    expected_reason: str,
) -> None:
    _clerk, server = await _server(tmp_path)
    try:
        request = {
            "operation": "drain_events",
            "after_seq": 0,
            **_consumer().model_dump(mode="json"),
            **changes,
        }
        client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
        with pytest.raises(AccountClerkRpcRejectedError) as exc:
            await client._request(request)
    finally:
        await server.close()

    assert exc.value.reason == expected_reason


@pytest.mark.asyncio
async def test_crash_after_bot_wal_before_cursor_redelivers_without_duplicate_fill_effect(tmp_path: Path) -> None:
    clerk, server = await _server(tmp_path)
    intent = _intent("intent-crash")
    run_dir = tmp_path / "run"
    cursor = AccountClerkEventCursorRepo(run_dir)
    try:
        await clerk.record_intent(intent)
        await clerk.record_broker_event(_fill(intent, exec_id="exec-crash", ts_ms=START_MS + 1))
        client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
        first_delivery = (
            await client.drain_events(after_seq=0, consumer=_consumer(), cursor=cursor)
        )[0]

        callback_wal = BrokerCallbackWal(run_dir / "broker_callbacks.jsonl")
        first_adapter = IbkrBrokerAdapter(SimpleNamespace())
        first_adapter.set_account_clerk_delivery_sink(
            lambda event, journal_seq: callback_wal.append_account_clerk_event(
                event,
                journal_seq=journal_seq,
            )
        )
        with pytest.raises(RuntimeError, match="simulated process crash"):
            first_adapter._record_clerk_delivery(
                _CrashBeforeCursorDelivery(
                    journal_seq=first_delivery.journal_seq,
                    event=first_delivery.event,
                )
            )
        # The first bot wrote and applied its callback before it crashed; its
        # cursor did not move because the crash boundary follows the fsync.
        first_events = first_adapter.drain_broker_events()
        portfolio = LivePortfolio(FakeBroker())
        for event in first_events:
            portfolio.record_broker_fill(
                OrderEvent(
                    order_id=event.order_id,
                    symbol=event.symbol or "SPY",
                    time=datetime.fromtimestamp(event.ts_ms / 1000, tz=UTC),
                    fill_price=Decimal(str(event.last_fill_price or event.avg_fill_price)),
                    fill_quantity=int(event.fill_quantity or 0),
                    direction=Direction.LONG,
                    fee=Decimal("0"),
                )
            )
        assert cursor.last_journal_seq(_consumer()) == 0

        restarted_cursor = AccountClerkEventCursorRepo(run_dir)
        redelivery = (
            await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).drain_events(
                after_seq=0,
                consumer=_consumer(),
                cursor=restarted_cursor,
            )
        )[0]
        assert redelivery.journal_seq == first_delivery.journal_seq

        adapter = IbkrBrokerAdapter(SimpleNamespace())
        adapter.set_account_clerk_delivery_sink(
            lambda event, journal_seq: callback_wal.append_account_clerk_event(
                event,
                journal_seq=journal_seq,
            )
        )
        adapter._record_clerk_delivery(redelivery)
        assert adapter.drain_broker_events() == []
        assert restarted_cursor.last_journal_seq(_consumer()) == redelivery.journal_seq
        # The redelivery was not added to the broker buffer, so it cannot
        # apply a second fill effect to the local portfolio.
        assert portfolio.get_position("SPY").quantity == 1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_restarted_bot_resumes_after_durable_cursor(tmp_path: Path) -> None:
    clerk, server = await _server(tmp_path)
    first = _intent("intent-resume-1")
    second = _intent("intent-resume-2")
    run_dir = tmp_path / "run"
    cursor = AccountClerkEventCursorRepo(run_dir)
    try:
        await clerk.record_intent(first)
        await clerk.record_broker_event(_fill(first, exec_id="exec-resume-1", ts_ms=START_MS + 1))
        await clerk.record_intent(second)
        await clerk.record_broker_event(_fill(second, exec_id="exec-resume-2", ts_ms=START_MS + 2))
        client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
        first_delivery = (
            await client.drain_events(after_seq=0, consumer=_consumer(), cursor=cursor)
        )[0]

        callback_wal = BrokerCallbackWal(run_dir / "broker_callbacks.jsonl")
        assert callback_wal.append_account_clerk_event(
            first_delivery.event,
            journal_seq=first_delivery.journal_seq,
        )
        assert first_delivery.acknowledge_after_durable_event_write()

        restarted_cursor = AccountClerkEventCursorRepo(run_dir)
        resumed_deliveries = await AccountClerkRpcClient(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
        ).drain_events(
            after_seq=restarted_cursor.last_journal_seq(_consumer()),
            consumer=_consumer(),
            cursor=restarted_cursor,
        )
    finally:
        await server.close()

    assert [delivery.journal_seq for delivery in resumed_deliveries] == [5]
    assert [delivery.event.exec_id for delivery in resumed_deliveries] == ["exec-resume-2"]


@pytest.mark.asyncio
async def test_empty_drain_does_not_mutate_cursor_or_existing_journal_or_cache_consumers(
    tmp_path: Path,
) -> None:
    _clerk, server = await _server(tmp_path)
    cursor = AccountClerkEventCursorRepo(tmp_path / "run")
    journal_path = tmp_path / "accounts" / ACCOUNT / "clerk_journal.jsonl"
    journal_before = journal_path.read_bytes()
    try:
        deliveries = await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).drain_events(
            after_seq=0,
            consumer=_consumer(),
            cursor=cursor,
        )
    finally:
        await server.close()

    assert deliveries == []
    assert not cursor.path.exists()
    assert journal_path.read_bytes() == journal_before
    assert not hasattr(server, "_events_by_namespace")
