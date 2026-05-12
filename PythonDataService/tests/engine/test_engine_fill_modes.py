"""End-to-end engine + consolidator + minute-stream tests for FillMode
dispatch — particularly the NEXT_SESSION_OPEN immediate-fill invariant
that produces QC trade-by-trade parity in the daily-consolidator-over-
minute-stream pattern.

These are the regression guards for the engine main loop's step ordering
(pending-fills → consolidator-fire → order-drain). Any future refactor
that re-orders those steps will surface here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode
from app.engine.strategy.base import Strategy

NY = ZoneInfo("America/New_York")


def _minute(date_: date, hour: int, minute: int, *, open_: str, high: str, low: str, close: str) -> TradeBar:
    start = datetime(date_.year, date_.month, date_.day, hour, minute, tzinfo=NY)
    return TradeBar(
        symbol="AAPL",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


class _SyntheticStream:
    """data_source.iter_bars contract: returns a fresh iterator each call."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start_date: date, end_date: date) -> Iterator[TradeBar]:
        return iter(self._bars)


class _EmitOnceStrategy(Strategy):
    """Submits one set_holdings(1.0) on the first consolidated bar it sees;
    records the consolidated bar's end_time and every order event."""

    def __init__(self) -> None:
        super().__init__()
        self.signal_bar_end_time: datetime | None = None
        self.events: list = []

    def initialize(self) -> None:
        self.set_start_date(2026, 2, 9)
        self.set_end_date(2026, 2, 12)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity("AAPL")
        self._symbol = symbol
        self.ctx.register_consolidator(symbol, timedelta(minutes=1440), self._on_daily)

    def _on_daily(self, bar: TradeBar) -> None:
        if self.signal_bar_end_time is None:
            self.signal_bar_end_time = bar.end_time
            assert self.ctx is not None
            self.ctx.set_holdings(self._symbol, Decimal("1.0"))

    def on_order_event(self, event) -> None:
        self.events.append(event)


def _two_day_stream() -> list[TradeBar]:
    """Two trading days, three minute bars each — enough to consolidate
    day-1's daily bar on day-2's first minute and for the day-2 09:30 bar
    to be both (a) the consolidator-fire iteration and (b) the first
    eligible NEXT_SESSION_OPEN candidate."""
    d1 = date(2026, 2, 9)
    d2 = date(2026, 2, 10)
    return [
        _minute(d1, 9, 30, open_="100.0", high="100.2", low="99.9", close="100.1"),
        _minute(d1, 12, 0, open_="100.1", high="100.3", low="100.0", close="100.2"),
        _minute(d1, 15, 59, open_="100.2", high="100.4", low="100.0", close="100.3"),
        _minute(d2, 9, 30, open_="102.0", high="102.5", low="101.8", close="102.2"),
        _minute(d2, 9, 31, open_="102.2", high="102.6", low="102.0", close="102.4"),
        _minute(d2, 15, 59, open_="102.4", high="102.7", low="102.3", close="102.5"),
    ]


