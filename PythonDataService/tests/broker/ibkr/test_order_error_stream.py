from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.config import IbkrSettings
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
    client.drain_order_errors = MagicMock(
        return_value=[(42, 201, "Order rejected - insufficient buying power", 1234)]
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
