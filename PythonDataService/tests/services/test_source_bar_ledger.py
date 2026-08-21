"""Durable source-bar and synthetic-price boundaries for Dry Run."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SourceBarConflictError, SourceBarLedger


def _bar(*, close: str = "501.25") -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=1_700_000_000_000,
        end_ms=1_700_000_060_000,
        open=Decimal("500"),
        high=Decimal("502"),
        low=Decimal("499"),
        close=Decimal(close),
        volume=42,
        fetched_at_ms=1_700_000_061_000,
        feed_id="polygon-minute",
        session_phase="RTH",
    )


def test_source_bar_ledger_is_idempotent_but_refuses_revised_identity(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    first = ledger.append(_bar())
    replay = ledger.append(_bar())

    assert replay == first
    assert len(ledger.bars(provider="polygon-minute", symbol="SPY")) == 1
    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_IDENTITY_CONFLICT"):
        ledger.append(_bar(close="502.25"))


@pytest.mark.asyncio
async def test_synthetic_port_fills_only_from_the_retained_decision_bar_and_recovers(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar())
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    order = await broker.submit(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        client_order_id="bot:ema:enter",
    )

    assert order.status == "filled"
    assert order.filled_avg_price == float(retained.close)
    assert order.filled_at_ms == retained.end_ms
    restarted = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    recovered = await restarted.get_order_by_client_order_id("bot:ema:enter")
    assert recovered == order
    assert (await restarted.list_positions())[0].quantity == 2
