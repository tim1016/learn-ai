"""Tests for IbkrBrokerAdapter — owned-set cancel scoping + event buffering."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.ibkr import orders as orders_module
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_owner_fence import (
    AccountOwnerWriteFenceError,
    account_owner_write_grant,
)
from app.engine.live.live_portfolio import IbkrBrokerAdapter


def _spec(**overrides) -> IbkrOrderSpec:
    # ADR 0008 / Phase 5B — ``place_paper_order`` refuses a spec without
    # ``order_ref``; the default test spec carries a syntactically valid token.
    base = {
        "symbol": "SPY",
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 1.0,
        "order_type": "MKT",
        "time_in_force": "DAY",
        "confirm_paper": True,
        "order_ref": "learn-ai/test-instance/v1:AAAAAAAAAAAAAAAAAAAAAA",
    }
    base.update(overrides)
    return IbkrOrderSpec(**base)


def _trade_namespace(*, account="DU1234567", order_id, status_str="Submitted"):
    contract = SimpleNamespace(secType="STK", conId=12345, symbol="SPY")
    order = SimpleNamespace(
        account=account,
        orderId=order_id,
        permId=order_id + 1000,
        action="BUY",
        totalQuantity=10.0,
        lmtPrice=0.0,
        tif="DAY",
    )
    order_status = SimpleNamespace(
        status=status_str, filled=0.0, remaining=10.0, avgFillPrice=0.0,
    )
    return SimpleNamespace(
        contract=contract, order=order,
        orderStatus=order_status, fills=[],
    )


@contextmanager
def _owner_grant(boundary: str = "broker.place_order") -> Iterator[None]:
    with account_owner_write_grant(
        account_id="DU1234567",
        owner_generation=7,
        boundary=boundary,
    ):
        yield


def _client(*, owned_open_id: int = 100, foreign_open_id: int = 999):
    settings = IbkrSettings(mode="paper", port=4002, readonly=False, _env_file=None)
    qualified = SimpleNamespace(conId=12345)
    place_order_return = SimpleNamespace(
        order=SimpleNamespace(orderId=owned_open_id, permId=999),
        orderStatus=SimpleNamespace(status="PendingSubmit"),
    )
    open_trades = [
        _trade_namespace(order_id=owned_open_id),
        _trade_namespace(order_id=foreign_open_id),
    ]
    cancel_calls: list[int] = []

    def _cancel_order(order):
        cancel_calls.append(int(order.orderId))

    async def _open_orders():
        return [
            trade
            for trade in open_trades
            if int(trade.order.orderId) not in cancel_calls
        ]

    ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[qualified]),
        placeOrder=MagicMock(return_value=place_order_return),
        reqAllOpenOrdersAsync=AsyncMock(side_effect=_open_orders),
        trades=MagicMock(return_value=open_trades),
        cancelOrder=MagicMock(side_effect=_cancel_order),
    )
    client = SimpleNamespace(
        ib=ib,
        settings=settings,
        connected_account="DU1234567",
        is_connected=lambda: True,
        require_connected=lambda: None,
        require_live=lambda: None,
    )
    return client, cancel_calls


@pytest.fixture(autouse=True)
def _clean_idempotency_cache():
    orders_module._idempotency_clear_for_testing()
    yield
    orders_module._idempotency_clear_for_testing()


@pytest.mark.asyncio
async def test_cancel_open_orders_only_cancels_owned() -> None:
    client, cancel_calls = _client(owned_open_id=100, foreign_open_id=999)
    adapter = IbkrBrokerAdapter(client)

    # Place one order so adapter records 100 as owned.
    with _owner_grant():
        await adapter.place_order(_spec())
    assert 100 in adapter.owned_order_ids
    assert 999 not in adapter.owned_order_ids

    with _owner_grant("broker.cancel_order"):
        cancelled = await adapter.cancel_open_orders()

    assert cancelled == [100]
    assert cancel_calls == [100]
    assert client.ib.reqAllOpenOrdersAsync.await_count == 2


@pytest.mark.asyncio
async def test_real_broker_adapter_refuses_direct_writes_without_account_owner_grant() -> None:
    client, cancel_calls = _client(owned_open_id=100, foreign_open_id=999)
    adapter = IbkrBrokerAdapter(
        client,
        require_account_owner_write_fence=True,
        owner_generation_provider=lambda: 7,
    )

    with pytest.raises(AccountOwnerWriteFenceError) as submit_exc:
        await adapter.place_order(_spec())
    with pytest.raises(AccountOwnerWriteFenceError) as cancel_exc:
        await adapter.cancel_open_orders()

    assert submit_exc.value.reason == "ACCOUNT_OWNER_WRITE_GRANT_MISSING"
    assert cancel_exc.value.reason == "ACCOUNT_OWNER_WRITE_GRANT_MISSING"
    assert client.ib.placeOrder.call_count == 0
    assert cancel_calls == []


@pytest.mark.asyncio
async def test_real_broker_adapter_accepts_matching_account_owner_grant() -> None:
    client, cancel_calls = _client(owned_open_id=100, foreign_open_id=999)
    adapter = IbkrBrokerAdapter(
        client,
        require_account_owner_write_fence=True,
        owner_generation_provider=lambda: 7,
    )

    with account_owner_write_grant(
        account_id="DU1234567",
        owner_generation=7,
        boundary="broker.place_order",
    ):
        await adapter.place_order(_spec())
    with account_owner_write_grant(
        account_id="DU1234567",
        owner_generation=7,
        boundary="broker.cancel_open_orders",
    ):
        cancelled = await adapter.cancel_open_orders()

    assert cancelled == [100]
    assert cancel_calls == [100]


@pytest.mark.asyncio
async def test_real_broker_adapter_refuses_stale_account_owner_grant() -> None:
    client, _ = _client(owned_open_id=100, foreign_open_id=999)
    adapter = IbkrBrokerAdapter(
        client,
        require_account_owner_write_fence=True,
        owner_generation_provider=lambda: 8,
    )

    with pytest.raises(AccountOwnerWriteFenceError) as exc, account_owner_write_grant(
        account_id="DU1234567",
        owner_generation=7,
        boundary="broker.place_order",
    ):
        await adapter.place_order(_spec())

    assert exc.value.reason == "OWNER_GENERATION_STALE_AT_BROKER_WRITE"
    assert client.ib.placeOrder.call_count == 0


@pytest.mark.asyncio
async def test_cancel_open_orders_empty_when_runner_owns_nothing() -> None:
    """A fresh adapter with no placements never cancels foreign orders."""
    client, cancel_calls = _client()
    adapter = IbkrBrokerAdapter(client)

    cancelled = await adapter.cancel_open_orders()

    assert cancelled == []
    assert cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_open_orders_waits_until_owned_order_is_terminal() -> None:
    client, cancel_calls = _client(owned_open_id=100, foreign_open_id=999)
    adapter = IbkrBrokerAdapter(client)
    with _owner_grant():
        await adapter.place_order(_spec())

    terminal = asyncio.Event()
    open_trades = list(await client.ib.reqAllOpenOrdersAsync())

    async def _open_orders():
        if not terminal.is_set():
            return open_trades
        return [trade for trade in open_trades if int(trade.order.orderId) != 100]

    client.ib.reqAllOpenOrdersAsync = AsyncMock(side_effect=_open_orders)
    with _owner_grant("broker.cancel_order"):
        cancel_task = asyncio.create_task(adapter.cancel_open_orders())

    await asyncio.sleep(0.01)
    assert cancel_calls == [100]
    assert not cancel_task.done()

    terminal.set()
    assert await asyncio.wait_for(cancel_task, timeout=0.2) == [100]


@pytest.mark.asyncio
async def test_cancel_open_orders_waits_until_terminal_fill_reaches_buffer() -> None:
    client, _ = _client(owned_open_id=100, foreign_open_id=999)
    adapter = IbkrBrokerAdapter(client)
    with _owner_grant():
        await adapter.place_order(_spec())
    owned_trade = next(
        trade for trade in client.ib.trades() if int(trade.order.orderId) == 100
    )
    owned_trade.fills = [SimpleNamespace()]
    adapter._event_task = asyncio.current_task()

    with _owner_grant("broker.cancel_order"):
        cancel_task = asyncio.create_task(adapter.cancel_open_orders())
    await asyncio.sleep(0.01)
    assert not cancel_task.done()

    adapter._observed_fill_count_by_order_id[100] = 1
    adapter._event_buffer_changed.set()
    assert await asyncio.wait_for(cancel_task, timeout=0.2) == [100]
    adapter._event_task = None


@pytest.mark.asyncio
async def test_event_stream_buffers_all_fills_including_foreign() -> None:
    """The streaming task buffers ALL fills, including ones placed by
    other clients on the same DU account.

    Spec § 7 requires this: 'Persist all received executions to
    executions.parquet whether or not Python originated them,
    regardless of clientId'. The previous adapter-level filter
    dropped foreigns entirely, defeating the outside-mutation halt
    check that needs to see them. Downstream ownership filtering
    for the engine's portfolio-update path lives in
    LiveEngine._convert_ibkr_fill — see the comment in
    IbkrBrokerAdapter._run_event_stream.
    """
    client, _ = _client(owned_open_id=100)
    adapter = IbkrBrokerAdapter(client)
    with _owner_grant():
        await adapter.place_order(_spec())

    # Replace stream_order_events with a controllable async generator.
    queued: asyncio.Queue[IbkrOrderEvent | None] = asyncio.Queue()

    async def _fake_stream(*_args, **_kwargs) -> AsyncIterator[IbkrOrderEvent]:
        while True:
            event = await queued.get()
            if event is None:
                return
            yield event

    import app.engine.live.live_portfolio as live_portfolio_module

    original = live_portfolio_module.stream_order_events
    live_portfolio_module.stream_order_events = _fake_stream
    try:
        await adapter.start_event_stream()

        owned = IbkrOrderEvent(
            account_id="DU1234567",
            order_id=100,
            event_type="fill",
            status="Filled",
            exec_id="exec-owned-1",
            client_id=42,
            fill_quantity=10.0,
            avg_fill_price=500.0,
            last_fill_price=500.0,
            cumulative_filled=10.0,
            remaining=0.0,
            ts_ms=1,
        )
        foreign = IbkrOrderEvent(
            account_id="DU1234567",
            order_id=999,  # not in adapter.owned_order_ids
            event_type="fill",
            status="Filled",
            exec_id="exec-foreign-1",
            client_id=0,  # manual TWS click, different clientId
            fill_quantity=5.0,
            avg_fill_price=499.0,
            last_fill_price=499.0,
            cumulative_filled=5.0,
            remaining=0.0,
            ts_ms=2,
        )
        await queued.put(owned)
        await queued.put(foreign)

        # Wait until both events have landed in the buffer.
        for _ in range(200):
            if len(adapter._event_buffer) >= 2:
                break
            await asyncio.sleep(0.01)

        drained = adapter.drain_broker_events()
        # Both are kept now — § 7 requirement.
        order_ids = [e.order_id for e in drained]
        assert 100 in order_ids
        assert 999 in order_ids
        # exec_id and client_id round-trip through the new model fields.
        foreign_drained = next(e for e in drained if e.order_id == 999)
        assert foreign_drained.exec_id == "exec-foreign-1"
        assert foreign_drained.client_id == 0
    finally:
        await adapter.stop_event_stream()
        live_portfolio_module.stream_order_events = original


@pytest.mark.asyncio
async def test_event_stream_invokes_callback_sink_before_buffer_drain() -> None:
    client, _ = _client(owned_open_id=100)
    adapter = IbkrBrokerAdapter(client)
    captured: list[IbkrOrderEvent] = []
    adapter.set_broker_callback_sink(captured.append)

    queued: asyncio.Queue[IbkrOrderEvent | None] = asyncio.Queue()

    async def _fake_stream(*_args, **_kwargs) -> AsyncIterator[IbkrOrderEvent]:
        while True:
            event = await queued.get()
            if event is None:
                return
            yield event

    import app.engine.live.live_portfolio as live_portfolio_module

    original = live_portfolio_module.stream_order_events
    live_portfolio_module.stream_order_events = _fake_stream
    try:
        await adapter.start_event_stream()

        event = IbkrOrderEvent(
            account_id="DU1234567",
            order_id=100,
            event_type="fill",
            status="Filled",
            exec_id="exec-owned-1",
            fill_quantity=10.0,
            avg_fill_price=500.0,
            last_fill_price=500.0,
            cumulative_filled=10.0,
            remaining=0.0,
            ts_ms=1,
        )
        await queued.put(event)

        for _ in range(200):
            if captured:
                break
            await asyncio.sleep(0.01)

        assert captured == [event]
        assert adapter.drain_broker_events() == [event]
    finally:
        await adapter.stop_event_stream()
        live_portfolio_module.stream_order_events = original
