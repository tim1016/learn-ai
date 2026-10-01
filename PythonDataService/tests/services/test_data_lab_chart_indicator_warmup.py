"""Data Lab chart indicators warm up across the lead-in before the trim (#2458).

The dataset export computes indicators over the picked window plus its
warm-up lead-in and only then trims to the window. The chart trimmed first —
at the server's local midnight — and built its regular-hours mask from the
visible dates alone, so the lead-in never reached an indicator: every EMA
restarted cold at the chart's first visible bar and disagreed with the
export's for the same window.

Both surfaces now size the lead-in through one resolver,
``dataset_service.resolve_indicator_window``, keyed on the length of the bars
the indicators run on and counted in scheduled NYSE sessions (owner decision
2026-09-29: "warm up per timeframe").

Which timeframes the chart and the export can be compared on, bar for bar:

* **1m, 5m, 15m, 30m — compared here, at atol=1e-9 on the full-precision
  frames.** The chart resamples 1-minute bars into bins anchored at each
  session's open (09:30 ET regular hours, 04:00 ET extended); Polygon aligns
  its multi-minute aggregates to the epoch. Both anchors — and a 13:00 ET
  early close — sit on 30-minute boundaries of the epoch grid, so the two
  surfaces build the same bars from the same minutes.
* **1h, 4h — not comparable.** The chart's hourly bins start at the 09:30
  open (09:30–10:30, …); Polygon's hour bars start on the hour (09:00–10:00,
  …). They are different bars, so their indicators are different series.
* **1D, 1W, 1M — not comparable.** The chart aggregates regular-hours
  minutes into ET calendar bins it labels itself (a week at the Sunday that
  ends it, a month at its last day); the export ships Polygon's own
  day/week/month aggregates, built from the provider's full-day
  consolidated prints. Neither the bars nor their labels coincide.

Session boundaries come from the canonical NYSE calendar; the only fixed
offsets are the synthetic extended-hours spans the fake provider serves
around each scheduled session.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from functools import cache
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.data_lake.polygon_fetcher import polygon_history_floor
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc
from app.models.requests import DatasetGenerationRequest
from app.routers import dataset as dataset_router
from app.schemas.chart import ChartDataResponse
from app.services import chart_service, dataset_service
from app.services.dataset_plan_service import prepare_generation_request
from app.services.dataset_service import (
    INDICATOR_CONFIGS,
    bar_minutes_for,
    calculate_indicators_then_trim,
    resolve_indicator_window,
)
from app.services.indicator_warmup_policy import IndicatorEntry, requested_indicator_warmup_lookback
from app.utils.session_anchors import et_midnight_ms

#: Wed 2026-01-07 → Thu 2026-01-08, the window the original regression used.
#: It is EST, so Tuesday's 19:00–20:00 ET post-market bars carry Wednesday's
#: UTC-morning timestamps.
_FROM, _TO = "2026-01-07", "2026-01-08"
#: The windows the chart and the export are compared on. A Monday's lead-in
#: must reach back past its weekend; the Monday after Thanksgiving's crosses
#: the Friday 13:00 ET early close, the Thursday holiday and a weekend.
_WINDOWS = {
    "wednesday": (_FROM, _TO),
    "monday": ("2026-01-12", "2026-01-13"),
    "after_thanksgiving": ("2025-12-01", "2025-12-02"),
}
#: Chart timeframe → the export's minute multiplier for the same bars.
_COMPARABLE_TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
_EMAS: list[dict[str, Any]] = [
    {"name": "ema", "params": {"length": 20}},
    {"name": "ema", "params": {"length": 200}},
]
#: chart indicator id → export dataset column for the same indicator.
_EMA_COLUMNS = {"ema_20": "ema_length20", "ema_200": "ema_length200"}

# The extended hours the fake provider serves around each scheduled session:
# 04:00 ET up to the open, and the close up to 20:00 ET on a regular day.
_PRE_MARKET_MINUTES = 330
_POST_MARKET_MINUTES = 240
_MINUTE_MS = 60_000
#: The universe spans every lead-in the compared windows need — the 30m
#: lead-in of the post-Thanksgiving window reaches back to mid-August 2025.
_UNIVERSE_FIRST, _UNIVERSE_LAST = date(2025, 8, 1), date(2026, 1, 16)
#: The instant the provider's history floor is resolved against.
_NOW_MS = int(datetime(2026, 9, 29, 16, 0, tzinfo=UTC).timestamp() * 1000)


@cache
def _universe() -> pd.DataFrame:
    """Contiguous extended-hours minute bars (bar-start stamps), every session.

    Prices are one seeded random walk across every session, so an EMA carries
    its whole history into its value — a lead-in that never reached it shows
    up as a different number, never as a coincidence of flat prices.
    """
    windows = session_windows_ms_utc(_UNIVERSE_FIRST, _UNIVERSE_LAST)
    stamps = [
        np.arange(
            window.open_ms_utc - _PRE_MARKET_MINUTES * _MINUTE_MS,
            window.close_ms_utc + _POST_MARKET_MINUTES * _MINUTE_MS,
            _MINUTE_MS,
            dtype="int64",
        )
        for window in windows
    ]
    session_dates = np.concatenate(
        [np.full(len(day), window.session_date, dtype=object) for day, window in zip(stamps, windows, strict=True)]
    )
    timestamps = np.concatenate(stamps)
    rng = np.random.default_rng(seed=2458)
    closes = np.round(100.0 + np.cumsum(rng.normal(0.0, 0.05, len(timestamps))), 4)
    opens = np.concatenate([[100.0], closes[:-1]])
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": np.maximum(opens, closes) + 0.02,
            "low": np.minimum(opens, closes) - 0.02,
            "close": closes,
            "volume": rng.integers(100, 10_000, len(timestamps)),
            "vwap": closes,
            "transactions": np.full(len(timestamps), 10, dtype="int64"),
            "session_date": session_dates,
        }
    )


def _provider(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """What the provider serves for an inclusive day range: every hour it has."""
    universe = _universe()
    served = universe["session_date"].between(date.fromisoformat(from_date), date.fromisoformat(to_date))
    return universe[served].drop(columns="session_date").to_dict("records")


def _session_bars(day: date) -> list[dict[str, Any]]:
    return _provider(day.isoformat(), day.isoformat())


@cache
def _session_open_history() -> tuple[tuple[date, dict[str, Any]], ...]:
    """One seeded regular-hours bar at each session's open, mid-2023 onward:
    years of history for weekly bars without years of minutes."""
    windows = session_windows_ms_utc(date(2023, 6, 1), _UNIVERSE_LAST)
    rng = np.random.default_rng(seed=2611)
    closes = np.round(100.0 + np.cumsum(rng.normal(0.0, 1.0, len(windows))), 4)
    return tuple(
        (
            window.session_date,
            {
                "timestamp": window.open_ms_utc,
                "open": float(close),
                "high": float(close) + 0.5,
                "low": float(close) - 0.5,
                "close": float(close),
                "volume": 1_000,
                "vwap": float(close),
                "transactions": 10,
            },
        )
        for window, close in zip(windows, closes, strict=True)
    )


def _session_open_bars(from_date: str, to_date: str) -> list[dict[str, Any]]:
    first, last = date.fromisoformat(from_date), date.fromisoformat(to_date)
    return [dict(bar) for day, bar in _session_open_history() if first <= day <= last]


def _aggregate(bars: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Polygon's multi-minute aggregates: epoch-aligned bins of the same minutes."""
    if minutes == 1:
        return bars
    frame = pd.DataFrame(bars)
    span_ms = minutes * _MINUTE_MS
    frame["bin"] = frame["timestamp"] // span_ms * span_ms
    grouped = frame.groupby("bin").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        vwap=("vwap", "last"),
        transactions=("transactions", "sum"),
    )
    return grouped.reset_index().rename(columns={"bin": "timestamp"}).to_dict("records")


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the provider's history floor against a fixed day, so the fixed
    windows below stay inside it whenever the suite runs."""
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: _NOW_MS)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Chart and export read the same provider minutes, each through its own seam."""
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()
    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, to_date, _adjusted, _requested_from, _session: (
            _provider(fetch_from, to_date),
            None,
        ),
    )
    monkeypatch.setattr(
        dataset_router,
        "fetch_bars_chunked",
        lambda _client, _ticker, from_date, to_date, **kwargs: _aggregate(
            _provider(from_date, to_date), kwargs["multiplier"]
        ),
    )
    yield
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()