def test_next_session_open_fills_at_first_eligible_minute_open() -> None:
    """THE invariant: when the daily consolidator fires day-1's bar during
    processing of day-2's first minute, NEXT_SESSION_OPEN fills against
    THAT minute_bar (not the next iteration's bar). Fill price = open of
    day-2 09:30; fill time = day-2 09:30 NY.

    This is the (R8) regression guard for the engine main loop's
    step-ordering invariant: a refactor that re-ordered Step 3
    (pending-fills) and Step 5 (order-drain) would either fill one bar
    early or one bar late."""
    stream = _SyntheticStream(_two_day_stream())
    strategy = _EmitOnceStrategy()
    engine = BacktestEngine(
        data_source=stream,
        fill_model=FillModel(
            mode=FillMode.NEXT_SESSION_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )

    engine.run(strategy)

    assert strategy.signal_bar_end_time is not None
    # Daily consolidated bar's end_time anchors to the last contained minute:
    # day-1 15:59→16:00.
    assert strategy.signal_bar_end_time == datetime(2026, 2, 9, 16, 0, tzinfo=NY)
    assert len(strategy.events) == 1
    event = strategy.events[0]
    assert event.direction is Direction.LONG
    # Fill time = next_bar.time = day-2 09:30 NY (start of the [09:30, 09:31) bar).
    assert event.time == datetime(2026, 2, 10, 9, 30, tzinfo=NY)
    # Fill price = open of day-2 [09:30, 09:31) = 102.0.
    assert event.fill_price == Decimal("102.0")


def test_next_session_open_same_date_signal_stays_pending_until_session_boundary() -> None:
    """Defensive: if (for some hypothetical configuration) the consolidator
    fires day-1's bar on a SAME-DAY minute (date == signal.date), the order
    must stay deferred across subsequent same-day minutes and fill only at
    the first later-date bar. Catches a regression where the immediate-fill
    branch wrongly accepts same-date candidates."""

    d1 = date(2026, 2, 9)
    d2 = date(2026, 2, 10)
    bars = [
        _minute(d1, 9, 30, open_="100.0", high="100.2", low="99.9", close="100.1"),
        _minute(d1, 9, 31, open_="100.1", high="100.3", low="100.0", close="100.2"),
        _minute(d1, 9, 32, open_="100.2", high="100.4", low="100.0", close="100.3"),
        _minute(d2, 9, 30, open_="102.0", high="102.5", low="101.8", close="102.2"),
    ]

    class _OneMinStrategy(Strategy):
        def __init__(self) -> None:
            super().__init__()
            self.events: list = []
            self._fired = False

        def initialize(self) -> None:
            self.set_start_date(2026, 2, 9)
            self.set_end_date(2026, 2, 12)
            self.set_cash(100_000)
            assert self.ctx is not None
            symbol = self.ctx.add_equity("AAPL")
            self._symbol = symbol
            self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_min)

        def _on_min(self, bar: TradeBar) -> None:
            if not self._fired:
                self._fired = True
                assert self.ctx is not None
                self.ctx.set_holdings(self._symbol, Decimal("1.0"))

        def on_order_event(self, event) -> None:
            self.events.append(event)

    strategy = _OneMinStrategy()
    engine = BacktestEngine(
        data_source=_SyntheticStream(bars),
        fill_model=FillModel(
            mode=FillMode.NEXT_SESSION_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )
    engine.run(strategy)

    # Order stays pending through day-1's 9:31 and 9:32, fills at day-2 09:30 open.
    assert len(strategy.events) == 1
    assert strategy.events[0].time == datetime(2026, 2, 10, 9, 30, tzinfo=NY)
    assert strategy.events[0].fill_price == Decimal("102.0")


def test_next_bar_open_keeps_existing_defer_behavior_on_same_stream() -> None:
    """Regression: NEXT_BAR_OPEN must NOT acquire the immediate-fill
    behavior. With the same two-day stream, NEXT_BAR_OPEN fills on the
    minute AFTER the consolidator-fire iteration (day-2 09:31, not 09:30).
    Catches a regression where the Step 5 NEXT_BAR_OPEN branch
    accidentally inherited the immediate-fill optimization."""
    stream = _SyntheticStream(_two_day_stream())
    strategy = _EmitOnceStrategy()
    engine = BacktestEngine(
        data_source=stream,
        fill_model=FillModel(
            mode=FillMode.NEXT_BAR_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )

    engine.run(strategy)

    assert len(strategy.events) == 1
    # NEXT_BAR_OPEN fills on the bar AFTER the consolidator-fire iteration:
    # consolidator fires on day-2 09:30 bar (queues order); fill happens on
    # day-2 09:31 bar's open.
    assert strategy.events[0].time == datetime(2026, 2, 10, 9, 31, tzinfo=NY)
    assert strategy.events[0].fill_price == Decimal("102.2")
