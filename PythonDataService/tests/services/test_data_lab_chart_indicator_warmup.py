"""Data Lab chart indicators warm up across the lead-in before the trim (#2458).

The dataset export computes indicators over the picked window plus its
warm-up lead-in and only then trims to the window. The chart trimmed first —
at the server's local midnight — and built its regular-hours mask from the
visible dates alone, so the lead-in never reached an indicator: every EMA
restarted cold at the chart's first visible bar and disagreed with the
export's for the same window.

Session boundaries come from the canonical NYSE calendar; the only fixed
offsets are the synthetic extended-hours spans the fake provider serves
around each scheduled session.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.models.requests import DatasetGenerationRequest
from app.routers import dataset as dataset_router
from app.services import chart_service
from app.services.dataset_plan_service import prepare_generation_request

#: Wed 2026-01-07 → Thu 2026-01-08. Chart and export both reach four calendar
#: days back for the EMA-200 lead-in, so Mon 01-05 and Tue 01-06 are warm-up
#: sessions. It is EST, so Tuesday's 19:00–20:00 ET post-market bars carry
#: Wednesday's UTC-morning timestamps.
_FROM, _TO = "2026-01-07", "2026-01-08"
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


def _build_universe() -> dict[date, list[dict[str, Any]]]:
    """Contiguous extended-hours minute bars (bar-start stamps) per session.

    Prices are one seeded random walk across every session, so an EMA carries
    its whole history into its value — a lead-in that never reached it shows
    up as a different number, never as a coincidence of flat prices.
    """
    rng = np.random.default_rng(seed=2458)
    sessions: dict[date, list[dict[str, Any]]] = {}
    close = 100.0
    for window in session_windows_ms_utc(date(2026, 1, 2), date(2026, 1, 9)):
        first = window.open_ms_utc - _PRE_MARKET_MINUTES * _MINUTE_MS
        last = window.close_ms_utc + _POST_MARKET_MINUTES * _MINUTE_MS
        bars = []
        for ts in range(first, last, _MINUTE_MS):
            open_ = close
            close = round(open_ + float(rng.normal(0.0, 0.05)), 4)
            bars.append(
                {
                    "timestamp": ts,
                    "open": open_,
                    "high": max(open_, close) + 0.02,
                    "low": min(open_, close) - 0.02,
                    "close": close,
                    "volume": int(rng.integers(100, 10_000)),
                    "vwap": close,
                    "transactions": 10,
                }
            )
        sessions[window.session_date] = bars
    return sessions


_UNIVERSE = _build_universe()


def _provider(from_date: str, to_date: str) -> list[dict[str, Any]]:
    """What the provider serves for an inclusive day range: every hour it has."""
    first, last = date.fromisoformat(from_date), date.fromisoformat(to_date)
    return [dict(bar) for day, bars in _UNIVERSE.items() if first <= day <= last for bar in bars]


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Chart and export read the same provider bars, each through its own seam."""
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
        lambda _client, _ticker, from_date, to_date, **_kwargs: _provider(from_date, to_date),
    )
    yield
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()


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


def _chart(session: str, indicators: list[dict[str, Any]], timeframe: str = "1m") -> dict[str, Any]:
    return chart_service.get_chart_data(
        ticker="SPY",
        from_date=_FROM,
        to_date=_TO,
        timeframe=timeframe,
        session=session,
        indicators=indicators,
    )


def _export(session: str) -> pd.DataFrame:
    request = prepare_generation_request(
        DatasetGenerationRequest(
            ticker="SPY",
            from_date=_FROM,
            to_date=_TO,
            indicator_entries=_EMAS,
            session=session,
            include_previous_close=False,
        )
    )
    df, _column_meta, _raw_count = dataset_router._fetch_and_process(request)
    return df


def _assert_chart_indicators_match_export(chart: dict[str, Any], export: pd.DataFrame) -> None:
    """The chart's EMA series equal the export's, from the first visible bar on.

    Both surfaces publish indicator values at 6 dp — the chart payload through
    ``round(v, 6)``, ``dataset.csv`` through ``:.6f`` — so the export's
    full-precision value is rounded the same way before the strict compare.
    """
    assert [bar["t"] for bar in chart["bars"]] == export["timestamp"].tolist()
    series = {indicator["id"]: indicator["data"] for indicator in chart["indicators"]}
    for chart_id, column in _EMA_COLUMNS.items():
        shown = [point["value"] for point in series[chart_id]]
        assert shown[0] is not None, f"{chart_id} restarted cold at the first visible bar"
        assert None not in shown
        np.testing.assert_allclose(
            np.array(shown, dtype="float64"),
            np.array([round(value, 6) for value in export[column].tolist()], dtype="float64"),
            atol=1e-9,
            rtol=0,
        )


@pytest.mark.parametrize("session", ["rth", "extended"])
def test_chart_indicators_match_the_export_for_the_same_window(served: None, session: str) -> None:
    """The #2458 regression: the chart's first visible EMA values were cold."""
    _assert_chart_indicators_match_export(_chart(session, _EMAS), _export(session))


def test_a_window_cached_without_indicators_still_warms_them(served: None) -> None:
    """A window first charted bare must not hand its lead-in-free bars to a
    later request for the same window that asks for indicators."""
    _chart("rth", [])

    chart = _chart("rth", _EMAS)

    _assert_chart_indicators_match_export(chart, _export("rth"))


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


def test_forward_fill_reports_only_the_windows_synthetic_bars(
    served: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forward-fill now spans the lead-in too; the report still counts only
    the synthetic bars the window shows."""
    rth_minute = _PRE_MARKET_MINUTES + 100
    lead_in_gap = _UNIVERSE[date(2026, 1, 6)][rth_minute]["timestamp"]
    window_gap = _UNIVERSE[date(2026, 1, 7)][rth_minute]["timestamp"]

    def holey_provider(from_date: str, to_date: str) -> list[dict[str, Any]]:
        return [bar for bar in _provider(from_date, to_date) if bar["timestamp"] not in {lead_in_gap, window_gap}]

    monkeypatch.setattr(
        chart_service,
        "_fetch_chart_bars",
        lambda _ticker, fetch_from, to_date, *_rest: (holey_provider(fetch_from, to_date), None),
    )

    chart = chart_service.get_chart_data(
        ticker="SPY",
        from_date=_FROM,
        to_date=_TO,
        timeframe="1m",
        session="rth",
        forward_fill=True,
        indicators=_EMAS,
    )

    assert chart["quality"]["synthetic_bars"] == 1
    assert [bar["t"] for bar in chart["bars"] if bar.get("synthetic")] == [window_gap]


@pytest.mark.parametrize("server_time_zone", ["UTC", "Asia/Tokyo", "Pacific/Honolulu"], indirect=True)
def test_the_chart_window_ignores_the_server_time_zone(served: None, server_time_zone: str) -> None:
    """The trim reads the shared ET-midnight window start, never the server's
    local midnight: east of ET that leaked the prior session's bars into the
    window, west of it that cut the picked day's pre-market."""
    chart = _chart("extended", _EMAS)

    assert [bar["t"] for bar in chart["bars"]] == [bar["timestamp"] for bar in _provider(_FROM, _TO)]
    _assert_chart_indicators_match_export(chart, _export("extended"))
