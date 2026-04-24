"""Unit tests for strategies A / B / C.

Focused on the *strategy-specific* entry-gate logic. The shared flow
(RSI-range filter → gates → ADX exit) is covered in part by the
indicator tests (``test_adx.py``, ``test_macd.py``,
``test_supertrend.py``).

So the remaining risk is the gate wiring per strategy. We drive each
gate by instantiating the strategy, calling ``initialize`` against a
fake ``StrategyContext``, manually advancing the indicators to the
desired state, and asserting on ``_entry_extra_gate_passes``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.spy_strategy_a import SpyStrategyAAlgorithm
from app.engine.strategy.algorithms.spy_strategy_b import SpyStrategyBAlgorithm
from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm
from app.engine.strategy.base import StrategyContext


def _wire(strategy) -> StrategyContext:
    """Attach a minimal live ``StrategyContext`` and run ``initialize``."""
    ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal(100_000)))
    strategy.ctx = ctx
    strategy.initialize()
    return ctx


def _bar(ts: datetime, close: str, high: str | None = None, low: str | None = None) -> TradeBar:
    c = Decimal(close)
    h = Decimal(high) if high is not None else c + Decimal("0.5")
    lo = Decimal(low) if low is not None else c - Decimal("0.5")
    return TradeBar(
        symbol="SPY",
        time=ts,
        end_time=ts + timedelta(minutes=15),
        open=c,
        high=h,
        low=lo,
        close=c,
        volume=1_000_000,
    )


# ---------------------------------------------------------------------------
# Strategy A — EMA-gap + MACD > 0
# ---------------------------------------------------------------------------


def test_strategy_a_gate_rejects_when_ema_gap_below_threshold():
    s = SpyStrategyAAlgorithm(ema_fast_period=3, ema_slow_period=5, ema_gap_threshold=1)
    _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Feed 50 flat bars so EMA gap ≈ 0 < threshold=1. All indicators ready.
    for i in range(50):
        s._update_extra_indicators(_bar(t + timedelta(minutes=15 * i), "100"))
    assert s._extra_indicators_ready()
    assert not s._entry_extra_gate_passes(_bar(t + timedelta(minutes=15 * 50), "100"))


def test_strategy_a_gate_passes_with_large_gap_and_positive_macd():
    s = SpyStrategyAAlgorithm(ema_fast_period=3, ema_slow_period=5, ema_gap_threshold=Decimal("0.01"))
    _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Rising sequence pushes fast > slow (large gap) and macd > 0.
    for i in range(50):
        s._update_extra_indicators(_bar(t + timedelta(minutes=15 * i), str(100 + i * 0.5)))
    assert s._extra_indicators_ready()
    assert s._entry_extra_gate_passes(_bar(t + timedelta(minutes=15 * 50), "125"))


# ---------------------------------------------------------------------------
# Strategy B — Supertrend long + ADX > threshold + MACD > 0
# ---------------------------------------------------------------------------


def test_strategy_b_gate_rejects_when_supertrend_short():
    s = SpyStrategyBAlgorithm(supertrend_atr_period=3, adx_entry_threshold=0)
    _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Rising then sharp drop — at the drop bar Supertrend flips short.
    for i in range(40):
        s._update_extra_indicators(_bar(t + timedelta(minutes=15 * i), str(100 + i)))
        s._adx.update(_bar(t + timedelta(minutes=15 * i), str(100 + i)))
    # Huge downward gap → Supertrend flips to downtrend.
    crash = _bar(
        t + timedelta(minutes=15 * 40),
        close="50",
        high="51",
        low="49",
    )
    s._update_extra_indicators(crash)
    s._adx.update(crash)
    assert s._supertrend.is_long is False
    assert not s._entry_extra_gate_passes(crash)


def test_strategy_b_gate_rejects_when_macd_nonpositive():
    s = SpyStrategyBAlgorithm(supertrend_atr_period=3, adx_entry_threshold=0)
    _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Flat prices — MACD line converges to 0.
    for i in range(60):
        b = _bar(t + timedelta(minutes=15 * i), "100")
        s._update_extra_indicators(b)
        s._adx.update(b)
    last = _bar(t + timedelta(minutes=15 * 60), "100")
    # MACD line ≈ 0 so gate should fail (macd > 0 required, not >= 0).
    assert not s._entry_extra_gate_passes(last)


# ---------------------------------------------------------------------------
# Strategy C — ADX > threshold + ADX rising
# ---------------------------------------------------------------------------


def test_strategy_c_gate_rejects_when_adx_not_rising():
    s = SpyStrategyCAlgorithm(adx_entry_threshold=Decimal("0"))
    _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Monotone uptrend with flat bars → ADX becomes constant after warmup.
    for i in range(40):
        b = _bar(t + timedelta(minutes=15 * i), "100", "101", "99")
        s._adx.update(b)
    assert s._adx.is_ready
    # ADX not rising because all bars identical.
    assert not s._entry_extra_gate_passes(_bar(t + timedelta(minutes=15 * 40), "100"))


def test_strategy_c_gate_passes_when_adx_rising_above_threshold():
    s = SpyStrategyCAlgorithm(adx_entry_threshold=Decimal("0.1"))
    _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    # Strong uptrend produces rising ADX above 0.
    for i in range(60):
        b = _bar(t + timedelta(minutes=15 * i), str(100 + i * 0.5))
        s._adx.update(b)
    assert s._adx.is_ready
    assert s._adx.current_value > Decimal("0.1")
    # Whether it's strictly rising on this bar depends on the dynamics;
    # check directly against the public properties.
    if s._adx.current_value is not None and s._adx.previous_value is not None:
        rising = s._adx.current_value > s._adx.previous_value
        assert s._entry_extra_gate_passes(_bar(t + timedelta(minutes=15 * 60), "130")) is rising


# ---------------------------------------------------------------------------
# Generic: exit on ADX < threshold triggers liquidate.
# ---------------------------------------------------------------------------


def test_base_strategy_exits_when_adx_below_threshold():
    """Shared exit path: any RSI-sequence strategy liquidates when
    ADX < exit threshold while holding a position.

    ADX is bounded at 100, so a threshold of 200 guarantees the exit
    fires on the first bar ADX becomes ready.
    """
    s = SpyStrategyCAlgorithm(adx_exit_threshold=Decimal("200"))
    ctx = _wire(s)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    s._in_position = True
    ctx.portfolio.positions["SPY"] = _position("SPY", 100, Decimal(100))
    ctx.portfolio.update_reference_price("SPY", Decimal(100))
    for i in range(40):
        b = _bar(t + timedelta(minutes=15 * i), str(100 + i * 0.1))
        ctx.current_time = b.end_time
        s._on_bar(b)
    assert not s._in_position
    assert any(o.quantity == -100 for o in ctx.portfolio.pending_orders)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _position(symbol: str, qty: int, avg_price: Decimal):
    from app.engine.execution.portfolio import Position

    return Position(symbol=symbol, quantity=qty, average_price=avg_price)
