"""Data Lab picked-date parity across chart, return study and export (#2457).

The Data Lab commits one numeric window per pick — start at the UTC midnight
of the first picked date, end at the final UTC instant of the last
(``data-lab-request-mapper.ts``). Chart, return study and dataset export must
turn that same committed pair into the same ET session window through one
shared resolver (``chart_service.resolve_request_dates``), and the export must
trim its rows to the requested start. Before #2457 the export re-floored the
committed instants by ET date, so every picker-made export silently gained the
previous session: a one-day pick showed 390 chart rows but exported 780.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_open_ms_utc,
    session_windows_ms_utc,
)
from app.models.requests import DatasetGenerationRequest
from app.routers import dataset as dataset_router
from app.services.chart_service import resolve_request_dates
from app.services.dataset_plan_service import prepare_generation_request

_EPOCH = date(1970, 1, 1)
_MS_PER_DAY = 86_400_000

#: A one-day pick of Tuesday 2026-06-16. The committed start (UTC midnight)
#: is 20:00 ET of Monday 2026-06-15 — a trading day — which is exactly the
#: instant the old ET-date export flooring read as "include Monday too".
_PICK = "2026-06-16"
_PREV_SESSION = "2026-06-15"


def _utc_midnight_ms(day: str) -> int:
    return (date.fromisoformat(day) - _EPOCH).days * _MS_PER_DAY


def _utc_day_end_ms(day: str) -> int:
    return _utc_midnight_ms(day) + _MS_PER_DAY - 1


def _picker_window(day: str) -> tuple[int, int]:
    """The numeric pair the Data Lab commits for a picked day (mapper convention)."""
    return _utc_midnight_ms(day), _utc_day_end_ms(day)


def _session_minutes(from_iso: str, to_iso: str) -> int:
    windows = session_windows_ms_utc(date.fromisoformat(from_iso), date.fromisoformat(to_iso))
    return sum((w.close_ms_utc - w.open_ms_utc) // 60_000 for w in windows)


def _generation_request(day: str, start: int, end: int) -> DatasetGenerationRequest:
    return DatasetGenerationRequest(
        ticker="SPY",
        from_date=day,
        to_date=day,
        start_ms_utc=start,
        end_ms_utc=end,
        indicator_entries=[],
        include_previous_close=False,
    )


# ── One shared resolver ─────────────────────────────────────────


def test_one_day_pick_exports_exactly_the_picked_session() -> None:
    """The #2457 regression: 390 chart rows must not become 780 export rows."""
    start, end = _picker_window(_PICK)

    chart_from, chart_to = resolve_request_dates(_PICK, _PICK, start, end)
    export = prepare_generation_request(_generation_request(_PICK, start, end))

    assert (chart_from, chart_to) == (_PICK, _PICK)
    assert (export.from_date, export.to_date) == (_PICK, _PICK)
    # A regular NYSE session is 390 minutes; two sessions would be 780.
    assert _session_minutes(chart_from, chart_to) == 390
    assert _session_minutes(export.from_date, export.to_date) == 390


def test_chart_study_and_export_agree_on_the_picked_sessions() -> None:
    """Chart, return study and export resolve one pick to one session set."""
    # A four-day pick spanning a weekend: Fri 2026-06-12 → Tue 2026-06-16.
    pick_from, pick_to = "2026-06-12", "2026-06-16"
    start, end = _picker_window(pick_from), _picker_window(pick_to)
    committed = (start[0], end[1])

    chart_from, chart_to = resolve_request_dates(pick_from, pick_to, *committed)
    # The return study resolves its wire window through the same authority.
    study_from, study_to = resolve_request_dates(None, None, *committed)
    export = prepare_generation_request(_generation_request("2026-06-16", *committed))

    expected = [d.isoformat() for d in expected_sessions(date.fromisoformat(pick_from), date.fromisoformat(pick_to))]
    assert [chart_from, chart_to] == [study_from, study_to] == [pick_from, pick_to]
    assert [export.from_date, export.to_date] == [pick_from, pick_to]
    assert expected == ["2026-06-12", "2026-06-15", "2026-06-16"]
    for from_iso, to_iso in (
        (chart_from, chart_to),
        (study_from, study_to),
        (export.from_date, export.to_date),
    ):
        assert [w.session_date.isoformat() for w in session_windows_ms_utc(date.fromisoformat(from_iso), date.fromisoformat(to_iso))] == expected


# ── Plan receipt ────────────────────────────────────────────────


@pytest.fixture
def api() -> FastAPI:
    app = FastAPI()
    app.include_router(dataset_router.router, prefix="/api/dataset")
    return app


@pytest.mark.asyncio
async def test_plan_receipt_lists_exactly_the_picked_sessions(api: FastAPI) -> None:
    """The plan receipt the UI renders names the same sessions the chart shows.

    Fri 2026-06-12 → Tue 2026-06-16 picked: the committed start (UTC midnight
    of 06-12) is 20:00 ET of Thursday 06-11 — a trading day the old ET-date
    flooring pulled into the receipt as a fourth session.
    """
    payload: dict[str, Any] = {
        "ticker": "SPY",
        "from_date": "2026-06-12",
        "to_date": "2026-06-16",
        "start_ms_utc": _utc_midnight_ms("2026-06-12"),
        "end_ms_utc": _utc_day_end_ms("2026-06-16"),
    }
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/api/dataset/plan", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["exchange_sessions"] == ["2026-06-12", "2026-06-15", "2026-06-16"]


# ── Export trims to the requested start ─────────────────────────


