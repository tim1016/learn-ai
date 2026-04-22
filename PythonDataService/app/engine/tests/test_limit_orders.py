"""Tests for resting limit orders with the penetration-based fill rule.

The user-specified math (from the PR 4 plan):

* BUY limit at ``L`` fills when ``L - bar.low >= penetration``.
* SELL limit at ``L`` fills when ``bar.high - L >= penetration``.
* Penetration is measured against the bar's ADVERSE extreme (low for
  buy, high for sell) — not the close.
* Default ``penetration = 0`` reproduces TradingView's permissive
  touch-fill. Non-zero values (e.g. 0.02 = 2 cents for US equities)
  are the realistic queue-position model.

Fills land AT the limit price with no slippage, plus commission.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.order import Direction
from app.engine.strategy.base import Strategy


class _StaticBarReader:
    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:  # noqa: ARG002
        yield from self._bars


class _LimitStrategy(Strategy):
    """Submits a single limit order on its first ``on_bar`` callback.

    ``direction=+1`` → buy limit (quantity 100). ``-1`` → sell limit.
    After submission the strategy sits idle so the resting order can
    interact with subsequent bars.
    """

    def __init__(
        self,
        *,
        limit_price: Decimal,
        direction: int = 1,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
    ) -> None:
        super().__init__()
        self._limit_price = limit_price
        self._sign = direction
        self._tp = take_profit
        self._sl = stop_loss
        self._submitted = False
        self._symbol = "SPY"
        self.order_events: list = []

    def initialize(self) -> None:
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 1, 3)
        self.set_cash(100_000)
        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol)
        self.ctx.register_consolidator(self._symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        if self._submitted:
            return
        self.ctx.portfolio.submit_limit_order(
            self._symbol,
            quantity=100 * self._sign,
            time=bar.end_time,
            limit_price=self._limit_price,
            tag="entry-limit",
            take_profit_price=self._tp,
            stop_loss_price=self._sl,
        )
        self._submitted = True

    def on_order_event(self, event) -> None:
        self.order_events.append(event)


class _EntryThenExitLimitStrategy(Strategy):
    """Market-enters on first bar, then submits a sell LIMIT (exit) on
    the ``exit_on_bar_index``-th callback. Used to exercise the
    session-cutoff-allows-exits path for limit orders."""

    def __init__(self, *, exit_on_bar_index: int, exit_limit: Decimal) -> None:
        super().__init__()
        self._exit_on = exit_on_bar_index
        self._exit_limit = exit_limit
        self._count = 0
        self._symbol = "SPY"
        self.order_events: list = []

    def initialize(self) -> None:
        self.set_start_date(2024, 1, 2)
        self.set_end_date(2024, 1, 3)
        self.set_cash(100_000)
        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol)
        self.ctx.register_consolidator(self._symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        idx = self._count
        self._count += 1
        if idx == 0:
            self.ctx.portfolio.submit_market_order(
                self._symbol, 100, bar.end_time, tag="entry"
            )
        elif idx == self._exit_on:
            self.ctx.portfolio.submit_limit_order(
                self._symbol,
                quantity=-100,
                time=bar.end_time,
                limit_price=self._exit_limit,
                tag="exit-limit",
            )

    def on_order_event(self, event) -> None:
        self.order_events.append(event)


def _bar(
    hour: int,
    minute: int,
    *,
    high: str = "500",
    low: str = "500",
    close: str = "500",
) -> TradeBar:
    start = datetime(2024, 1, 2, hour, minute, tzinfo=timezone.utc)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


def _run(
    bars: list[TradeBar],
    strategy: Strategy,
    *,
    execution_config: ExecutionConfig | None = None,
) -> Strategy:
    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        execution_config=execution_config or ExecutionConfig(),
    )
    engine.run(strategy)
    return strategy


# ===========================================================================
# Basic touch-fill (penetration = 0)
# ===========================================================================


def test_buy_limit_touch_fill_when_bar_low_equals_limit():
    """Zero penetration + ``bar.low == limit_price`` → fills exactly at
    the limit. This is TV's permissive default."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),  # submits buy limit @ 499 on this fire
        _bar(15, 32, high="501", low="499", close="500"),  # low touches 499 exactly
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(bars, strategy, execution_config=ExecutionConfig())

    assert len(strategy.order_events) == 1
    fill = strategy.order_events[0]
    assert fill.fill_price == Decimal("499")
    assert fill.direction is Direction.LONG
    assert fill.fill_quantity == 100


