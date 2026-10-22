"""Only accepted price observations can author a chain's display mark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ib_async import Ticker
from ib_async.objects import TickData

from app.broker.ibkr.live.protocol import QuoteUpdate
from app.broker.ibkr.live.quote_callbacks import quote_update
from app.broker.ibkr.live.quotes import QuoteLease
from app.broker.ibkr.market_data import _resolve_market_price

_GOLDEN = Path(__file__).parents[2] / "fixtures/golden/market-data/IB-MARK-001/v1"
_INPUTS = json.loads((_GOLDEN / "input.json").read_text())
_MARKS = json.loads((_GOLDEN / "output.json").read_text())["marks"]


@pytest.mark.parametrize("values,expected", zip(_INPUTS, _MARKS, strict=True))
def test_display_mark_matches_pinned_vendor_selection(values: dict[str, float | None], expected: float | None) -> None:
    update = QuoteUpdate(received_at_ms=1000, data_type=1, ticks=(), values=values, greeks={})
    actual = _resolve_market_price(update)
    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected, abs=1e-9, rel=0)


def test_late_joiners_mark_uses_only_accepted_prices_and_current_empty_sides() -> None:
    lease = QuoteLease(lambda: None)
    ticker = Ticker()
    ticker.bid, ticker.ask, ticker.last = 2.0, 3.0, 2.4
    ticker.bidSize = ticker.askSize = 1.0
    ticker.ticks = [TickData(None, 0, 2.0, 1.0)]
    lease.publish(quote_update(ticker, {}))
    assert all(lease.latest.values[name] is None for name in ("bid", "ask", "last"))
    assert _resolve_market_price(lease.latest) is None
    ticker.ticks = [TickData(None, 1, 2.0, 1.0), TickData(None, 2, 3.0, 1.0)]
    lease.publish(quote_update(ticker, {}))
    assert _resolve_market_price(lease.latest) == 2.5
    ticker.ticks = [TickData(None, 4, 2.4, 1.0)]
    lease.publish(quote_update(ticker, {}))
    assert _resolve_market_price(lease.latest) == 2.4
