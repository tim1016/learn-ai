"""End-to-end-lite tests for engine-managed TP/SL brackets.

Synthesizes a sequence of 1-minute bars, feeds them through the engine
with a toy strategy that enters long on its first bar (with TP/SL
attached), and asserts the bracket fires at the correct bar with the
correct outcome per the pessimistic resolver.

Three scenarios:

* Both TP and SL inside the trigger bar's range → SL wins (pessimistic).
* Only TP in range → TP fires.
* Only SL in range → SL fires.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode
from app.engine.strategy.base import Strategy


class _StaticBarReader:
    """Minimal data source that yields a fixed list of TradeBars."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        yield from self._bars


class _BracketEntryStrategy(Strategy):
    """Enters long on its first observed bar with TP/SL attached.

    The strategy records every ``OrderEvent`` it receives so the test
    can assert on the bracket exit's tag and fill price.
    """

    def __init__(
        self,
        take_profit: Decimal | None,
        stop_loss: Decimal | None,
    ) -> None:
        super().__init__()
        self._tp = take_profit
        self._sl = stop_loss
        self._entered = False
        self._symbol = "SPY"
        self.order_events: list = []

    def initialize(self) -> None:
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 1, 3)
        self.set_cash(100_000)
        assert self.ctx is not None
        symbol = self.ctx.add_equity(self._symbol)
        self._symbol = symbol
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        if self._entered:
            return
        self.ctx.portfolio.submit_market_order(
            self._symbol,
            quantity=100,
            time=bar.end_time,
            take_profit_price=self._tp,
            stop_loss_price=self._sl,
        )
        self._entered = True

    def on_order_event(self, event) -> None:
        self.order_events.append(event)


def _make_bar(minute: int, *, high: str, low: str, close: str = "500.00") -> TradeBar:
    start = datetime(2024, 1, 2, 14, 30, tzinfo=UTC) + timedelta(minutes=minute)
    end = start + timedelta(minutes=1)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=end,
        open=Decimal("500.00"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


def _run(bars: list[TradeBar], tp: Decimal | None, sl: Decimal | None) -> _BracketEntryStrategy:
    strategy = _BracketEntryStrategy(tp, sl)
    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        fill_model=FillModel(mode=FillMode.SIGNAL_BAR_CLOSE),
    )
    engine.run(strategy)
    return strategy


# ---------------------------------------------------------------------------
# Scenario bar layout (constant across tests — only bar-3's range varies):
#
#   bar 0: flat — consolidator starts building its first working bar
#   bar 1: flat — fires bar 0; strategy sees its first on_bar and enters
#                 long at bar-0.close=500 with TP/SL attached
#   bar 2: flat — fires bar 1; bracket evaluated against bar 1's (flat)
#                 range, no trigger
#   bar 3: the "wide" bar whose range determines the outcome
#   bar 4: flat — fires bar 3; bracket evaluated → exit fires here
# ---------------------------------------------------------------------------


def _scaffold(trigger_high: str, trigger_low: str) -> list[TradeBar]:
    return [
        _make_bar(0, high="500", low="500"),
        _make_bar(1, high="500", low="500"),
        _make_bar(2, high="500", low="500"),
        _make_bar(3, high=trigger_high, low=trigger_low),
        _make_bar(4, high="495", low="495", close="495"),
    ]


def test_bracket_pessimistic_sl_wins_when_both_in_range():
    """Bar 3 has high=512 and low=488 — both TP (510) and SL (490) in
    range. Pessimistic rule says SL fires; exit price = 490."""
    bars = _scaffold(trigger_high="512", trigger_low="488")

    strategy = _run(bars, tp=Decimal("510"), sl=Decimal("490"))

    # Entry event + one bracket exit event.
    assert len(strategy.order_events) == 2

    entry = strategy.order_events[0]
    assert entry.fill_price == Decimal("500.00")
    assert entry.direction is Direction.LONG
    assert entry.fill_quantity == 100

    exit_event = strategy.order_events[1]
    assert exit_event.tag == "SL"
    assert exit_event.fill_price == Decimal("490")
    assert exit_event.direction is Direction.SHORT
    assert exit_event.fill_quantity == -100


def test_bracket_only_take_profit_in_range_fires_tp():
    """Bar 3 has high=512 and low=495 — TP (510) in range, SL (490) not."""
    bars = _scaffold(trigger_high="512", trigger_low="495")

    strategy = _run(bars, tp=Decimal("510"), sl=Decimal("490"))

    assert len(strategy.order_events) == 2
    exit_event = strategy.order_events[1]
    assert exit_event.tag == "TP"
    assert exit_event.fill_price == Decimal("510")


def test_bracket_only_stop_loss_in_range_fires_sl():
    """Bar 3 has high=505 and low=488 — SL (490) in range, TP (510) not."""
    bars = _scaffold(trigger_high="505", trigger_low="488")

    strategy = _run(bars, tp=Decimal("510"), sl=Decimal("490"))

    assert len(strategy.order_events) == 2
    exit_event = strategy.order_events[1]
    assert exit_event.tag == "SL"
    assert exit_event.fill_price == Decimal("490")


def test_bracket_no_trigger_holds_position_through_run():
    """All bars flat — bracket never fires. Only the entry event is seen."""
    bars = [_make_bar(i, high="500", low="500") for i in range(6)]

    strategy = _run(bars, tp=Decimal("510"), sl=Decimal("490"))

    assert len(strategy.order_events) == 1
    assert strategy.order_events[0].direction is Direction.LONG


def test_orders_without_brackets_do_not_register_a_watcher():
    """Regression guard: a market order with no TP/SL must not
    accidentally activate the bracket mechanism. Proves the new code
    path is strictly opt-in — bit-exact LEAN parity for strategies that
    don't set bracket fields is preserved."""
    bars = _scaffold(trigger_high="512", trigger_low="488")

    strategy = _run(bars, tp=None, sl=None)

    # Only the entry fill — no exit, no bracket watcher.
    assert len(strategy.order_events) == 1
    assert strategy.order_events[0].direction is Direction.LONG
