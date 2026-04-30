"""
Tests for the dataset-CSV ``PC`` (previous close) column.

Contract — PC is the close of the most recently *completed* RTH session at
or before each bar's timestamp:

  * Bar wall-clock < 16:00 ET (during or before today's RTH)  → PC = prior
    trading day's RTH close.
  * Bar wall-clock ≥ 16:00 ET (today's RTH has just printed)  → PC = today's
    RTH close.

This is the time-aware reading: across the 16:00 boundary the reference
flips from "yesterday's close" to "today's close", so adjacent extended-
session bars on either side of the close subtract cleanly to give the
overnight gap.

Two units under test:
  * ``fetch_rth_closes`` — pulls daily bars with a 14-day buffer before
    from_date and returns ``{trading_date_iso → that_day_RTH_close}``.
  * ``add_previous_close_column`` — applies the rolling rule above. Bars
    whose lookup target isn't covered (e.g. the very first trading day with
    no prior session in the feed) get NaN, never a silent zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.dataset_service import (
    add_previous_close_column,
    fetch_rth_closes,
)

_ET = ZoneInfo("US/Eastern")


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """ET wall-clock → unix ms UTC."""
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def _daily_bar(ts_ms: int, close: float) -> dict[str, Any]:
    """Skeletal Polygon daily aggregate for the helpers under test."""
    return {"timestamp": ts_ms, "open": close, "high": close, "low": close, "close": close, "volume": 1_000_000}


# ── fetch_rth_closes ──────────────────────────────────────────────────


def test_fetch_rth_closes_maps_each_trading_day_to_its_own_close():
    """Returns today's close per trading day, not yesterday's. The rolling
    logic that picks prior-vs-current lives in add_previous_close_column."""
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [
        _daily_bar(_ms(2026, 1, 2), close=100.0),  # Friday
        _daily_bar(_ms(2026, 1, 5), close=101.0),  # Monday
        _daily_bar(_ms(2026, 1, 6), close=102.0),  # Tuesday
    ]

    closes = fetch_rth_closes(polygon, "SPY", "2026-01-05", "2026-01-06")

    assert closes == {"2026-01-02": 100.0, "2026-01-05": 101.0, "2026-01-06": 102.0}


def test_fetch_rth_closes_passes_buffer_and_adjusted_to_polygon():
    """Buffer-start must precede from_date by `buffer_days` and the adjusted
    flag must be propagated unchanged so PC matches the bar adjustment policy."""
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [_daily_bar(_ms(2026, 1, 2), 100.0)]

    fetch_rth_closes(polygon, "SPY", "2026-01-05", "2026-01-07", adjusted=False, buffer_days=14)

    kwargs = polygon.fetch_aggregates.call_args.kwargs
    assert kwargs["ticker"] == "SPY"
    assert kwargs["timespan"] == "day"
    assert kwargs["multiplier"] == 1
    assert kwargs["adjusted"] is False
    assert kwargs["from_date"] == "2025-12-22"  # 2026-01-05 minus 14 days
    assert kwargs["to_date"] == "2026-01-07"


def test_fetch_rth_closes_empty_response_returns_empty_map():
    """An empty Polygon response (e.g. invalid ticker) must not crash; the
    column-add helper will then attach NaN PC values rather than fabricated
    numbers."""
    polygon = Mock()
    polygon.fetch_aggregates.return_value = []

    assert fetch_rth_closes(polygon, "INVALID", "2026-01-05", "2026-01-07") == {}


# ── add_previous_close_column — rolling rule ─────────────────────────


def test_pc_before_1600_uses_prior_trading_day_close():
    """Morning RTH and pre-market bars on day D reference D−1's close so
    morning gap is open − PC."""
    rth_closes = {"2026-01-05": 99.5, "2026-01-06": 102.0}
    bars = pd.DataFrame(
        [
            {"timestamp": _ms(2026, 1, 6, 4, 30), "close": 101.5},   # pre-market
            {"timestamp": _ms(2026, 1, 6, 9, 30), "close": 101.7},   # RTH open
            {"timestamp": _ms(2026, 1, 6, 12, 0), "close": 101.9},   # RTH midday
            {"timestamp": _ms(2026, 1, 6, 15, 59), "close": 102.0},  # RTH last minute
        ]
    )

    out = add_previous_close_column(bars, rth_closes)

    assert out["PC"].tolist() == [99.5, 99.5, 99.5, 99.5]


def test_pc_at_or_after_1600_uses_same_day_close():
    """Bars at 16:00 or later on day D reference D's just-completed close
    so post-market drift is close − PC against today's close."""
    rth_closes = {"2026-01-05": 99.5, "2026-01-06": 102.0}
    bars = pd.DataFrame(
        [
            {"timestamp": _ms(2026, 1, 6, 16, 0), "close": 102.05},   # close boundary (after-hours start)
            {"timestamp": _ms(2026, 1, 6, 17, 30), "close": 102.10},  # post-market
            {"timestamp": _ms(2026, 1, 6, 19, 59), "close": 102.20},  # post-market last minute
        ]
    )

    out = add_previous_close_column(bars, rth_closes)

    assert out["PC"].tolist() == [102.0, 102.0, 102.0]