@pytest.fixture
def chart_frames(monkeypatch: pytest.MonkeyPatch) -> list[pd.DataFrame]:
    """The chart's full-precision indicator frames, before its payload rounds them."""
    frames: list[pd.DataFrame] = []
    compute = chart_service._compute_indicators

    def capture(
        df: pd.DataFrame, indicators: list[dict[str, Any]], trim_from_ts: int | None = None
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        enriched, column_meta = compute(df, indicators, trim_from_ts=trim_from_ts)
        frames.append(enriched)
        return enriched, column_meta

    monkeypatch.setattr(chart_service, "_compute_indicators", capture)
    return frames


@pytest.fixture
def server_time_zone(request: pytest.FixtureRequest) -> Iterator[str]:
    """Run the test with the process-local time zone set to ``request.param``."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _chart(
    session: str,
    indicators: list[dict[str, Any]],
    timeframe: str = "1m",
    window: tuple[str, str] = (_FROM, _TO),
    forward_fill: bool = False,
) -> dict[str, Any]:
    return chart_service.get_chart_data(
        ticker="SPY",
        from_date=window[0],
        to_date=window[1],
        timeframe=timeframe,
        session=session,
        forward_fill=forward_fill,
        indicators=indicators,
    )


def _export(session: str, multiplier: int = 1, window: tuple[str, str] = (_FROM, _TO)) -> pd.DataFrame:
    request = prepare_generation_request(
        DatasetGenerationRequest(
            ticker="SPY",
            from_date=window[0],
            to_date=window[1],
            indicator_entries=_EMAS,
            session=session,
            include_previous_close=False,
            timespan="minute",
            multiplier=multiplier,
        )
    )
    df, _column_meta, _raw_count = dataset_router._fetch_and_process(request)
    return df


def _assert_chart_indicators_match_export(
    chart: dict[str, Any], chart_frame: pd.DataFrame, export: pd.DataFrame
) -> None:
    """The chart's EMA series equal the export's, from the first visible bar on.

    First the full-precision frames the two surfaces compute, at atol=1e-9 —
    the tested fact — then what each publishes: the chart payload through
    ``round(v, 6)``, ``dataset.csv`` through ``:.6f``, so the export's value is
    rounded the same way before the published compare.
    """
    assert [bar["t"] for bar in chart["bars"]] == export["timestamp"].tolist()
    assert chart_frame["timestamp"].tolist() == export["timestamp"].tolist()
    series = {indicator["id"]: indicator["data"] for indicator in chart["indicators"]}
    for chart_id, column in _EMA_COLUMNS.items():
        computed = chart_frame[column].to_numpy(dtype="float64")
        exported = export[column].to_numpy(dtype="float64")
        assert not np.isnan(computed).any(), f"{column} is blank inside the chart's window"
        assert not np.isnan(exported).any(), f"{column} is blank inside the export's window"
        np.testing.assert_allclose(computed, exported, atol=1e-9, rtol=0)

        shown = [point["value"] for point in series[chart_id]]
        np.testing.assert_allclose(
            np.array(shown, dtype="float64"),
            np.array([round(value, 6) for value in export[column].tolist()], dtype="float64"),
            atol=1e-9,
            rtol=0,
        )


# ── Chart ⇄ export parity ───────────────────────────────────────


@pytest.mark.parametrize("window", list(_WINDOWS))
@pytest.mark.parametrize("timeframe", list(_COMPARABLE_TIMEFRAMES))
def test_chart_indicators_match_the_export_for_the_same_window(
    served: None, chart_frames: list[pd.DataFrame], timeframe: str, window: str
) -> None:
    """The #2458 regression, per timeframe: both surfaces warm the same EMA up
    on the same bars, so a multi-minute export no longer starts its lead-in on
    a weekend (the old bars-per-day multiplication) while the chart reached
    back a fixed four calendar days."""
    chart = _chart("rth", _EMAS, timeframe, _WINDOWS[window])

    export = _export("rth", _COMPARABLE_TIMEFRAMES[timeframe], _WINDOWS[window])

    _assert_chart_indicators_match_export(chart, chart_frames[-1], export)


@pytest.mark.parametrize("timeframe", list(_COMPARABLE_TIMEFRAMES))
def test_extended_hours_chart_indicators_match_the_export(
    served: None, chart_frames: list[pd.DataFrame], timeframe: str
) -> None:
    """Pre- and post-market bars feed the same lead-in on both surfaces."""
    chart = _chart("extended", _EMAS, timeframe, _WINDOWS["monday"])

    export = _export("extended", _COMPARABLE_TIMEFRAMES[timeframe], _WINDOWS["monday"])

    _assert_chart_indicators_match_export(chart, chart_frames[-1], export)


def test_a_window_cached_without_indicators_still_warms_them(
    served: None, chart_frames: list[pd.DataFrame]
) -> None:
    """A window first charted bare must not hand its lead-in-free bars to a
    later request for the same window that asks for indicators."""
    _chart("rth", [])

    chart = _chart("rth", _EMAS)

    _assert_chart_indicators_match_export(chart, chart_frames[-1], _export("rth"))


@pytest.mark.parametrize("server_time_zone", ["UTC", "Asia/Tokyo", "Pacific/Honolulu"], indirect=True)
def test_the_chart_window_ignores_the_server_time_zone(
    served: None, chart_frames: list[pd.DataFrame], server_time_zone: str
) -> None:
    """The trim reads the shared ET-midnight window start, never the server's
    local midnight: east of ET that leaked the prior session's bars into the
    window, west of it that cut the picked day's pre-market."""
    chart = _chart("extended", _EMAS)

    assert [bar["t"] for bar in chart["bars"]] == [bar["timestamp"] for bar in _provider(_FROM, _TO)]
    _assert_chart_indicators_match_export(chart, chart_frames[-1], _export("extended"))


# ── What the lead-in may and may not change ─────────────────────


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1D", "1W", "1M"])
@pytest.mark.parametrize("session", ["rth", "extended"])
def test_the_lead_in_changes_indicator_values_only(served: None, session: str, timeframe: str) -> None:
    """The warm-up sessions reach the indicators and nothing else: the bars and
    the quality report describe the picked window with or without them — a
    weekly or monthly bar never absorbs lead-in days before the window."""
    bare = _chart(session, [], timeframe)
    # Each arm computes its own window; neither may be served the other's.
    chart_service._resample_cache.clear()
    warm = _chart(session, _EMAS, timeframe)

    assert warm["bars"] == bare["bars"]
    assert warm["quality"] == bare["quality"]
    assert warm["quality"]["missing_session_dates"] == []
    assert warm["quality"]["session_coverage_pct"] == 100.0


def test_the_quality_report_counts_flat_and_malformed_bars_in_the_window_only(
    served: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat bar and an OHLC violation in the warm-up lead-in are never shown,
    so the report must not count them; the same defects inside the window are."""
    rth_minute = _PRE_MARKET_MINUTES + 100
    lead_in_bars = _session_bars(date(2026, 1, 6))
    window_bars = _session_bars(date(2026, 1, 7))
    defects = {
        lead_in_bars[rth_minute]["timestamp"]: "flat",
        lead_in_bars[rth_minute + 1]["timestamp"]: "malformed",
        window_bars[rth_minute]["timestamp"]: "flat",
        window_bars[rth_minute + 1]["timestamp"]: "malformed",
    }

    def defective_provider(from_date: str, to_date: str) -> list[dict[str, Any]]:
        bars = _provider(from_date, to_date)
        for bar in bars:
            defect = defects.get(bar["timestamp"])
            if defect == "flat":
                bar.update(open=bar["close"], high=bar["close"], low=bar["close"], volume=0)
            elif defect == "malformed":
                bar.update(high=bar["close"] - 1.0)
        return bars

    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, to_date, *_rest: (defective_provider(fetch_from, to_date), None),
    )

    chart = _chart("rth", _EMAS)

    assert chart["quality"]["flat_bars_detected"] == 1
    assert chart["quality"]["ohlc_violations_detected"] == 1


@pytest.mark.parametrize("session", ["rth", "extended"])
def test_raw_bars_counts_the_window_only(served: None, session: str) -> None:
    """The quality panel's Raw Bars sits beside cards that describe the picked
    window, so it counts the provider minutes of that window — the warm-up
    lead-in fetched before it is never shown and never counted."""
    window_minutes = len(_provider(_FROM, _TO))

    chart = _chart(session, _EMAS)

    assert chart["indicator_warmup"]["lead_in_bars"] > 0
    assert chart["quality"]["raw_bar_count"] == window_minutes


def test_forward_fill_reports_only_the_windows_synthetic_bars(
    served: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forward-fill spans the lead-in too; the report still counts only the
    synthetic bars the window shows."""
    rth_minute = _PRE_MARKET_MINUTES + 100
    lead_in_gap = _session_bars(date(2026, 1, 6))[rth_minute]["timestamp"]
    window_gap = _session_bars(date(2026, 1, 7))[rth_minute]["timestamp"]

    def holey_provider(from_date: str, to_date: str) -> list[dict[str, Any]]:
        return [bar for bar in _provider(from_date, to_date) if bar["timestamp"] not in {lead_in_gap, window_gap}]

    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, to_date, *_rest: (holey_provider(fetch_from, to_date), None),
    )

    chart = _chart("rth", _EMAS, forward_fill=True)

    assert chart["quality"]["synthetic_bars"] == 1
    assert [bar["t"] for bar in chart["bars"] if bar.get("synthetic")] == [window_gap]


@pytest.mark.parametrize("session", ["rth", "extended"])
def test_a_window_with_no_bars_is_no_data_though_its_lead_in_has_some(
    served: None, monkeypatch: pytest.MonkeyPatch, session: str
) -> None:
    """A lead-in alone is not a chart: when the provider holds the warm-up
    sessions but none of the picked window's, the answer is NO_DATA."""
    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, _to_date, *_rest: (_provider(fetch_from, "2026-01-06"), None),
    )

    chart = _chart(session, _EMAS)

    assert chart["error_code"] == "NO_DATA"


