"""EMA persistence — round-trip + bit-identical outputs on next bars.

EMA carries an internal SMA used to seed the EMA during warmup. The
SMA's state must also persist for the round-trip to be exact through
the warmup-then-recursion transition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.indicators.ema import ExponentialMovingAverage


def _feed(ind: ExponentialMovingAverage, values: list[Decimal], t0: datetime) -> None:
    for i, v in enumerate(values):
        ind.update(t0 + timedelta(minutes=15 * i), v)


def test_round_trip_post_warmup() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    # Period 5; feed 8 bars so EMA is in the recursive phase.
    warmup = [Decimal(x) for x in ("100", "101", "102", "103", "104", "105", "106", "107")]
    src = ExponentialMovingAverage("EMA5", 5)
    _feed(src, warmup, t0)
    assert src.is_ready

    state = src.to_state_dict()
    dst = ExponentialMovingAverage("EMA5", 5)
    dst.restore_state(state)

    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    # SMA internals also restored.
    assert dst._sma.current_value == src._sma.current_value
    assert list(dst._sma._window) == list(src._sma._window)


def test_bit_identical_outputs_after_restore_post_warmup() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    warmup = [Decimal(x) for x in ("100", "101", "102", "103", "104", "105", "106", "107")]
    src = ExponentialMovingAverage("EMA5", 5)
    _feed(src, warmup, t0)
    state = src.to_state_dict()

    next_t = t0 + timedelta(minutes=15 * 8)
    src.update(next_t, Decimal("108"))
    expected = src.current_value

    dst = ExponentialMovingAverage("EMA5", 5)
    dst.restore_state(state)
    dst.update(next_t, Decimal("108"))

    assert dst.current_value == expected


def test_bit_identical_for_five_more_bars_after_restore() -> None:
    """Stronger property: equivalence persists through several iterations,
    not just the next bar. This is the load-bearing claim of warm-start
    equivalence."""
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    warmup = [Decimal(x) for x in ("100", "101", "102", "103", "104", "105", "106", "107")]
    src = ExponentialMovingAverage("EMA5", 5)
    _feed(src, warmup, t0)
    state = src.to_state_dict()

    dst = ExponentialMovingAverage("EMA5", 5)
    dst.restore_state(state)

    for i in range(5):
        t = t0 + timedelta(minutes=15 * (8 + i))
        v = Decimal(108 + i)
        src.update(t, v)
        dst.update(t, v)
        assert dst.current_value == src.current_value, f"bar {i}: {dst.current_value} != {src.current_value}"
