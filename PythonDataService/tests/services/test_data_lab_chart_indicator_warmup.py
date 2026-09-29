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

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.data_lake.polygon_fetcher import polygon_history_floor
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc
from app.models.requests import DatasetGenerationRequest
from app.routers import dataset as dataset_router
from app.routers import indicators as indicators_router
from app.schemas.chart import ChartDataResponse
from app.services import chart_service, dataset_service
from app.services.dataset_plan_service import prepare_generation_request
from app.services.dataset_service import bar_minutes_for, resolve_indicator_window
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
    # raw_bar_count counts what was fetched, lead-in included, by design.
    warm_quality = {key: value for key, value in warm["quality"].items() if key != "raw_bar_count"}
    bare_quality = {key: value for key, value in bare["quality"].items() if key != "raw_bar_count"}
    assert warm_quality == bare_quality
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
    """Bars per session divide by the bar length (the old sizing multiplied):
    the lead-in holds the warm-up, and one session fewer would not."""
    window = resolve_indicator_window(from_date, max_lookback=200, bar_minutes=bar_minutes)

    spans = {
        session.session_date: -(-(session.close_ms_utc - session.open_ms_utc) // (bar_minutes * _MINUTE_MS))
        for session in session_windows_ms_utc(date.fromisoformat(window.fetch_from), date.fromisoformat(from_date))
    }
    bars = [spans[day] for day in _lead_in_sessions(from_date, window.fetch_from)]
    assert sum(bars) >= window.warmup_bars > sum(bars[1:])


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


# ── The indicator table trims at the same window start ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("server_time_zone", ["Asia/Tokyo", "Pacific/Honolulu"], indirect=True)
async def test_the_indicator_table_window_ignores_the_server_time_zone(
    monkeypatch: pytest.MonkeyPatch, server_time_zone: str
) -> None:
    """``/generate-table`` trimmed its lead-in at the server's local midnight:
    east of ET that kept the prior session's afternoon, west of it that cut the
    picked day's pre-market. It trims at the resolver's ET-midnight start now."""
    monkeypatch.setattr(
        indicators_router.polygon_client,
        "fetch_aggregates",
        lambda *, from_date, to_date, **_kwargs: _provider(from_date, to_date),
    )
    api = FastAPI()
    api.include_router(indicators_router.router, prefix="/api/indicators")

    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post(
            "/api/indicators/generate-table",
            json={"symbol": "SPY", "from_date": _FROM, "to_date": _TO, "session": "extended"},
        )

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["time"] for row in rows] == [bar["timestamp"] for bar in _provider(_FROM, _TO)]