# ── Where history runs out: said, never silent ──────────────────


def test_a_fully_warmed_chart_carries_no_warmup_note(served: None) -> None:
    chart = _chart("rth", _EMAS)

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert warmup.note is None
    assert warmup.cold_bars == 0
    assert warmup.uncomputed == []
    assert warmup.lead_in_bars >= warmup.required_bars == 1000


def test_a_daily_ema20_chart_with_enough_history_carries_no_warmup_note(served: None) -> None:
    """#2611: sized from the request, a daily EMA-20 chart warms up on 100
    sessions and the held universe reaches further back, so no note. On master
    the fixed 200-bar floor demanded 1,000 sessions and every daily chart
    reported a warm-up shortfall it did not have."""
    chart = _chart("rth", _EMAS[:1], "1D")

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert (warmup.required_bars, warmup.cold_bars, warmup.uncomputed) == (100, 0, [])
    assert warmup.lead_in_bars >= 100
    assert warmup.note is None


def test_a_weekly_ema20_chart_that_really_lacks_history_still_says_so(served: None) -> None:
    """Right-sizing the lead-in does not mean always satisfying it: EMA-20 on
    weekly bars warms up on 100 weekly bars — 500 sessions, most of a year —
    and the held universe holds 23 of them, so every visible value is short
    and the note says so."""
    chart = _chart("rth", _EMAS[:1], "1W")

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert (warmup.required_bars, warmup.lead_in_bars) == (100, 23)
    assert warmup.cold_bars == len(chart["bars"])
    assert warmup.note is not None
    assert "not fully warmed up" in warmup.note