def test_sell_limit_touch_fill_when_bar_high_equals_limit():
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="501", low="499", close="500"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("501"), direction=-1)

    _run(bars, strategy, execution_config=ExecutionConfig())

    assert len(strategy.order_events) == 1
    fill = strategy.order_events[0]
    assert fill.fill_price == Decimal("501")
    assert fill.direction is Direction.SHORT
    assert fill.fill_quantity == -100


def test_buy_limit_does_not_fill_when_price_stays_above():
    """Price never drops to the limit → no fill; order stays resting
    through end of run."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="502", low="501", close="501"),
        _bar(15, 33, high="503", low="502", close="502"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(bars, strategy, execution_config=ExecutionConfig())

    assert strategy.order_events == []


# ===========================================================================
# Penetration rule
# ===========================================================================


def test_buy_limit_with_penetration_rejects_pure_touch():
    """With ``penetration=0.02`` a bar that kisses the limit exactly
    (``bar.low == limit_price``) does NOT fill — the user's core anti-
    touch-bias rule. A live order would likely sit at the back of the
    queue and miss this print."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="502", low="499", close="500"),  # touches 499 only
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(limit_penetration=Decimal("0.02")),
    )

    assert strategy.order_events == []


def test_buy_limit_fills_when_penetration_requirement_met_exactly():
    """At ``bar.low == limit_price - penetration`` the inequality
    ``limit - low >= penetration`` is exactly satisfied → fill."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="502", low="498.98", close="499.50"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(limit_penetration=Decimal("0.02")),
    )

    assert len(strategy.order_events) == 1
    assert strategy.order_events[0].fill_price == Decimal("499")


def test_sell_limit_penetration_measured_against_bar_high():
    """Mirror rule for sells: penetration comes off ``bar.high``, not
    close. A bar whose high sits exactly at the limit does not fill
    when penetration is required."""
    # First bar: high only touches 501 — no fill under 0.02 penetration.
    bars_no_fill = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="501", low="499", close="500"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("501"), direction=-1)
    _run(
        bars_no_fill,
        strategy,
        execution_config=ExecutionConfig(limit_penetration=Decimal("0.02")),
    )
    assert strategy.order_events == []

    # Second run: high reaches 501.02 — penetrates exactly → fill.
    bars_fill = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="501.02", low="499", close="500"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("501"), direction=-1)
    _run(
        bars_fill,
        strategy,
        execution_config=ExecutionConfig(limit_penetration=Decimal("0.02")),
    )
    assert len(strategy.order_events) == 1
    assert strategy.order_events[0].fill_price == Decimal("501")


def test_limit_fill_price_is_exactly_limit_not_bar_low():
    """Even if the bar penetrates far past the limit, the fill is at
    the limit price — not at the more-favorable bar.low. This is
    standard limit-order semantics; slippage is orthogonal and does
    NOT apply to limits (``slippage_per_share`` is set here to prove
    it doesn't leak)."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="500", low="497", close="498"),  # penetrates to 497
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(
            slippage_per_share=Decimal("0.05"),
            limit_penetration=Decimal("0.02"),
        ),
    )

    fill = strategy.order_events[0]
    assert fill.fill_price == Decimal("499")  # limit, not 497, not 499.05


def test_limit_charges_commission():
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="501", low="499", close="500"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(commission_per_order=Decimal("1.50")),
    )

    assert strategy.order_events[0].fee == Decimal("1.50")


# ===========================================================================
# Resting across bars
# ===========================================================================


