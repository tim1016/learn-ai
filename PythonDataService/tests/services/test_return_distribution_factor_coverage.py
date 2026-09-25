"""The return study never labels a window adjusted that its factor file does not cover (#2452).

End to end through the real capture pipeline: ``materialize_symbol_history``
and a chart capture drive the real ``ensure_data`` (Polygon and the LEAN
launcher mocked at the HTTP layer, the catalog replaced by the in-memory
``FakeCatalog``), and the study then reads what they wrote from the same
tmp lake. The symbol trades flat at 100 and splits 2:1 on 2024-06-03, so
on a split-and-dividend basis the split day's close-to-close return is
exactly 0% and an unadjusted read of it is exactly -50% (100 raw / two
shares == 50 raw) — an identity oracle, compared at ``atol=1e-12``
percentage points, ``rtol=0``.

Owner decision #2432: studies adjust their whole window with every
corporate action known today, and a window the adjustment data does not
cover is refused, never labelled adjusted.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.data_lake import run_materialization
from app.data_lake.factor_files import SessionRun, read_recorded_factor_file
from app.data_lake.path_policy import LeanFactorFilePath, resolve_lake_root
from app.data_lake.types import ArtifactIdentity
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.services import return_distribution_service as study
from tests._helpers.fake_lake_catalog import (
    FakeCatalog,
    install_fake_catalog,
    mock_launcher,
    point_lake_writer_at_tmp,
)

SYMBOL = "SYNTH"
SPLIT_DAY = date(2024, 6, 3)
PRE_SPLIT_PRICE = 100.0
POST_SPLIT_PRICE = 50.0

# The first capture: wide enough for the study's 14-day lead-in and its
# 30-session floor, and it spans the split.
WIDE_START = date(2024, 4, 15)
WIDE_END = date(2024, 7, 12)
# The study window, read with its lead-in entirely inside WIDE_*.
STUDY_FROM = date(2024, 5, 1)
STUDY_TO = WIDE_END


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def lake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: FakeCatalog) -> Path:
    """The raw lake root every capture writes and the study reads."""
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    return resolve_lake_root("raw")


def _day_bars(day: date, *, regular_session: bool = True) -> list[dict[str, object]]:
    """Three RTH minutes at one flat price: the open, the next minute, and
    the session's last minute (half-days included, via the calendar). A day
    without a regular session gets one pre-market minute instead: captured,
    with no regular-session close."""
    window = session_windows_ms_utc(day, day)[0]
    price = PRE_SPLIT_PRICE if day < SPLIT_DAY else POST_SPLIT_PRICE
    if regular_session:
        starts: tuple[int, ...] = (window.open_ms_utc, window.open_ms_utc + 60_000, window.close_ms_utc - 60_000)
    else:
        starts = (window.open_ms_utc - 90 * 60_000,)
    return [
        {"v": 100, "vw": price, "o": price, "c": price, "h": price, "l": price, "t": t, "n": 1}
        for t in starts
    ]


def _minute_aggs(days_without_regular_session: frozenset[date]):
    def _respond(request: httpx.Request) -> httpx.Response:
        match = re.search(r"/range/1/minute/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", request.url.path)
        assert match is not None, request.url
        start, end = (date.fromisoformat(g) for g in match.groups())
        results = [
            bar
            for window in session_windows_ms_utc(start, end)
            for bar in _day_bars(
                window.session_date, regular_session=window.session_date not in days_without_regular_session
            )
        ]
        return httpx.Response(200, json={"ticker": SYMBOL, "status": "OK", "results": results})

    return _respond


def _mock_provider(
    *,
    dividends: tuple[dict[str, object], ...] = (),
    days_without_regular_session: frozenset[date] = frozenset(),
) -> None:
    mock_launcher()
    respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/{SYMBOL}/range/1/minute/.*").mock(
        side_effect=_minute_aggs(days_without_regular_session)
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {
                        "ticker": SYMBOL,
                        "execution_date": SPLIT_DAY.isoformat(),
                        "split_from": 1,
                        "split_to": 2,
                    }
                ],
            },
        )
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": list(dividends)})
    )
    respx.get(re.compile(rf"https://api\.polygon\.io/v3/reference/tickers/{SYMBOL}/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )


def _utc_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


async def _study(from_date: date = STUDY_FROM, to_date: date = STUDY_TO) -> study.StudyOutcome:
    return await study.compute_return_distribution(
        symbol=SYMBOL,
        from_ms_utc=_utc_ms(from_date),
        to_ms_utc=_utc_ms(to_date),
        bin_width_pct=0.5,
        span_pct=5.0,
    )


def _split_day_close_to_close_pct(outcome: study.StudyOutcome) -> float | None:
    split_open_ms = session_windows_ms_utc(SPLIT_DAY, SPLIT_DAY)[0].open_ms_utc
    return next(d.close_to_close_pct for d in outcome.result.days if d.session_open_ms_utc == split_open_ms)


def _factor_rows(lake_root: Path) -> list[str]:
    path = lake_root.joinpath(*LeanFactorFilePath(market="usa", symbol=SYMBOL).relative_path().parts)
    return path.read_text(encoding="ascii").splitlines()


@respx.mock
async def test_a_narrower_capture_never_drops_an_older_split_the_study_spans(lake_root: Path) -> None:
    """The issue's case: a wide capture, then a narrower one that rebuilds
    the factor file; a study spanning the older split must still be adjusted
    for it — and a narrow request must never shrink the file."""
    _mock_provider()

    wide = await run_materialization.materialize_symbol_history(symbol=SYMBOL, start=WIDE_START, end=WIDE_END)
    assert wide.status == "complete", wide.detail
    # Later, narrower, after the split — and one week past the first
    # capture, so its source set differs and the factor file is rebuilt.
    narrow = await run_materialization.materialize_symbol_history(
        symbol=SYMBOL, start=date(2024, 6, 24), end=date(2024, 7, 19)
    )
    assert narrow.status == "complete", narrow.detail

    outcome = await _study()

    assert outcome.capture.status == "not_attempted"
    assert outcome.result.adjustment == "split_and_dividend"
    assert _split_day_close_to_close_pct(outcome) == pytest.approx(0.0, abs=1e-12, rel=0)
    # The split row (dated the session before the ex-date) survived the rebuild.
    assert any(row.startswith("20240531,") for row in _factor_rows(lake_root))


@respx.mock
async def test_a_chart_widened_window_is_not_studied_on_a_narrower_factor_file(lake_root: Path) -> None:
    """Codex L1: a study capture ending before the split, then a chart
    capture that widens the raw bars past it without factor files. The
    study's bars are all present, so the old probe never captured and
    labelled the uncovered split day adjusted at -50%."""
    _mock_provider()

    first = await run_materialization.materialize_symbol_history(
        symbol=SYMBOL, start=WIDE_START, end=date(2024, 5, 31)
    )
    assert first.status == "complete", first.detail
    chart = await asyncio.to_thread(
        run_materialization.materialize_chart_range,
        symbol=SYMBOL,
        start=WIDE_START,
        end=WIDE_END,
        price_adjustment_mode="raw",
        requester="test-chart",
    )
    assert chart.status == "complete", chart.detail

    outcome = await _study()

    # Every bar was present, so only the uncovered factor file asked for the
    # capture — and the capture's rebuild is what covers the window now.
    assert outcome.capture.status == "complete", outcome.capture.detail
    assert outcome.result.adjustment == "split_and_dividend"
    assert _split_day_close_to_close_pct(outcome) == pytest.approx(0.0, abs=1e-12, rel=0)


@respx.mock
async def test_a_window_the_rebuild_cannot_cover_is_refused_with_the_reason(lake_root: Path) -> None:
    """The same chart-widened lake, but the corporate-action provider fails
    on the rebuild: the study refuses — naming the uncovered sessions and
    what the capture did — instead of labelling the -50% day adjusted."""
    _mock_provider()
    first = await run_materialization.materialize_symbol_history(
        symbol=SYMBOL, start=WIDE_START, end=date(2024, 5, 31)
    )
    assert first.status == "complete", first.detail
    chart = await asyncio.to_thread(
        run_materialization.materialize_chart_range,
        symbol=SYMBOL,
        start=WIDE_START,
        end=WIDE_END,
        price_adjustment_mode="raw",
        requester="test-chart",
    )
    assert chart.status == "complete", chart.detail
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(503, json={"status": "ERROR"})
    )

    with pytest.raises(study.AdjustmentNotCoveredError) as refused:
        await _study()

    message = str(refused.value)
    assert "2024-04-15..2024-05-31" in message, message
    assert "factor_file/provider_api_error" in (refused.value.capture_note or "")


@respx.mock
async def test_an_unpriced_session_in_the_lead_in_the_returns_never_read_does_not_refuse_the_study(
    lake_root: Path,
) -> None:
    """CodeRabbit on #2481: a dividend goes ex on 04-23, and its reference
    session 04-22 is captured with no regular-session close, so the builder
    ends a covered span there. The study from 05-01 reads its lead-in
    (04-17..04-30) only for the close of 04-30, which the first returned
    day returns against; that close and every returned day lie in the later
    span. The study is adjusted. It must not be refused (409) for lead-in
    sessions it never compares, a refusal no rebuild could clear."""
    unpriced = date(2024, 4, 22)
    _mock_provider(
        dividends=({"ticker": SYMBOL, "ex_dividend_date": "2024-04-23", "cash_amount": 1.0},),
        days_without_regular_session=frozenset({unpriced}),
    )
    wide = await run_materialization.materialize_symbol_history(symbol=SYMBOL, start=WIDE_START, end=WIDE_END)
    assert wide.status == "complete", wide.detail
    assert read_recorded_factor_file(lake_root, market="usa", symbol=SYMBOL).spans == (
        SessionRun(WIDE_START, unpriced),
        SessionRun(date(2024, 4, 23), WIDE_END),
    )

    outcome = await _study()

    assert outcome.capture.status == "not_attempted"
    assert outcome.result.adjustment == "split_and_dividend"
    assert _split_day_close_to_close_pct(outcome) == pytest.approx(0.0, abs=1e-12, rel=0)


@respx.mock
async def test_a_first_day_returning_across_a_coverage_break_is_refused(lake_root: Path) -> None:
    """The other side of the lead-in rule: the close of the lead-in's last
    session is read. The factor file is built while 05-31 (the 06-03
    split's reference session) is not captured, so its spans break there
    and the split is in neither span. A raw chart capture then adds 05-31.
    A study from 06-03 returns 06-03 against 05-31's close across the
    split. That pair is uncovered, and with the rebuild failing the study
    is refused rather than labelling the -50% day adjusted."""
    _mock_provider()
    late_end = date(2024, 7, 19)
    for start, end in ((WIDE_START, date(2024, 5, 30)), (SPLIT_DAY, late_end)):
        capture = await run_materialization.materialize_symbol_history(symbol=SYMBOL, start=start, end=end)
        assert capture.status == "complete", capture.detail
    chart = await asyncio.to_thread(
        run_materialization.materialize_chart_range,
        symbol=SYMBOL,
        start=date(2024, 5, 31),
        end=date(2024, 5, 31),
        price_adjustment_mode="raw",
        requester="test-chart",
    )
    assert chart.status == "complete", chart.detail
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(503, json={"status": "ERROR"})
    )

    with pytest.raises(study.AdjustmentNotCoveredError) as refused:
        await _study(SPLIT_DAY, late_end)

    assert "the sessions 2024-05-31..2024-07-19" in str(refused.value), refused.value


@respx.mock
async def test_a_factor_file_without_a_coverage_record_is_rebuilt_not_trusted(lake_root: Path) -> None:
    """A factor file written before coverage was recorded covers nothing, even
    when its catalog row is current: the study's capture rebuilds it."""
    _mock_provider()
    wide = await run_materialization.materialize_symbol_history(symbol=SYMBOL, start=WIDE_START, end=WIDE_END)
    assert wide.status == "complete", wide.detail
    record = lake_root.joinpath(*LeanFactorFilePath(market="usa", symbol=SYMBOL).coverage_record_path().parts)
    record.unlink()

    outcome = await _study()

    assert outcome.capture.status == "complete", outcome.capture.detail
    assert record.is_file()
    assert _split_day_close_to_close_pct(outcome) == pytest.approx(0.0, abs=1e-12, rel=0)


@respx.mock
async def test_a_failed_factor_file_row_is_reclaimed_rather_than_waited_on(
    lake_root: Path, fake_catalog: FakeCatalog
) -> None:
    """A factor-file row a failed build left ``'failed'`` used to read as
    "in-flight elsewhere" forever; with the study refusing an uncovered
    window, that would have made the symbol unstudiable. It is reclaimed."""
    _mock_provider()
    # A pre-#2452 writer claimed before building, so a provider failure
    # stranded the row 'failed'; seed exactly that row.
    identity = ArtifactIdentity(
        artifact_kind="factor_file",
        market="usa",
        symbol=SYMBOL,
        provider="polygon",
        price_adjustment_mode="raw",
    )
    stranded = await fake_catalog.claim_corp_action_artifact(
        identity, "old-writer", 300_000, "legacy-window-hash", "equity/usa/factor_files/synth.csv"
    )
    assert stranded is not None
    await fake_catalog.fail_artifact(
        stranded, "provider_api_error", "503", worker_id="old-writer", lease_generation=1
    )

    outcome = await _study()

    assert outcome.capture.status == "complete", outcome.capture.detail
    assert fake_catalog.rows[stranded]["status"] == "complete"
    assert _split_day_close_to_close_pct(outcome) == pytest.approx(0.0, abs=1e-12, rel=0)
