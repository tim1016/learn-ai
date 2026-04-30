"""
Regression: forward_fill_gaps must not reshape higher-resolution bars onto a
1-minute grid.

Bug 2026-04-29: with the chart's timeframe selector set to 15m and the ZIP
download fired, the dataset.csv came back at 1-minute intervals even though
metadata.csv said timespan=minute, multiplier=15. Root cause was that
``forward_fill_gaps`` hardcoded ``freq="min"`` and merged the 15-minute bars
onto a 1-minute template, ffilling OHLC across every intermediate minute.

These tests pin the new contract:
  * For (timespan='minute', multiplier=N), the filled grid steps every N
    minutes — no fabricated intermediate bars.
  * For non-minute timespans (day/week/month), the function returns the
    frame unchanged (within-day fill is meaningless when there is at most
    one bar per day group).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.dataset_service import forward_fill_gaps

_ET = ZoneInfo("US/Eastern")


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """ET wall-clock → unix ms UTC."""
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def _bar(ts_ms: int, *, close: float = 100.0) -> dict[str, float | int]:
    return {
        "timestamp": ts_ms,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000,
        "transactions": 5,
    }


def test_15m_bars_stay_at_15m_resolution():
    """A handful of 15-min bars across an RTH session must produce 26
    rows (09:30 → 15:45 inclusive), not 390 rows. Before the fix this
    returned 390 minute-stamped rows with ffilled OHLC."""
    bars = [
        _bar(_ms(2026, 1, 5, 9, 30), close=100.0),
        _bar(_ms(2026, 1, 5, 9, 45), close=101.0),
        _bar(_ms(2026, 1, 5, 10, 0), close=102.0),
        _bar(_ms(2026, 1, 5, 15, 45), close=110.0),
    ]
    df = pd.DataFrame(bars)

    out = forward_fill_gaps(df, session="rth", timespan="minute", multiplier=15)

    assert len(out) == 26, f"expected 26 fifteen-minute slots, got {len(out)}"

    # Spacing between consecutive timestamps must be exactly 15 minutes.
    deltas_ms = out["timestamp"].diff().dropna().unique().tolist()
    assert deltas_ms == [15 * 60 * 1000], f"non-15m spacing: {deltas_ms}"

    # The known bars survive at their own timestamps with the correct close.
    by_ts = {int(row["timestamp"]): row for _, row in out.iterrows()}
    assert by_ts[_ms(2026, 1, 5, 9, 30)]["close"] == 100.0
    assert by_ts[_ms(2026, 1, 5, 9, 45)]["close"] == 101.0
    assert by_ts[_ms(2026, 1, 5, 10, 0)]["close"] == 102.0


def test_1m_bars_unchanged_behavior():
    """The pre-existing minute-resolution path must keep working — 390 RTH
    slots with ffilled gaps."""
    bars = [
        _bar(_ms(2026, 1, 5, 9, 30), close=100.0),
        _bar(_ms(2026, 1, 5, 9, 35), close=101.0),
        _bar(_ms(2026, 1, 5, 15, 59), close=110.0),
    ]
    df = pd.DataFrame(bars)

    out = forward_fill_gaps(df, session="rth", timespan="minute", multiplier=1)

    assert len(out) == 390  # 09:30 → 15:59 inclusive
    deltas_ms = out["timestamp"].diff().dropna().unique().tolist()
    assert deltas_ms == [60 * 1000]


@pytest.mark.parametrize("timespan", ["day", "week", "month"])
def test_day_and_above_returns_frame_unchanged(timespan: str):
    """Day/week/month resolutions: at most one bar per groupby-date group,
    so within-day forward-fill is a no-op. The function must short-circuit
    instead of building a grid that doesn't map cleanly to those resolutions."""
    bars = [
        _bar(_ms(2026, 1, 5, 9, 30), close=100.0),
        _bar(_ms(2026, 1, 6, 9, 30), close=101.0),
    ]
    df = pd.DataFrame(bars)

    out = forward_fill_gaps(df, session="rth", timespan=timespan, multiplier=1)

    assert len(out) == 2
    assert out["close"].tolist() == [100.0, 101.0]