def _rth_minute_bars(day: str, count: int) -> list[dict[str, Any]]:
    """Contiguous minute bars (bar close timestamps) from the session open."""
    open_ms = session_windows_ms_utc(date.fromisoformat(day), date.fromisoformat(day))[0].open_ms_utc
    bars = []
    for i in range(1, count + 1):
        ts = open_ms + i * 60_000
        bars.append(
            {
                "timestamp": ts,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1_000,
                "vwap": 100.2,
                "transactions": 10,
            }
        )
    return bars


def test_export_rows_trim_to_the_requested_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-session numeric start drops the bars before it (#2457 trim rule)."""
    bars = _rth_minute_bars(_PICK, 30)
    open_ms = bars[0]["timestamp"] - 60_000
    start_ms_utc = open_ms + 10 * 60_000  # 10 minutes into the session

    monkeypatch.setattr(
        dataset_router, "fetch_bars_chunked", lambda *a, **k: list(bars), raising=True
    )
    request = _generation_request(_PICK, start_ms_utc, _utc_day_end_ms(_PICK))
    request = prepare_generation_request(request)

    df, _column_meta, _raw_count = dataset_router._fetch_and_process(request)

    assert len(df) == 21  # bars at open+10 … open+30 inclusive
    assert int(df["timestamp"].min()) == start_ms_utc
    assert int(df["timestamp"].max()) == open_ms + 30 * 60_000


# ── #2524 review follow-ups ───────────────────────────────────────


def test_export_rows_stop_at_the_exclusive_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The numeric end is exclusive by contract: a mid-session end must not
    ship the rest of that session just because the day-granular fetch
    cannot split it (#2524 review)."""
    bars = _rth_minute_bars(_PICK, 30)
    open_ms = bars[0]["timestamp"] - 60_000
    end_ms_utc = open_ms + 20 * 60_000  # mid-session, exclusive

    monkeypatch.setattr(dataset_router, "fetch_bars_chunked", lambda *a, **k: list(bars), raising=True)
    request = _generation_request(_PICK, _utc_midnight_ms(_PICK), end_ms_utc)
    request = prepare_generation_request(request)

    df, _column_meta, _raw_count = dataset_router._fetch_and_process(request)

    assert int(df["timestamp"].max()) < end_ms_utc
    assert len(df) == 19  # bars at open+1 … open+19


def test_the_picked_sessions_postmarket_tail_of_the_prior_day_is_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During EST the prior session's 19:00–20:00 ET post-market bars carry
    timestamps from the picked date's UTC morning (00:00–01:00 UTC); a
    UTC-midnight trim kept them. The trim now anchors to ET midnight of the
    picked date (#2524 review)."""
    picked = "2026-01-06"  # Tuesday; EST, so 19:00 ET == 00:00 UTC next day
    prior = date(2026, 1, 5)
    prior_close = session_windows_ms_utc(prior, prior)[0].close_ms_utc  # 16:00 ET
    bars = [
        {  # 19:00 ET post-market bar of Monday's session — 00:00 UTC Tuesday
            "timestamp": prior_close + 3 * 3_600_000,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 1, "vwap": 100.2, "transactions": 1,
        },
        *_rth_minute_bars(picked, 5),
    ]
    assert datetime.fromtimestamp(bars[0]["timestamp"] / 1000, tz=UTC).date() == date.fromisoformat(picked)

    monkeypatch.setattr(dataset_router, "fetch_bars_chunked", lambda *a, **k: list(bars), raising=True)
    request = prepare_generation_request(
        _generation_request(picked, _utc_midnight_ms(picked), _utc_day_end_ms(picked))
    )

    df, _column_meta, _raw_count = dataset_router._fetch_and_process(request)

    assert int(df["timestamp"].min()) == int(bars[1]["timestamp"]), "prior-session post-market tail must go"


def test_a_gap_inside_the_trimmed_lead_in_does_not_fail_the_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing minute before the requested numeric start used to trip
    fail_on_gaps before the trim could drop the affected bars; gap
    validation is scoped to the output window (#2524 review)."""
    bars = _rth_minute_bars(_PICK, 30)
    open_ms = bars[0]["timestamp"] - 60_000
    del bars[5]  # a gap 25 minutes before the requested start
    start_ms_utc = open_ms + 20 * 60_000

    monkeypatch.setattr(dataset_router, "fetch_bars_chunked", lambda *a, **k: list(bars), raising=True)
    request = prepare_generation_request(
        _generation_request(_PICK, start_ms_utc, _utc_day_end_ms(_PICK))
    )
    request = request.model_copy(update={"fail_on_gaps": True})

    df, _column_meta, _raw_count = dataset_router._fetch_and_process(request)

    assert len(df) == 11  # open+20 … open+30, the gap long gone

@pytest.mark.asyncio
async def test_the_plan_receipt_excludes_a_zero_overlap_trailing_session(api: FastAPI) -> None:
    """An exclusive end exactly at a session's open leaves that session zero
    overlap: the receipt must not list or count it (#2524 review)."""
    pick_from, pick_to = "2026-06-12", "2026-06-16"
    end_at_tuesdays_open = session_open_ms_utc(date(2026, 6, 16))
    payload: dict[str, Any] = {
        "ticker": "SPY",
        "from_date": pick_from,
        "to_date": pick_to,
        "start_ms_utc": _utc_midnight_ms(pick_from),
        "end_ms_utc": end_at_tuesdays_open,
    }
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/api/dataset/plan", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["exchange_sessions"] == ["2026-06-12", "2026-06-15"]
    assert body["session_count"] == 2