def test_a_weekly_ema20_chart_with_enough_history_carries_no_warmup_note(
    served: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2611: EMA-20 on weekly bars warms up on 100 weekly bars — 500 sessions,
    which the provider's five years hold — so no note. The 200-bar floor asked
    for 1,000 weekly bars, past the provider's history floor, and noted a
    shortfall on every weekly chart. One regular-hours bar per session back to
    mid-2023 is history enough: a weekly bar is built from whatever minutes
    its sessions hold."""
    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, to_date, *_rest: (_session_open_bars(fetch_from, to_date), None),
    )

    chart = _chart("rth", _EMAS[:1], "1W")

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert (warmup.required_bars, warmup.cold_bars, warmup.uncomputed) == (100, 0, [])
    assert warmup.lead_in_bars >= 100
    assert warmup.note is None


@pytest.mark.parametrize(
    ("indicator", "required_bars"),
    [
        # No params: sized on the catalog default, RSI-14 at 5 × 2 × 14.
        ({"name": "rsi", "params": {}}, 140),
        # No whole-number length: today's 200-bar floor.
        ({"name": "psar", "params": {"af0": 0.02, "af": 0.02, "max_af": 0.2}}, 1000),
        ({"name": "obv", "params": {}}, 1000),
    ],
)
def test_a_request_without_a_sized_length_still_warms_up_and_says_when_it_falls_short(
    served: None, indicator: IndicatorEntry, required_bars: int
) -> None:
    """#2611 review P1-2: a request with no params, and one whose parameters
    are all fractions, was sized at zero — the chart read no lead-in, every
    visible value started cold, and the note never said so. Each now warms up
    on a real lead-in, and where the held history falls short the note says
    so."""
    held_sessions = len(expected_sessions(_UNIVERSE_FIRST, date(2026, 1, 9)))

    chart = _chart("rth", [indicator], "1D", _WINDOWS["monday"])

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert (warmup.required_bars, warmup.lead_in_bars, warmup.cold_bars) == (required_bars, held_sessions, 2)
    assert warmup.note is not None
    assert "not fully warmed up" in warmup.note


def test_a_daily_chart_says_which_values_the_held_history_could_not_warm_up(served: None) -> None:
    """Owner decision 2026-09-29: warm up per timeframe, and where the provider
    simply does not hold enough history, say so. EMA-200 on daily bars needs
    1,000 sessions before the window; the provider holds far fewer, so every
    visible value is short of its warm-up and EMA-200 cannot be computed at
    all — neither a silent blank nor a silently dropped series."""
    window = _WINDOWS["monday"]
    held_sessions = len(expected_sessions(_UNIVERSE_FIRST, date(2026, 1, 9)))

    chart = _chart("rth", _EMAS, "1D", window)

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert (warmup.required_bars, warmup.lead_in_bars, warmup.cold_bars) == (1000, held_sessions, 2)
    assert [entry.model_dump() for entry in warmup.uncomputed] == [{"name": "ema", "params": {"length": 200}}]
    assert warmup.note == (
        "Indicator values on all 2 bars are not fully warmed up: the chart warms its indicators up on "
        f"1,000 bars of earlier history, and only {held_sessions} bars are available before this range. "
        f"Not shown: EMA (length=200) could not be computed from the {held_sessions + 2} bars available."
    )
    ema_20 = next(indicator for indicator in chart["indicators"] if indicator["id"] == "ema_20")
    assert all(point["value"] is not None for point in ema_20["data"])


def test_a_single_held_bar_before_the_range_is_said_in_the_singular(
    served: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note is backend-authored prose: one bar of lead-in "is" available."""
    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, to_date, *_rest: (_provider(max(fetch_from, "2026-01-09"), to_date), None),
    )

    chart = _chart("rth", _EMAS[:1], "1D", _WINDOWS["monday"])

    warmup = ChartDataResponse.model_validate(chart).indicator_warmup
    assert warmup is not None
    assert warmup.lead_in_bars == 1
    assert warmup.required_bars == 100
    assert warmup.note == (
        "Indicator values on all 2 bars are not fully warmed up: the chart warms its indicators up on "
        "100 bars of earlier history, and only 1 bar is available before this range. "
        "Not shown: EMA (length=20) could not be computed from the 3 bars available."
    )


