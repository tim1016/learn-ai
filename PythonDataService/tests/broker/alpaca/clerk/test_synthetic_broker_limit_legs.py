"""Synthetic-broker limit legs are honest about ``order_type``/price and cancel a non-marketable rest (Ruling R9)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SourceBarLedger


def _bar(*, start_ms: int = 1_700_000_000_000) -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("100.00"),
        high=Decimal("100.00"),
        low=Decimal("100.00"),
        close=Decimal("100.00"),
        volume=1,
        fetched_at_ms=start_ms + 60_000,
        feed_id="polygon-minute",
        session_phase="RTH",
    )


@pytest.mark.asyncio
async def test_a_marketable_limit_buy_fills_honestly_at_the_bar_close(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar(), run_id="run-a")
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    broker.bind_evaluated_bar("bot:ema:enter", retained)

    order = await broker.submit(
        BrokerOrderLeg(
            symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=100.10, extended_hours=True
        ),
        client_order_id="bot:ema:enter",
    )

    assert order.status == "filled"
    assert order.filled_avg_price == 100.0
    assert order.order_type == "limit"
    assert order.limit_price == 100.10
    assert order.time_in_force == "day"
    assert order.extended_hours is True


@pytest.mark.asyncio
async def test_a_non_marketable_limit_buy_is_canceled_immediately_with_no_fill(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar(), run_id="run-a")
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    broker.bind_evaluated_bar("bot:ema:enter", retained)

    order = await broker.submit(
        BrokerOrderLeg(
            symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=99.50, extended_hours=True
        ),
        client_order_id="bot:ema:enter",
    )

    assert order.status == "canceled"
    assert order.filled_quantity == 0
    assert order.canceled_at_ms == retained.end_ms
    assert order.events == []


@pytest.mark.asyncio
async def test_a_market_buy_is_unchanged(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar(), run_id="run-a")
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    broker.bind_evaluated_bar("bot:ema:enter", retained)

    order = await broker.submit(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        client_order_id="bot:ema:enter",
    )

    assert order.order_type == "market"
    assert order.status == "filled"
    assert order.filled_avg_price == float(retained.close)
