"""SMA persistence — round-trip + bit-identical outputs on next bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.indicators.sma import SimpleMovingAverage


def _feed(ind: SimpleMovingAverage, values: list[Decimal], t0: datetime) -> None:
    for i, v in enumerate(values):
        ind.update(t0 + timedelta(minutes=15 * i), v)


def test_round_trip_through_state_dict() -> None:
    src = SimpleMovingAverage("S", 3)
    _feed(src, [Decimal(x) for x in ("100", "101", "102", "103")], datetime(2026, 5, 18, 14, 0, tzinfo=UTC))
    state = src.to_state_dict()

    dst = SimpleMovingAverage("S", 3)
    dst.restore_state(state)
    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    # Internals match: deque contents and sum.
    assert list(dst._window) == list(src._window)
    assert dst._sum == src._sum


def test_bit_identical_outputs_after_restore() -> None:
    """The load-bearing property: a restored SMA + the next bar produces
    the exact same value as a freshly-warmed SMA + the same bar."""
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    warmup = [Decimal(x) for x in ("100", "101", "102", "103")]

    src = SimpleMovingAverage("S", 3)
    _feed(src, warmup, t0)
    state = src.to_state_dict()

    # Path A: continue the original.
    extra_bar_time = t0 + timedelta(minutes=15 * 4)
    src.update(extra_bar_time, Decimal("104"))
    expected = src.current_value

    # Path B: restore a fresh instance and feed the same extra bar.
    dst = SimpleMovingAverage("S", 3)
    dst.restore_state(state)
    dst.update(extra_bar_time, Decimal("104"))
    actual = dst.current_value

    assert actual == expected  # Decimal equality — atol=0
