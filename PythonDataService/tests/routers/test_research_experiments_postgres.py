"""The experiment tables against the real ``"ResearchExperiments"`` / ``"SignalExperiments"`` / ``"Tickers"`` tables.

A signal report is served by its experiment's id, and both tables are two stores of the Research History.

These run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. Every
test seeds tickers of its own (a symbol made from the test's tag); removing them removes their experiments with them,
as the tables' foreign keys cascade.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.data_lake import catalog_client
from app.main import app
from app.research.history.registry import default_history_sources
from app.routers.research_history import get_history_sources
from tests._helpers.stored_signal_report import a_busy_signal_report, served, stored_text
from tests.research.conftest import second_conn as second_conn
from tests.research.conftest import unique as unique

BASE = "/api/research"
# Written with a microsecond part, so a row that rounded rather than floored its millisecond would show.
OLDEST = datetime(2026, 1, 2, 3, 4, 5, 678_912, tzinfo=UTC)
MIDDLE = datetime(2026, 1, 3, 3, 4, 5, 999_999, tzinfo=UTC)
NEWEST = datetime(2026, 1, 4, 3, 4, 5, 1_000, tzinfo=UTC)
OLDEST_MS, MIDDLE_MS, NEWEST_MS = 1_767_323_045_678, 1_767_409_445_999, 1_767_495_845_001


@pytest.fixture
def db(second_conn: asyncpg.Connection) -> asyncpg.Connection:
    """A plain session on the attested database (the research suites' fixture skips the test without one)."""
    return second_conn


@dataclass(frozen=True)
class Tickers:
    symbol: str
    id: int
    other_symbol: str
    other_id: int


@pytest.fixture
async def tickers(db: asyncpg.Connection, unique: str) -> AsyncIterator[Tickers]:
    symbol, other_symbol = f"T{unique.upper()}", f"U{unique.upper()}"
    ids = [
        await db.fetchval(
            """INSERT INTO public."Tickers" ("Symbol", "Name", "Market", "Active", "CreatedAt")
               VALUES ($1, $1, 'stocks', true, now()) RETURNING "Id" """,
            sym,
        )
        for sym in (symbol, other_symbol)
    ]
    yield Tickers(symbol, ids[0], other_symbol, ids[1])
    await db.execute('DELETE FROM public."Tickers" WHERE "Symbol" = ANY($1::text[])', [symbol, other_symbol])


@pytest.fixture
async def client(db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """The app, talking to the attested database."""
    monkeypatch.setattr(settings, "POSTGRES_URL", os.environ["POSTGRES_URL"])
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        await catalog_client.close_pool()


async def _research_row(
    db: asyncpg.Connection, ticker_id: int, feature: str, created_at: datetime, **columns: Any
) -> int:
    row = {
        "StartDate": "2025-01-02",
        "EndDate": "2025-06-30",
        "BarsUsed": 48_750,
        "MeanIC": "0.12345678",
        "ICTStat": "2.50000000",
        "ICPValue": "0.01200000",
        "AdfPValue": "0.00100000",
        "KpssPValue": "0.40000000",
        "IsStationary": True,
        "PassedValidation": False,
        "MonotonicityRatio": "0.80000000",
        "IsMonotonic": True,
        **columns,
    }
    return await db.fetchval(
        """INSERT INTO public."ResearchExperiments"
               ("TickerId", "FeatureName", "StartDate", "EndDate", "BarsUsed", "MeanIC", "ICTStat", "ICPValue", "AdfPValue",
                "KpssPValue", "IsStationary", "PassedValidation", "MonotonicityRatio", "IsMonotonic", "JsonReport", "CreatedAt")
           VALUES ($1, $2, $3, $4, $5, $6::numeric, $7::numeric, $8::numeric, $9::numeric, $10::numeric, $11, $12, $13::numeric, $14, '{}', $15)
           RETURNING "Id" """,
        ticker_id,
        feature,
        row["StartDate"],
        row["EndDate"],
        row["BarsUsed"],
        row["MeanIC"],
        row["ICTStat"],
        row["ICPValue"],
        row["AdfPValue"],
        row["KpssPValue"],
        row["IsStationary"],
        row["PassedValidation"],
        row["MonotonicityRatio"],
        row["IsMonotonic"],
        created_at,
    )


async def _signal_row(
    db: asyncpg.Connection, ticker_id: int, feature: str, created_at: datetime, json_report: str = "{}", **columns: Any
) -> int:
    row = {
        "OverallGrade": "B",
        "StatusLabel": "Research",
        "OverallPassed": True,
        "MeanOosSharpe": "0.65000000",
        **columns,
    }
    return await db.fetchval(
        """INSERT INTO public."SignalExperiments"
               ("TickerId", "FeatureName", "StartDate", "EndDate", "BarsUsed", "OverallGrade", "StatusLabel", "OverallPassed",
                "MeanOosSharpe", "BestThreshold", "BestCostBps", "FlipSign", "RegimeGateEnabled", "JsonReport", "CreatedAt")
           VALUES ($1, $2, '2025-01-02', '2025-06-30', 48750, $3, $4, $5, $6::numeric, 1.0, 2.5, false, true, $7, $8)
           RETURNING "Id" """,
        ticker_id,
        feature,
        row["OverallGrade"],
        row["StatusLabel"],
        row["OverallPassed"],
        row["MeanOosSharpe"],
        json_report,
        created_at,
    )


async def test_a_stored_report_is_served_whole_by_the_experiments_id(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers
) -> None:
    report = a_busy_signal_report()
    experiment_id = await _signal_row(db, tickers.id, "momentum_5m", NEWEST, json_report=stored_text(report))

    response = await client.get(f"{BASE}/signal-experiments/{experiment_id}/report")

    assert response.status_code == 200, response.text
    assert response.json() == served(report)


async def test_a_report_that_cannot_be_read_is_a_500_that_says_so_and_the_other_reports_still_read(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers
) -> None:
    broken = await _signal_row(db, tickers.id, "momentum_5m", OLDEST, json_report='{"bars_used": "many"')
    wrong_type = await _signal_row(db, tickers.id, "momentum_5m", MIDDLE, json_report='{"bars_used": "many"}')
    fine = await _signal_row(db, tickers.id, "momentum_5m", NEWEST, json_report='{"success": true, "ticker": "X"}')

    broken_response = await client.get(f"{BASE}/signal-experiments/{broken}/report")
    wrong_type_response = await client.get(f"{BASE}/signal-experiments/{wrong_type}/report")
    fine_response = await client.get(f"{BASE}/signal-experiments/{fine}/report")

    assert broken_response.status_code == wrong_type_response.status_code == 500
    assert (
        broken_response.json()["detail"]["code"]
        == wrong_type_response.json()["detail"]["code"]
        == "SIGNAL_REPORT_UNREADABLE"
    )
    assert f"signal experiment {broken}" in broken_response.json()["detail"]["message"]
    assert "bars_used" in wrong_type_response.json()["detail"]["message"]
    assert (fine_response.status_code, fine_response.json()["ticker"]) == (200, "X")


async def test_a_report_no_row_has_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{BASE}/signal-experiments/2147483647/report")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SIGNAL_EXPERIMENT_NOT_FOUND"


# ── The Research History over these two tables ─────────────────────

HISTORY = "/api/research/history"
JAN_2_2025_ET, JUN_30_2025_ET = 1_735_794_000_000, 1_751_256_000_000


@pytest.fixture
def history_over_no_files(tmp_path: Path) -> Iterator[None]:
    """The real stores, with the file stores pointed at an artifacts root that holds nothing."""
    app.dependency_overrides[get_history_sources] = lambda: default_history_sources(tmp_path)
    yield
    app.dependency_overrides.pop(get_history_sources, None)


async def _history(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get(HISTORY, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_both_tables_page_as_one_history_with_ties_inside_a_millisecond_broken_by_id(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers, history_over_no_files: None
) -> None:
    same = datetime(2026, 2, 3, 4, 5, 6, 700_000, tzinfo=UTC)
    # Two instants inside one millisecond. The store orders them by their microseconds (the later first); the shared
    # order compares milliseconds, so the lower id must come first or a merge of pages would repeat or skip a row.
    early, late = datetime(2026, 2, 3, 4, 5, 7, 123_100, tzinfo=UTC), datetime(2026, 2, 3, 4, 5, 7, 123_900, tzinfo=UTC)
    research = [await _research_row(db, tickers.id, f"f{i}", when) for i, when in enumerate([same, same, early, late])]
    signal = [await _signal_row(db, tickers.id, f"s{i}", when) for i, when in enumerate([same, early, late])]
    await _research_row(db, tickers.other_id, "other", same)

    expected = [
        ("feature-experiment", research[2]),
        ("feature-experiment", research[3]),
        ("signal-report", signal[1]),
        ("signal-report", signal[2]),
        ("feature-experiment", research[0]),
        ("feature-experiment", research[1]),
        ("signal-report", signal[0]),
    ]
    seen: list[tuple[str, int]] = []
    for page in (1, 2, 3, 4):
        body = await _history(client, symbol=tickers.symbol.lower(), page=page, page_size=2)
        assert body["total"] == 7
        seen.extend((e["type"], int(e["native_id"])) for e in body["entries"])

    assert seen == expected
    everything = await _history(client, symbol=tickers.symbol, page_size=100)
    assert [(e["type"], int(e["native_id"])) for e in everything["entries"]] == expected
    assert {e["created_at_ms"] for e in everything["entries"]} == {1_770_091_506_700, 1_770_091_507_123}


async def test_an_experiment_reads_as_its_store_holds_it_and_nothing_more(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers, history_over_no_files: None
) -> None:
    feature = await _research_row(db, tickers.id, "rsi_14", OLDEST)
    signal = await _signal_row(db, tickers.id, "momentum_5m", NEWEST, OverallGrade="F", StatusLabel="Exploratory")

    body = await _history(client, symbol=tickers.symbol)

    by_type = {e["type"]: e for e in body["entries"]}
    assert by_type["feature-experiment"] == {
        "type": "feature-experiment",
        "native_id": str(feature),
        "question": f"Does rsi_14 predict the next move of {tickers.symbol}?",
        "symbols": [tickers.symbol],
        "window": {"start_ms": JAN_2_2025_ET, "end_ms": JUN_30_2025_ET},
        "status": "completed",
        "status_detail": None,
        "created_at_ms": OLDEST_MS,
        "completed_at_ms": None,
        "provenance": None,
        "headline": (
            "Mean IC 0.1235, t-stat 2.50; ADF p 0.0010, KPSS p 0.4000 (stationary); monotonic (ratio 0.80); "
            "did not pass validation; 48,750 bars"
        ),
        "parent": None,
    }
    assert by_type["signal-report"]["native_id"] == str(signal)
    assert by_type["signal-report"]["headline"] == "Grade F (Exploratory); mean OOS Sharpe 0.65; best threshold 1; 48,750 bars"
    assert by_type["signal-report"]["created_at_ms"] == NEWEST_MS
    assert by_type["signal-report"]["provenance"] is None and by_type["signal-report"]["completed_at_ms"] is None


async def test_the_symbol_filter_offers_every_symbol_in_range_and_narrows_only_the_entries(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers, history_over_no_files: None
) -> None:
    await _research_row(db, tickers.id, "rsi_14", OLDEST)
    await _signal_row(db, tickers.other_id, "momentum_5m", NEWEST)

    body = await _history(client, symbol=tickers.symbol)

    assert body["total"] == 1
    assert {tickers.symbol, tickers.other_symbol} <= set(body["symbols"])


async def test_a_status_other_than_completed_matches_none_of_the_legacy_rows(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers, history_over_no_files: None
) -> None:
    await _research_row(db, tickers.id, "rsi_14", OLDEST)
    await _signal_row(db, tickers.id, "momentum_5m", NEWEST)

    failed = await _history(client, symbol=tickers.symbol, status="failed")
    completed = await _history(client, symbol=tickers.symbol, status="completed")

    assert (failed["total"], failed["entries"], failed["partial"]) == (0, [], False)
    assert completed["total"] == 2


async def test_the_time_filter_is_half_open_on_the_millisecond_the_rows_are_ordered_by(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers, history_over_no_files: None
) -> None:
    oldest = await _research_row(db, tickers.id, "a", OLDEST)
    middle = await _research_row(db, tickers.id, "b", MIDDLE)
    newest = await _research_row(db, tickers.id, "c", NEWEST)

    from_middle = await _history(client, symbol=tickers.symbol, created_from_ms=MIDDLE_MS)
    before_middle = await _history(client, symbol=tickers.symbol, created_before_ms=MIDDLE_MS)

    assert [int(e["native_id"]) for e in from_middle["entries"]] == [newest, middle]
    assert [int(e["native_id"]) for e in before_middle["entries"]] == [oldest]


async def test_a_stored_window_that_is_not_a_date_reads_as_not_recorded_and_the_row_stays(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers, history_over_no_files: None
) -> None:
    row = await _research_row(db, tickers.id, "rsi_14", OLDEST, StartDate="last spring")

    body = await _history(client, symbol=tickers.symbol)

    assert [(e["native_id"], e["window"]) for e in body["entries"]] == [(str(row), None)]
    assert body["total"] == 1
