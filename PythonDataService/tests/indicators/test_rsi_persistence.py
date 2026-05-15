"""RSI persistence — round-trip + bit-identical outputs on next bars.

RSI carries six extra fields beyond the base: _prev_input, _avg_gain,
_avg_loss, _gain_sum, _loss_sum, _delta_samples.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.engine.indicators.rsi import RelativeStrengthIndex


def _feed(ind: RelativeStrengthIndex, values: list[Decimal], t0: datetime) -> None:
    for i, v in enumerate(values):
        ind.update(t0 + timedelta(minutes=15 * i), v)


def test_round_trip_post_warmup() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    # Period 14 needs 15 samples to be ready; feed 20.
    closes = [Decimal(100 + i) for i in range(20)]
    src = RelativeStrengthIndex("RSI14", 14)
    _feed(src, closes, t0)
    assert src.is_ready

    state = src.to_state_dict()
    dst = RelativeStrengthIndex("RSI14", 14)
    dst.restore_state(state)

    assert dst.samples == src.samples
    assert dst.current_value == src.current_value
    assert dst._prev_input == src._prev_input
    assert dst._avg_gain == src._avg_gain
    assert dst._avg_loss == src._avg_loss
    assert dst._delta_samples == src._delta_samples


def test_bit_identical_outputs_for_five_more_bars_after_restore() -> None:
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    closes = [Decimal(100 + i) for i in range(20)]
    src = RelativeStrengthIndex("RSI14", 14)
    _feed(src, closes, t0)
    state = src.to_state_dict()

    dst = RelativeStrengthIndex("RSI14", 14)
    dst.restore_state(state)

    # Mix of up and down moves to exercise both avg_gain and avg_loss branches.
    next_values = [Decimal("125"), Decimal("123"), Decimal("128"), Decimal("130"), Decimal("129")]
    for i, v in enumerate(next_values):
        t = t0 + timedelta(minutes=15 * (20 + i))
        src.update(t, v)
        dst.update(t, v)
        assert dst.current_value == src.current_value, f"bar {i}: {dst.current_value} != {src.current_value}"