# ── What a request-sized lead-in may change (#2611) ─────────────

#: Every request warmed up on at least this many bars before #2611: a 200-bar
#: lookback floor at the ×5 multiplier.
_TODAYS_LEAD_IN_BARS = 1_000
_VISIBLE_BARS = 250


@cache
def _daily_bars() -> pd.DataFrame:
    """Seeded daily OHLCV holding today's 1,000-bar lead-in and a 250-bar
    window. A random walk, so an indicator's leftover seed shows up as a
    different number instead of hiding in flat prices."""
    rng = np.random.default_rng(seed=2611)
    count = _TODAYS_LEAD_IN_BARS + _VISIBLE_BARS
    close = 200.0 * np.exp(np.cumsum(rng.normal(0.0, 0.015, count)))
    open_ = close * (1.0 + rng.normal(0.0, 0.004, count))
    return pd.DataFrame(
        {
            "timestamp": np.arange(count, dtype="int64") * 86_400_000,
            "open": open_,
            "high": np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.008, count))),
            "low": np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.008, count))),
            "close": close,
            "volume": rng.integers(1_000_000, 5_000_000, count).astype("float64"),
        }
    )


def _visible_values(entry: IndicatorEntry, lead_in_bars: int) -> dict[str, np.ndarray]:
    """The window's values, computed over ``lead_in_bars`` of lead-in through
    the compute-then-trim path the chart and the export share."""
    bars = _daily_bars()
    frame = bars.iloc[_TODAYS_LEAD_IN_BARS - lead_in_bars :].reset_index(drop=True)
    first_visible_ms = int(bars["timestamp"].iloc[_TODAYS_LEAD_IN_BARS])
    df, column_meta = calculate_indicators_then_trim(frame, [dict(entry)], trim_from_ts=first_visible_ms)
    assert len(df) == _VISIBLE_BARS
    return {meta["column"]: df[meta["column"]].to_numpy(dtype="float64") for meta in column_meta}


