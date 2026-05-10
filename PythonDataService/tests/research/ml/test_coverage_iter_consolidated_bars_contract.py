"""Regression test: iter_consolidated_bars contract on data_source.

``iter_consolidated_bars`` calls ``data_source.iter_bars(...)`` once to
harvest consolidated bars for the coverage check; the runner then calls
the same method again to drive the engine. The documented contract is:
every ``iter_bars`` call must return a FRESH iterator.

This test pins the contract using a minimal stub. A stateful single-use
iterator would cause the second call to yield zero bars, silently
breaking either the coverage check or the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.research.ml.coverage import iter_consolidated_bars

NY = ZoneInfo("America/New_York")

_SYMBOL = "SPY"
_START = date(2024, 5, 1)
_END = date(2024, 5, 2)


def _minute_bar(t: datetime, close: float = 100.0) -> TradeBar:
    """Minimal TradeBar for testing — only end_time and close are used."""
    price = Decimal(str(close))
    return TradeBar(
        symbol=_SYMBOL,
        time=t - timedelta(minutes=1),
        end_time=t,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
    )


@dataclass
class _FreshIteratorDataSource:
    """Stub data source that returns a fresh generator on every iter_bars call.

    Tracks call count so the test can verify the contract is honoured by
    the caller (i.e. each call truly creates a new generator).
    """

    _bars: list[TradeBar]
    call_count: int = 0

    def iter_bars(self, symbol: str, start: date, end: date):
        self.call_count += 1
        yield from self._bars


def test_iter_bars_returns_fresh_iterator_per_call() -> None:
    """Documented contract: data sources must return a fresh generator on
    each iter_bars call. Consuming one iterator must not exhaust subsequent
    calls.
    """
    # Build 30 minute bars (9:31..10:00 ET). The 15-min consolidator fires
    # at 9:45 and 10:00, yielding 2 complete consolidated bars per call.
    base = datetime(2024, 5, 1, 9, 31, tzinfo=NY)
    minute_bars = [_minute_bar(base + timedelta(minutes=i)) for i in range(30)]

    source = _FreshIteratorDataSource(_bars=minute_bars)

    # First call: consume the consolidated stream.
    first_call = list(
        iter_consolidated_bars(
            source,
            symbol=_SYMBOL,
            start_date=_START,
            end_date=_END,
            resolution_minutes=15,
        )
    )
    assert len(first_call) >= 1, f"expected at least 1 consolidated bar, got {len(first_call)}"

    # Second call: a fresh generator must be returned; consuming it yields
    # the same number of bars as the first call.
    second_call = list(
        iter_consolidated_bars(
            source,
            symbol=_SYMBOL,
            start_date=_START,
            end_date=_END,
            resolution_minutes=15,
        )
    )
    assert len(second_call) == len(first_call), (
        f"second call yielded {len(second_call)} bars instead of {len(first_call)}; "
        "data source may be reusing a single exhausted iterator"
    )

    # The stub was called twice — confirming iter_consolidated_bars invokes
    # iter_bars exactly once per call (not zero, not more).
    assert source.call_count == 2