def test_limit_rests_across_bars_until_condition_met():
    """Order is placed on bar 1; bars 2-4 miss the limit; bar 5
    touches. Fill must happen on bar 5, not before."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),  # submits limit
        _bar(15, 32, high="502", low="501", close="501"),
        _bar(15, 33, high="501", low="500", close="500"),
        _bar(15, 34, high="500", low="499.5", close="499.6"),
        _bar(15, 35, high="500", low="499", close="499"),  # touches 499 here
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(bars, strategy, execution_config=ExecutionConfig())

    assert len(strategy.order_events) == 1
    fill = strategy.order_events[0]
    assert fill.time == datetime(2024, 1, 2, 15, 36, tzinfo=timezone.utc)


# ===========================================================================
# Integration with PR 2 (brackets) and PR 3 (session wrapper)
# ===========================================================================


def test_limit_entry_with_bracket_activates_on_fill():
    """A limit entry with TP/SL attached: once the limit fills, the
    bracket becomes active and can fire on subsequent bars. The
    trailing flat bar is needed because the consolidator only fires a
    completed bar when the NEXT minute bar arrives."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),
        _bar(15, 32, high="500", low="499", close="499.50"),  # limit fills @ 499
        _bar(15, 33, high="509", low="498", close="499"),  # no bracket trigger
        _bar(15, 34, high="512", low="488", close="495"),  # both TP=510 and SL=490 → SL wins
        _bar(15, 35, high="495", low="495", close="495"),  # trailing bar forces fire of 15:34
    ]
    strategy = _LimitStrategy(
        limit_price=Decimal("499"),
        direction=1,
        take_profit=Decimal("510"),
        stop_loss=Decimal("490"),
    )

    _run(bars, strategy, execution_config=ExecutionConfig())

    # Entry at 499, SL exit at 490.
    assert len(strategy.order_events) == 2
    assert strategy.order_events[0].fill_price == Decimal("499")
    assert strategy.order_events[1].tag == "SL"
    assert strategy.order_events[1].fill_price == Decimal("490")


def test_force_flat_cancels_resting_limit_order():
    """User-flagged orphan gotcha for limits: a buy limit placed at
    14:00 that has not filled by force-flat must be cancelled.
    Otherwise it could fill tomorrow morning."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),  # submits buy limit @ 499
        _bar(15, 32, high="502", low="501", close="501"),  # no fill
        _bar(15, 45, high="502", low="501", close="501"),  # FORCE-FLAT — cancel limit
        # If the limit survives, this bar's low=499 would trigger the fill.
        _bar(15, 46, high="501", low="499", close="499.50"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(force_flat_at=time(15, 45)),
    )

    # No fills at all — limit was cancelled before it could fill, and
    # no position existed at force-flat to close.
    assert strategy.order_events == []


def test_session_cutoff_drops_newly_submitted_entry_limit():
    """Entry-direction limit orders submitted past the cutoff are
    dropped by the existing session-cutoff filter (which uses
    position math, so it handles MARKET and LIMIT identically)."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),  # strategy submits entry limit here; cutoff fires
        _bar(15, 32, high="500", low="499", close="499.50"),
    ]
    strategy = _LimitStrategy(limit_price=Decimal("499"), direction=1)

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(session_entry_cutoff=time(15, 31)),
    )

    # Entry limit was dropped at submission — no fills ever.
    assert strategy.order_events == []


def test_session_cutoff_allows_exit_limit_after_cutoff():
    """An EXIT limit (reduces the position) must pass through the
    cutoff unchanged — it's only entries that get blocked."""
    bars = [
        _bar(15, 30),
        _bar(15, 31),  # market entry
        _bar(15, 32),  # hold
        _bar(15, 33),
        _bar(15, 34),  # exit-limit submitted here (past cutoff)
        _bar(15, 35, high="502", low="499", close="500"),  # exit-limit is SELL @ 501
    ]
    strategy = _EntryThenExitLimitStrategy(
        exit_on_bar_index=3,
        exit_limit=Decimal("501"),
    )

    _run(
        bars,
        strategy,
        execution_config=ExecutionConfig(session_entry_cutoff=time(15, 33)),
    )

    # Entry + exit both fill.
    assert len(strategy.order_events) == 2
    assert strategy.order_events[0].tag == "entry"
    assert strategy.order_events[1].tag == "exit-limit"
    assert strategy.order_events[1].fill_price == Decimal("501")