@pytest.mark.parametrize(
    ("entry", "atol", "rtol"),
    [
        # Finite window (m = 1): the value reads only its own window — exact.
        ({"name": "sma", "params": {"length": 20}}, 1e-9, 0.0),
        # EMA-smoothed (m = 1): ((N−1)/(N+1))^{5N} ≤ e^−10 of the seed survives
        # 5N bars; the owner's bound on a price scale is 1e-4 of the value
        # (20 seeded paths: at most 5e-6).
        ({"name": "ema", "params": {"length": 20}}, 0.0, 1e-4),
        # Wilder-smoothed (m = 2): (1 − 1/N)^{10N} ≤ e^−10; the owner's bound on
        # a 0–100 oscillator is 0.01 points (20 seeded paths: at most 6.8e-3;
        # at m = 1 it was 0.76).
        ({"name": "rsi", "params": {"length": 14}}, 1e-2, 0.0),
        # Double-Wilder (m = 3): the second RMA carries the first one's seed as
        # (k/N)·e^{−k/N}, 15·e^−15 at 15N; 0.01 points on ADX, ADXR, +DI and −DI
        # (20 seeded paths: at most 4.6e-4; at m = 1 it was 3.8).
        ({"name": "adx", "params": {"length": 14}}, 1e-2, 0.0),
        # Path-dependent: a cumulative sum never forgets, so OBV keeps today's
        # 1,000-bar lead-in and today's values exactly.
        ({"name": "obv", "params": {}}, 1e-9, 0.0),
    ],
)
def test_a_request_sized_lead_in_keeps_warmed_values_within_their_family_bound(
    entry: IndicatorEntry, atol: float, rtol: float
) -> None:
    """#2611 (owner decision 2026-09-30, "reduce the accuracy needed at
    warmup, judiciously"): the lead-in the policy sizes from the request gives
    the window the values today's 1,000-bar lead-in gives, within the
    tolerance the indicator's smoothing family documents — derivation in
    ``indicator_warmup_policy``, per-indicator measurements in
    ``docs/references/data-lab-indicator-warmup.md``."""
    lookback = requested_indicator_warmup_lookback([entry], INDICATOR_CONFIGS)
    lead_in = resolve_indicator_window(
        "2026-01-12", max_lookback=lookback, bar_minutes=bar_minutes_for("day", 1)
    ).warmup_bars

    sized = _visible_values(entry, lead_in)
    todays = _visible_values(entry, _TODAYS_LEAD_IN_BARS)

    assert sized
    assert sized.keys() == todays.keys()
    for column, values in sized.items():
        assert not np.isnan(values).any(), f"{column} is blank inside the window"
        np.testing.assert_allclose(values, todays[column], atol=atol, rtol=rtol, err_msg=column)


