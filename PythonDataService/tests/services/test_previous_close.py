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
import pytest

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


@pytest.mark.parametrize(
    ("symbol", "from_date", "to_date", "daily_closes", "expected_closes"),
    [
        # Returns today's close per trading day, not yesterday's. The rolling
        # logic that picks prior-vs-current lives in add_previous_close_column.
        pytest.param(
            "SPY",
            "2026-01-05",
            "2026-01-06",
            ((2, 100.0), (5, 101.0), (6, 102.0)),  # Friday, Monday, Tuesday
            (("2026-01-02", 100.0), ("2026-01-05", 101.0), ("2026-01-06", 102.0)),
            id="maps_each_trading_day_to_its_own_close",
        ),
        # An empty Polygon response (e.g. invalid ticker) must not crash; the
        # column-add helper will then attach NaN PC values rather than
        # fabricated numbers.
        pytest.param("INVALID", "2026-01-05", "2026-01-07", (), (), id="empty_response_returns_empty_map"),
    ],
)
def test_fetch_rth_closes_maps_daily_bars_to_closes(symbol, from_date, to_date, daily_closes, expected_closes):
    polygon = Mock()
    polygon.fetch_aggregates.return_value = [_daily_bar(_ms(2026, 1, day), close=close) for day, close in daily_closes]

    closes = fetch_rth_closes(polygon, symbol, from_date, to_date)

    assert closes == dict(expected_closes), "each trading day maps to its own close; no bars map to nothing"


# ── add_previous_close_column — rolling rule ─────────────────────────


@pytest.mark.parametrize(
    ("rth_closes", "bars", "expected_pc"),
    [
        # Morning RTH and pre-market bars on day D reference D−1's close so
        # morning gap is open − PC.
        pytest.param(
            (("2026-01-05", 99.5), ("2026-01-06", 102.0)),
            (
                ((2026, 1, 6, 4, 30), 101.5),  # pre-market
                ((2026, 1, 6, 9, 30), 101.7),  # RTH open
                ((2026, 1, 6, 12, 0), 101.9),  # RTH midday
                ((2026, 1, 6, 15, 59), 102.0),  # RTH last minute
            ),
            (99.5, 99.5, 99.5, 99.5),
            id="before_1600_uses_prior_trading_day_close",
        ),
        # Bars at 16:00 or later on day D reference D's just-completed close
        # so post-market drift is close − PC against today's close.
        pytest.param(
            (("2026-01-05", 99.5), ("2026-01-06", 102.0)),
            (
                ((2026, 1, 6, 16, 0), 102.05),  # close boundary (after-hours start)
                ((2026, 1, 6, 17, 30), 102.10),  # post-market
                ((2026, 1, 6, 19, 59), 102.20),  # post-market last minute
            ),
            (102.0, 102.0, 102.0),
            id="at_or_after_1600_uses_same_day_close",
        ),
        # The whole point of the rolling rule: post-market on D and pre-market
        # on D+1 reference the same close (D's), so two adjacent extended-session
        # bars subtract cleanly to give the overnight gap (here 102.8 − 102.5 =
        # 0.3). Without the rolling rule, post-market on D would reference D−1's
        # close and the gap would smuggle in two days of movement.
        pytest.param(
            (("2026-01-05", 99.5), ("2026-01-06", 102.0), ("2026-01-07", 103.0)),
            (
                ((2026, 1, 6, 17, 0), 102.5),  # post-market on Tue (Jan 6)
                ((2026, 1, 7, 6, 0), 102.8),  # pre-market on Wed (Jan 7)
            ),
            (102.0, 102.0),
            id="overnight_gap_is_clean_subtraction_across_1600",
        ),
        # A bar at 22:00 ET on Jan 5 is 03:00 UTC on Jan 6 — its trading date
        # is Jan 5 in ET. Because 22:00 ≥ 16:00 ET, PC = Jan 5's close, not
        # Jan 6's. Without ET tz-conversion this would mis-key into Jan 6.
        pytest.param(
            (("2026-01-05", 99.0), ("2026-01-06", 100.0)),
            (((2026, 1, 5, 22, 0), 99.5),),
            (99.0,),
            id="groups_by_et_date_not_utc",
        ),
    ],
)
def test_pc_follows_the_rolling_previous_close_rule(rth_closes, bars, expected_pc):
    frame = pd.DataFrame([{"timestamp": _ms(*when), "close": close} for when, close in bars])

    out = add_previous_close_column(frame, dict(rth_closes))

    assert out["PC"].tolist() == list(expected_pc), "each bar references the most recently completed RTH close"


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