def test_pc_overnight_gap_is_clean_subtraction_across_1600():
    """The whole point of the rolling rule: post-market on D and pre-market
    on D+1 reference the same close (D's), so two adjacent extended-session
    bars subtract cleanly to give the overnight gap. Without the rolling
    rule, post-market on D would reference D−1's close and the gap would
    smuggle in two days of movement."""
    rth_closes = {
        "2026-01-05": 99.5,
        "2026-01-06": 102.0,
        "2026-01-07": 103.0,
    }
    bars = pd.DataFrame(
        [
            {"timestamp": _ms(2026, 1, 6, 17, 0), "close": 102.5},  # post-market on Tue (Jan 6)
            {"timestamp": _ms(2026, 1, 7, 6, 0), "close": 102.8},   # pre-market on Wed (Jan 7)
        ]
    )

    out = add_previous_close_column(bars, rth_closes)

    # Both reference Jan 6's close (102.0). Spread = 102.8 − 102.5 = 0.3
    # is the genuine overnight move between the two extended-session prints.
    assert out["PC"].tolist() == [102.0, 102.0]


def test_pc_unmapped_lookup_target_becomes_nan():
    """If the rolling rule's lookup target isn't in the closes map (e.g.
    morning bar on the very first trading day in a fresh feed with no prior
    session available), PC must be NaN — never silently zero or back-filled."""
    rth_closes = {"2026-01-06": 102.0}  # only one day, no prior

    morning_bar = pd.DataFrame([{"timestamp": _ms(2026, 1, 6, 9, 30), "close": 101.7}])
    out_morning = add_previous_close_column(morning_bar, rth_closes)
    assert pd.isna(out_morning.loc[0, "PC"])

    # Same date but after the close → today's close *is* in the map → not NaN.
    afternoon_bar = pd.DataFrame([{"timestamp": _ms(2026, 1, 6, 16, 30), "close": 102.05}])
    out_afternoon = add_previous_close_column(afternoon_bar, rth_closes)
    assert out_afternoon.loc[0, "PC"] == 102.0


def test_pc_groups_by_et_date_not_utc():
    """A bar at 22:00 ET on Jan 5 is 03:00 UTC on Jan 6 — its trading date
    is Jan 5 in ET. Because 22:00 ≥ 16:00 ET, PC = Jan 5's close, not
    Jan 6's. Without ET tz-conversion this would mis-key into Jan 6."""
    rth_closes = {"2026-01-05": 99.0, "2026-01-06": 100.0}
    bars = pd.DataFrame([{"timestamp": _ms(2026, 1, 5, 22, 0), "close": 99.5}])

    out = add_previous_close_column(bars, rth_closes)

    assert out["PC"].tolist() == [99.0]


def test_pc_empty_df_keeps_pc_column():
    """Empty input must still gain a PC column (column-order consumers rely
    on its presence regardless of row count)."""
    bars = pd.DataFrame({"timestamp": pd.Series([], dtype="int64"), "close": pd.Series([], dtype="float64")})

    out = add_previous_close_column(bars, {"2026-01-05": 99.0})

    assert "PC" in out.columns
    assert len(out) == 0


def test_pc_empty_closes_map_yields_all_nan():
    """An empty rth_closes map (Polygon returned no daily bars) must produce
    a PC column of NaN, not crash."""
    bars = pd.DataFrame(
        [
            {"timestamp": _ms(2026, 1, 6, 9, 30), "close": 101.7},
            {"timestamp": _ms(2026, 1, 6, 17, 0), "close": 102.05},
        ]
    )

    out = add_previous_close_column(bars, {})

    assert out["PC"].isna().all()