# ── The one resolver ────────────────────────────────────────────


def _lead_in_sessions(window_from: str, fetch_from: str) -> list[date]:
    first = date.fromisoformat(window_from)
    return [day for day in expected_sessions(date.fromisoformat(fetch_from), first) if day < first]


def test_a_monday_window_warms_up_on_the_sessions_before_its_weekend() -> None:
    """Three regular sessions hold the 1,000 one-minute bars EMA-200 warms up
    on; for a Monday they are the Wednesday, Thursday and Friday before it —
    never a lead-in that starts on the Saturday."""
    window = resolve_indicator_window("2026-01-12", max_lookback=200, bar_minutes=1)

    assert window.fetch_from == "2026-01-07"
    assert window.window_start_ms == et_midnight_ms(date(2026, 1, 12))
    assert window.warmup_bars == 1000


def test_an_early_close_in_the_lead_in_counts_its_shorter_session() -> None:
    """The Friday after Thanksgiving closes at 13:00 ET: its 210 minutes plus
    two full sessions fall short of 1,000 bars, so the lead-in takes the
    Monday before too."""
    window = resolve_indicator_window("2025-12-01", max_lookback=200, bar_minutes=1)

    assert window.fetch_from == "2025-11-24"


@pytest.mark.parametrize("bar_minutes", [1, 5, 15, 30, 60, 240])
@pytest.mark.parametrize("from_date", ["2026-01-07", "2026-01-12", "2025-12-01"])
def test_an_intraday_lead_in_holds_just_enough_bars_of_its_own_length(from_date: str, bar_minutes: int) -> None:
    """Bars per session: a bar length that evenly divides the session span
    counts its exact bins; anything else counts its floor, because the bin
    straddling the open or close may be dropped by the regular-hours filter
    (Polygon serves 6 or 7 hourly bins per regular session; #2611). The
    lead-in holds the warm-up under that guaranteed count, and one session
    fewer would not."""
    window = resolve_indicator_window(from_date, max_lookback=200, bar_minutes=bar_minutes)

    spans = {
        session.session_date: (session.close_ms_utc - session.open_ms_utc) // (bar_minutes * _MINUTE_MS)
        for session in session_windows_ms_utc(date.fromisoformat(window.fetch_from), date.fromisoformat(from_date))
    }
    bars = [spans[day] for day in _lead_in_sessions(from_date, window.fetch_from)]
    assert sum(bars) >= window.warmup_bars > sum(bars[1:])


