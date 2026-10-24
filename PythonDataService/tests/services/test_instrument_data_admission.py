"""What the Data tab reports, through the real admission seam (#3503, T05, T06).

A Fill is the established backfill job over the window the report names, and
that job is ``run_backfill`` over the real ``ensure_data`` (Polygon and the LEAN
launcher mocked at the HTTP layer, the catalog the in-memory ``FakeCatalog``,
the lake a tmp tree). The report is read through the real route, so the counts
and reasons asserted here are the ones the tab shows:

* held: every session the lake holds reads ready, and nothing is offered;
* one gap: the report names that one session, and a Fill closes exactly it;
* fully cold: every scheduled session is a gap, the window is the whole span;
* a provider that fails or has no bars never turns a gap into a complete result;
* corrupt adjustment: a complete row a run refuses is a rebuild with its reason;
* the derived daily file is its own fact and never makes a minute session unready;
* the provider's window advancing past retained history leaves it readable.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.data_lake import path_policy, run_materialization
from app.data_lake.adjustment_versions import companion_path
from app.data_lake.backfill import BackfillResult, run_backfill
from app.data_lake.types import DataRunSpec, trading_date_to_calendar_anchor_ms
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.trading_calendar import session_open_ms_utc, session_windows_ms_utc
from app.routers import data_lake as data_lake_router
from app.routers import instrument_data as instrument_data_router
from app.services import instrument_data_service
from tests._helpers.fake_lake_catalog import (
    FakeCatalog,
    install_fake_catalog,
    mock_launcher,
    point_lake_writer_at_tmp,
)

SYMBOL = "SYNTH"
# Monday to Friday, all long past and well inside the provider's window today.
WEEK_START, WEEK_END = date(2024, 5, 20), date(2024, 5, 24)
WEDNESDAY, THURSDAY = date(2024, 5, 22), date(2024, 5, 23)

Provider = Callable[[date], list[dict[str, object]] | httpx.Response]


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: FakeCatalog) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def _bars(day: date) -> list[dict[str, object]]:
    """Three flat minutes of the session: its open, the next minute, its last minute."""
    window = session_windows_ms_utc(day, day)[0]
    starts = (window.open_ms_utc, window.open_ms_utc + 60_000, window.close_ms_utc - 60_000)
    return [{"v": 100, "vw": 100.0, "o": 100.0, "c": 100.0, "h": 100.0, "l": 100.0, "t": t, "n": 1} for t in starts]


def _serve(provider: Provider = _bars) -> None:
    """The provider and the launcher, answering what ``provider`` says per requested day."""
    mock_launcher()

    def _minute_aggs(request: httpx.Request) -> httpx.Response:
        match = re.search(r"/range/1/minute/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", request.url.path)
        assert match is not None, request.url
        start, end = (date.fromisoformat(g) for g in match.groups())
        results: list[dict[str, object]] = []
        for window in session_windows_ms_utc(start, end):
            answer = provider(window.session_date)
            if isinstance(answer, httpx.Response):
                return answer
            results.extend(answer)
        return httpx.Response(200, json={"ticker": SYMBOL, "status": "OK", "results": results})

    respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/{SYMBOL}/range/1/minute/.*").mock(
        side_effect=_minute_aggs
    )
    for kind in ("splits", "dividends"):
        respx.get(re.compile(rf"https://api\.polygon\.io/v3/reference/{kind}.*")).mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": []})
        )
    respx.get(re.compile(rf"https://api\.polygon\.io/v3/reference/tickers/{SYMBOL}/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )


def _server_error(_: date) -> httpx.Response:
    return httpx.Response(500, json={"status": "ERROR"})


def _fill_spec(start: date, end: date, mode: str) -> DataRunSpec:
    return DataRunSpec(
        request_id="12345678-1234-5678-1234-567812345678",
        run_type="python_lab",
        symbols=[SYMBOL],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(start),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(end),
        data_types=["trade"],
        lean_image_digest=PINNED_LEAN_IMAGE_DIGEST,
        price_adjustment_mode=mode,
    )


async def _fill(start: date, end: date, mode: str = "raw") -> BackfillResult:
    """The Data tab's Fill: the backfill job's own worker over the real ``ensure_data``."""
    return await run_backfill(_fill_spec(start, end, mode))


def _fill_window_dates(report: dict) -> tuple[date, date] | None:
    window = report["fill_window"]
    if window is None:
        return None
    return tuple(  # type: ignore[return-value]
        datetime.fromtimestamp(window[key] / 1000, tz=UTC).date()
        for key in ("start_trading_date_ms", "end_trading_date_ms")
    )


async def _inspect(start: date = WEEK_START, end: date = WEEK_END, mode: str = "raw") -> dict:
    """The route the Data tab reads, over the real catalog fake and the tmp lake."""
    app = FastAPI()
    app.include_router(instrument_data_router.router)
    window = [
        int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000) for day in (start, end)
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/data-lake/instrument-data",
            params={
                "symbol": SYMBOL,
                "start_trading_date_ms": window[0],
                "end_trading_date_ms": window[1],
                "price_adjustment_mode": mode,
            },
        )
    assert response.status_code == 200, response.text
    return response.json()


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)


async def _refusal(path: str, params: dict[str, object]) -> tuple[int, object]:
    """What a route answers for a request, over the bare app: its status and its ``detail``."""
    app = FastAPI()
    app.include_router(instrument_data_router.router)
    app.include_router(data_lake_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path, params=params)
    return response.status_code, response.json().get("detail")


def _states(report: dict) -> list[str]:
    return [session["state"] for session in report["sessions"]]


def _delete_session(catalog: FakeCatalog, lake_root: Path, day: date) -> None:
    """The session is gone from the catalog and from disk, as after a pruned volume."""
    (artifact_id,) = [
        key
        for key, row in catalog.rows.items()
        if row["artifact_kind"] == "time_series_bars" and row["resolution"] == "minute" and row["trading_date"] == day
    ]
    row = catalog.rows.pop(artifact_id)
    catalog.keys = {key: value for key, value in catalog.keys.items() if value != artifact_id}
    (lake_root / row["file_path"]).unlink()


# ── held ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_held_window_reads_ready_and_offers_nothing_to_fill(lake: Path) -> None:
    _serve()
    result = await _fill(WEEK_START, WEEK_END)
    assert result.overall_status == "complete", result.failures

    report = await _inspect()

    assert _states(report) == ["ready"] * 5
    assert report["summary"]["session_count"] == 5
    assert report["summary"]["calendar_day_count"] == 5
    assert report["summary"]["run_readable"] is True
    assert report["summary"]["fetchable"] == 0
    assert report["fill_window"] is None
    assert report["daily_rollup"] == {"state": "current", "reason": None}


def _is_polygon(host: str) -> bool:
    return host == "polygon.io" or host.endswith(".polygon.io")


@respx.mock
async def test_opening_the_report_fetches_nothing(lake: Path, fake_catalog: FakeCatalog) -> None:
    _serve()

    report = await _inspect()

    assert _states(report) == ["missing"] * 5
    assert fake_catalog.rows == {}, "reading a cold window leaves the catalog as it found it"
    assert not any(_is_polygon(request.request.url.host) for request in respx.calls)
    assert not list((lake / "lake").rglob("*.zip"))


# ── one gap ─────────────────────────────────────────────────────────────────


@respx.mock
async def test_one_gap_is_named_and_a_fill_closes_exactly_it(lake: Path, fake_catalog: FakeCatalog) -> None:
    _serve()
    await _fill(WEEK_START, WEEK_END)
    _delete_session(fake_catalog, path_policy.resolve_lake_root("raw"), WEDNESDAY)

    before = await _inspect()

    assert _states(before) == ["ready", "ready", "missing", "ready", "ready"]
    assert before["summary"]["ready"] == 4
    assert before["summary"]["missing"] == 1
    assert before["summary"]["fetchable"] == 1
    assert before["summary"]["run_readable"] is False
    assert _fill_window_dates(before) == (WEDNESDAY, WEDNESDAY)
    # Its removal changed the set the derived daily file was built from.
    assert before["daily_rollup"]["state"] == "stale"
    assert before["daily_rollup"]["reason"] == "sources_changed"

    asked = len(respx.calls)
    result = await _fill(*_fill_window_dates(before))
    after = await _inspect()

    assert result.total_sessions == 1
    assert _states(after) == ["ready"] * 5
    assert after["summary"]["ready"] - before["summary"]["ready"] == 1, "added: the one session"
    assert before["summary"]["ready"] == 4, "reused: the four it already held"
    assert any("range/1/minute/2024-05-22" in str(call.request.url) for call in list(respx.calls)[asked:])
    assert not any(
        re.search(r"range/1/minute/2024-05-2[0134]", str(call.request.url)) for call in list(respx.calls)[asked:]
    ), "the sessions it held were not asked for again"
    assert after["fill_window"] is None
    assert after["daily_rollup"]["state"] == "current"


# ── fully cold ──────────────────────────────────────────────────────────────


@respx.mock
async def test_a_cold_window_is_every_scheduled_session_and_a_fill_takes_the_whole_span(lake: Path) -> None:
    _serve()
    # A window with a weekend and the Memorial Day holiday (2024-05-27) inside it.
    start, end = date(2024, 5, 24), date(2024, 5, 29)

    cold = await _inspect(start, end)

    assert cold["summary"]["calendar_day_count"] == 6
    assert cold["summary"]["session_count"] == 3, "the Friday, then Tuesday and Wednesday"
    assert _states(cold) == ["missing"] * 3
    assert cold["daily_rollup"]["state"] == "not_built"
    assert _fill_window_dates(cold) == (date(2024, 5, 24), date(2024, 5, 29))

    result = await _fill(*_fill_window_dates(cold))
    warm = await _inspect(start, end)

    assert result.overall_status == "complete", result.failures
    assert _states(warm) == ["ready"] * 3
    assert [session["trading_date_ms"] for session in warm["sessions"]] == [
        session_open_ms_utc(date(2024, 5, 24)),
        session_open_ms_utc(date(2024, 5, 28)),
        session_open_ms_utc(date(2024, 5, 29)),
    ]


@respx.mock
async def test_a_fill_the_provider_fails_never_reads_as_a_complete_result(lake: Path) -> None:
    _serve(lambda day: _server_error(day) if day == THURSDAY else _bars(day))

    result = await _fill(WEEK_START, WEEK_END)
    report = await _inspect()

    assert result.overall_status == "partial"
    assert report["summary"]["run_readable"] is False
    assert _states(report) == ["ready", "ready", "ready", "failed", "ready"]
    failed = report["sessions"][3]
    assert failed["reason"] == "provider_api_error"
    assert failed["fetchable"] is True, "an outage is worth asking again"
    assert _fill_window_dates(report) == (THURSDAY, THURSDAY)


@respx.mock
async def test_a_session_the_provider_has_no_bars_for_is_named_and_not_offered_again(lake: Path) -> None:
    _serve(lambda day: [] if day == WEEK_START else _bars(day))

    await _fill(WEEK_START, WEEK_END)
    report = await _inspect()

    assert _states(report) == ["failed", "ready", "ready", "ready", "ready"]
    no_bars = report["sessions"][0]
    assert (no_bars["reason"], no_bars["fetchable"], no_bars["blocked_by"]) == ("provider_no_data", False, "provider_no_data")
    assert report["summary"]["provider_no_data"] == 1
    assert report["summary"]["run_readable"] is False
    assert report["fill_window"] is None


# ── a window the coverage read refuses ──────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "start", "end", "reason"),
    [
        ("not a symbol", _ms(WEEK_START), _ms(WEEK_END), "invalid_symbol"),
        (SYMBOL, _ms(WEEK_END), _ms(WEEK_START), "invalid_range"),
        (SYMBOL, _ms(date(2015, 1, 5)), _ms(date(2026, 1, 5)), "range_too_large"),
        (SYMBOL, 2**62, 2**62 + 1, "invalid_trading_date_ms"),
    ],
    ids=["symbol", "reversed", "too wide", "not milliseconds"],
)
async def test_a_window_the_coverage_read_refuses_is_refused_here_with_the_same_typed_reason(
    lake: Path, symbol: str, start: int, end: int, reason: str
) -> None:
    """Both routes read the coverage through one function, so neither can refuse a window the other accepts."""
    params = {"symbol": symbol, "start_trading_date_ms": start, "end_trading_date_ms": end, "price_adjustment_mode": "raw"}

    coverage = await _refusal("/api/data-lake/coverage", {**params, "market": "usa", "data_type": "trade"})
    inspected = await _refusal("/api/data-lake/instrument-data", params)

    assert coverage[0] == inspected[0] == 422
    assert coverage[1] == inspected[1]
    assert isinstance(inspected[1], dict)
    assert inspected[1]["reason"] == reason


# ── corrupt adjustment ──────────────────────────────────────────────────────


@respx.mock
async def test_a_complete_adjusted_row_a_run_refuses_is_a_rebuild_a_fill_repairs(
    lake: Path, fake_catalog: FakeCatalog
) -> None:
    mode = "polygon_split_adjusted"
    _serve()
    result = await _fill(WEEK_START, WEEK_END, mode)
    assert result.overall_status == "complete", result.failures
    root = path_policy.resolve_lake_root(mode)
    paths = {
        row["trading_date"]: row["file_path"]
        for row in fake_catalog.rows.values()
        if row["artifact_kind"] == "time_series_bars" and row["resolution"] == "minute" and row["price_adjustment_mode"] == mode
    }
    assert len(paths) == 5
    # One session loses its adjustment record, another's no longer matches its price file.
    (root / companion_path(PurePosixPath(paths[date(2024, 5, 21)]))).unlink()
    (root / companion_path(PurePosixPath(paths[THURSDAY]))).write_text('{"schema_version": 1}')

    before = await _inspect(mode=mode)

    assert _states(before) == ["ready", "needs_rebuild", "ready", "needs_rebuild", "ready"]
    assert [(s["reason"], s["fetchable"]) for s in before["sessions"] if s["state"] == "needs_rebuild"] == [
        ("adjustment_record_missing", True),
        ("adjustment_record_rejected", True),
    ]
    assert before["summary"]["needs_rebuild"] == 2
    assert before["summary"]["run_readable"] is False
    assert _fill_window_dates(before) == (date(2024, 5, 21), THURSDAY)

    await _fill(*_fill_window_dates(before), mode)
    after = await _inspect(mode=mode)

    assert _states(after) == ["ready"] * 5
    assert after["summary"]["run_readable"] is True


# ── the derived daily file ──────────────────────────────────────────────────


@respx.mock
async def test_a_stale_daily_file_never_makes_a_minute_session_unready(lake: Path) -> None:
    _serve()
    await _fill(date(2024, 5, 21), WEEK_END)
    assert (await _inspect(date(2024, 5, 21), WEEK_END))["daily_rollup"]["state"] == "current"

    # A chart capture widens the minute history and, as the chart does, builds no daily file.
    chart = await _chart_capture(date(2024, 5, 20), date(2024, 5, 20))
    assert chart.status == "complete", chart.detail
    report = await _inspect()

    assert _states(report) == ["ready"] * 5
    assert report["summary"]["run_readable"] is True, "a minute run never opens the daily file"
    assert report["daily_rollup"] == {"state": "stale", "reason": "sources_changed"}

    # A fill over a window with nothing to fetch still brings the daily file up to the sessions held.
    await _fill(WEEK_START, WEEK_END)
    assert (await _inspect())["daily_rollup"] == {"state": "current", "reason": None}


@respx.mock
async def test_an_adjusted_daily_file_whose_adjustment_record_is_gone_is_stale_for_that_evidence(
    lake: Path, fake_catalog: FakeCatalog
) -> None:
    mode = "polygon_split_adjusted"
    _serve()
    result = await _fill(WEEK_START, WEEK_END, mode)
    assert result.overall_status == "complete", result.failures
    assert (await _inspect(mode=mode))["daily_rollup"] == {"state": "current", "reason": None}

    (daily_path,) = [
        row["file_path"]
        for row in fake_catalog.rows.values()
        if row["artifact_kind"] == "time_series_bars" and row["resolution"] == "daily" and row["price_adjustment_mode"] == mode
    ]
    (path_policy.resolve_lake_root(mode) / companion_path(PurePosixPath(daily_path))).unlink()
    report = await _inspect(mode=mode)

    assert report["daily_rollup"] == {"state": "stale", "reason": "adjustment_evidence_invalid"}
    assert report["summary"]["run_readable"] is True, "a minute run never opens the daily file"


@respx.mock
async def test_a_lake_only_ever_charted_has_no_daily_file_and_every_minute_session_ready(lake: Path) -> None:
    _serve()
    chart = await _chart_capture(WEEK_START, WEEK_END)
    assert chart.status == "complete", chart.detail

    report = await _inspect()

    assert _states(report) == ["ready"] * 5
    assert report["daily_rollup"]["state"] == "not_built"
    assert report["summary"]["run_readable"] is True


async def _chart_capture(start: date, end: date):
    import asyncio

    return await asyncio.to_thread(
        run_materialization.materialize_chart_range,
        symbol=SYMBOL,
        start=start,
        end=end,
        price_adjustment_mode="raw",
        requester="test-chart",
    )


# ── the provider's window ───────────────────────────────────────────────────


@respx.mock
async def test_history_the_provider_no_longer_serves_stays_readable_and_a_missing_old_day_refuses(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve()
    await _fill(WEEK_START, WEEK_END)
    # A window a day wider on each side than what is held.
    start, end = date(2024, 5, 17), date(2024, 5, 28)

    def _clock(year: int, month: int, day: int) -> None:
        now_ms = int(datetime(year, month, day, 15, tzinfo=UTC).timestamp() * 1000)
        monkeypatch.setattr(instrument_data_service, "now_ms_utc", lambda: now_ms)

    _clock(2026, 6, 1)  # the provider still serves back past 2024-05
    serving = await _inspect(start, end)
    assert _states(serving) == ["missing", "ready", "ready", "ready", "ready", "ready", "missing"]
    assert serving["summary"]["fetchable"] == 2
    assert _fill_window_dates(serving) == (date(2024, 5, 17), date(2024, 5, 28)), "the span is never cut short"

    _clock(2030, 1, 15)  # its window has advanced past everything held
    aged = await _inspect(start, end)

    assert _states(aged) == ["missing", "ready", "ready", "ready", "ready", "ready", "missing"]
    refused = [session for session in aged["sessions"] if session["state"] == "missing"]
    assert [(s["fetchable"], s["blocked_by"]) for s in refused] == [(False, "before_provider_history")] * 2
    assert aged["summary"]["ready"] == 5, "retained history still opens"
    assert aged["summary"]["before_provider_history"] == 2
    assert aged["fill_window"] is None, "an old day the lake lacks is not offered for fetching"
    assert aged["provider_history_start_ms"] > session_open_ms_utc(date(2024, 5, 28))
