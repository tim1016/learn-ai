from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.order_error_stream import OrderErrorEvent
from app.broker.ibkr.orders import stream_order_events


def _client() -> SimpleNamespace:
    ib = SimpleNamespace(trades=MagicMock())
    return SimpleNamespace(
        ib=ib,
        settings=IbkrSettings(mode="paper", port=4002, _env_file=None),
        connected_account="DU1234567",
        require_connected=lambda: None,
        require_live=lambda: None,
    )


def _trade(*, order_ref: str) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(secType="STK", conId=12345, symbol="SPY"),
        order=SimpleNamespace(
            account="",
            orderId=42,
            permId=999,
            action="BUY",
            totalQuantity=10.0,
            orderType="MKT",
            orderRef=order_ref,
        ),
        orderStatus=SimpleNamespace(
            status="Inactive",
            filled=0.0,
            remaining=10.0,
            avgFillPrice=0.0,
        ),
        fills=[],
    )


@pytest.mark.asyncio
async def test_stream_order_events_emits_ibkr_rejection_error_callback() -> None:
    order_ref = "learn-ai/sid-pub-test/v1:intent-pub-1"
    client = _client()
    client.ib.trades = MagicMock(return_value=[_trade(order_ref=order_ref)])
    client.order_errors_after = MagicMock(
        return_value=[
            OrderErrorEvent(
                seq=1,
                req_id=42,
                error_code=201,
                error_message="Order rejected - insufficient buying power",
                ts_ms=1234,
            )
        ]
    )

    out = []
    async for event in stream_order_events(client, poll_seconds=0.001):
        out.append(event)
        if len(out) >= 1:
            break

    event = out[0]
    assert event.event_type == "error"
    assert event.req_id == 42
    assert event.order_id == 42
    assert event.order_ref == order_ref
    assert event.error_code == 201
    assert event.error_message == "Order rejected - insufficient buying power"
    assert event.symbol == "SPY"
    assert event.side == "BUY"
    assert event.order_type == "MKT"


@pytest.mark.asyncio
async def test_stream_order_events_replays_error_to_each_iterator() -> None:
    order_ref = "learn-ai/sid-pub-test/v1:intent-pub-1"
    client = _client()
    client.ib.trades = MagicMock(return_value=[_trade(order_ref=order_ref)])
    buffered = [
        OrderErrorEvent(
            seq=1,
            req_id=42,
            error_code=201,
            error_message="Order rejected - insufficient buying power",
            ts_ms=1234,
        )
    ]
    client.order_errors_after = lambda seq: [event for event in buffered if event.seq > seq]

    first = stream_order_events(client, poll_seconds=0.001)
    second = stream_order_events(client, poll_seconds=0.001)
    try:
        first_event = await anext(first)
        second_event = await anext(second)
    finally:
        await first.aclose()
        await second.aclose()

    assert first_event.event_type == "error"
    assert second_event.event_type == "error"
    assert first_event.req_id == second_event.req_id == 42