def test_the_lead_in_is_sized_from_the_requested_indicators_not_a_fixed_floor() -> None:
    """#2611: EMA-20 on daily bars warms up on 100 sessions (5 × 20), not the
    1,000 a fixed 200-bar lookback floor forced on every daily, weekly and
    monthly chart."""
    lookback = requested_indicator_warmup_lookback(_EMAS[:1], INDICATOR_CONFIGS)

    window = resolve_indicator_window("2026-01-12", max_lookback=lookback, bar_minutes=bar_minutes_for("day", 1))

    assert window.warmup_bars == 100
    assert len(_lead_in_sessions("2026-01-12", window.fetch_from)) == 100


def test_hour_bars_count_the_guaranteed_six_per_regular_session() -> None:
    """#2611: Polygon serves 6 or 7 hourly bins per regular session after the
    regular-hours filter, so the lead-in is sized on the guaranteed 6 and can
    never be one bin short. 20 warm-up bars take Tue 2026-01-06 through Fri
    2026-01-09 (4 × 6 = 24); one session fewer holds only 18."""
    window = resolve_indicator_window("2026-01-12", max_lookback=4, bar_minutes=60)

    assert window.warmup_bars == 20
    assert window.fetch_from == "2026-01-06"


def test_daily_bars_warm_up_on_one_session_each() -> None:
    window = resolve_indicator_window("2026-01-12", max_lookback=200, bar_minutes=bar_minutes_for("day", 1))

    assert len(_lead_in_sessions("2026-01-12", window.fetch_from)) == 1000


@pytest.mark.parametrize("timeframe", ["1W", "1M"])
def test_the_lead_in_never_reaches_past_the_providers_history_floor(timeframe: str) -> None:
    """1,000 weekly or monthly bars predate what the provider serves; the lead-in
    stops at its floor rather than asking for history that fails the fetch."""
    minutes = chart_service.TIMEFRAME_DEFS[timeframe]["minutes"]

    window = resolve_indicator_window("2026-01-12", max_lookback=200, bar_minutes=minutes)

    assert window.fetch_from == polygon_history_floor(date(2026, 9, 29)).isoformat()


def test_a_window_before_the_history_floor_has_no_lead_in() -> None:
    window = resolve_indicator_window("2021-06-01", max_lookback=200, bar_minutes=1)

    assert window.fetch_from == "2021-06-01"
    assert window.warmup_bars == 1000


def test_no_indicators_need_no_lead_in() -> None:
    window = resolve_indicator_window("2026-01-12", max_lookback=0, bar_minutes=1)

    assert (window.fetch_from, window.warmup_bars) == ("2026-01-12", 0)


@pytest.mark.parametrize(
    ("timespan", "multiplier", "timeframe"),
    [
        ("minute", 1, "1m"),
        ("minute", 5, "5m"),
        ("minute", 15, "15m"),
        ("minute", 30, "30m"),
        ("hour", 1, "1h"),
        ("hour", 4, "4h"),
        ("day", 1, "1D"),
        ("week", 1, "1W"),
        ("month", 1, "1M"),
    ],
)
def test_an_export_bar_is_as_long_as_the_chart_bar_of_the_same_span(
    timespan: str, multiplier: int, timeframe: str
) -> None:
    assert bar_minutes_for(timespan, multiplier) == chart_service.TIMEFRAME_DEFS[timeframe]["minutes"]
