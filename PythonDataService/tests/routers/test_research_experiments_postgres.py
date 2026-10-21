"""The experiment history against the real ``"ResearchExperiments"`` / ``"SignalExperiments"`` / ``"Tickers"`` tables.

These run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. Every
test seeds tickers of its own (a symbol made from the test's tag); removing them removes their experiments with them,
as the tables' foreign keys cascade.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.data_lake import catalog_client
from app.main import app
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


async def test_a_tickers_research_experiments_come_newest_first_and_only_its_own(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers
) -> None:
    oldest = await _research_row(db, tickers.id, "rsi_14", OLDEST)
    middle = await _research_row(db, tickers.id, "macd", MIDDLE, PassedValidation=True, IsStationary=False)
    newest = await _research_row(db, tickers.id, "rsi_14", NEWEST)
    tied = await _research_row(db, tickers.id, "ema_20", NEWEST)
    await _research_row(db, tickers.other_id, "rsi_14", NEWEST)

    response = await client.get(f"{BASE}/experiments", params={"ticker": tickers.symbol.lower()})

    assert response.status_code == 200, response.text
    rows = response.json()
    # A tie on the creation instant falls back to the newer id, so the order is the same on every call.
    assert [row["id"] for row in rows] == [tied, newest, middle, oldest]
    assert {row["ticker"] for row in rows} == {tickers.symbol}
    assert rows[3] == {
        "id": oldest,
        "ticker": tickers.symbol,
        "feature_name": "rsi_14",
        "start_date": "2025-01-02",
        "end_date": "2025-06-30",
        "bars_used": 48_750,
        "mean_ic": 0.12345678,
        "ic_t_stat": 2.5,
        "ic_p_value": 0.012,
        "adf_p_value": 0.001,
        "kpss_p_value": 0.4,
        "is_stationary": True,
        "passed_validation": False,
        "monotonicity_ratio": 0.8,
        "is_monotonic": True,
        "created_at_ms": OLDEST_MS,
    }
    assert (rows[2]["created_at_ms"], rows[2]["passed_validation"], rows[2]["is_stationary"]) == (
        MIDDLE_MS,
        True,
        False,
    )
    assert rows[1]["created_at_ms"] == NEWEST_MS


async def test_the_feature_name_filter_keeps_only_that_features_experiments(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers
) -> None:
    older = await _research_row(db, tickers.id, "rsi_14", OLDEST)
    await _research_row(db, tickers.id, "macd", MIDDLE)
    newer = await _research_row(db, tickers.id, "rsi_14", NEWEST)

    filtered = await client.get(f"{BASE}/experiments", params={"ticker": tickers.symbol, "feature_name": "rsi_14"})
    unmatched = await client.get(f"{BASE}/experiments", params={"ticker": tickers.symbol, "feature_name": "RSI_14"})

    assert [row["id"] for row in filtered.json()] == [newer, older]
    assert unmatched.json() == []


async def test_a_ticker_with_no_experiments_has_an_empty_list(client: httpx.AsyncClient, tickers: Tickers) -> None:
    for path in ("experiments", "signal-experiments"):
        response = await client.get(f"{BASE}/{path}", params={"ticker": tickers.symbol})
        assert (response.status_code, response.json()) == (200, [])
    unknown = await client.get(f"{BASE}/experiments", params={"ticker": "NOSUCHTICKER"})
    assert (unknown.status_code, unknown.json()) == (200, [])


async def test_a_tickers_signal_experiments_come_newest_first_with_their_headlines(
    client: httpx.AsyncClient, db: asyncpg.Connection, tickers: Tickers
) -> None:
    oldest = await _signal_row(
        db, tickers.id, "momentum_5m", OLDEST, OverallGrade="F", StatusLabel="Exploratory", OverallPassed=False
    )
    newest = await _signal_row(db, tickers.id, "momentum_5m", NEWEST)
    await _signal_row(db, tickers.other_id, "momentum_5m", MIDDLE)

    response = await client.get(f"{BASE}/signal-experiments", params={"ticker": tickers.symbol})

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["id"] for row in rows] == [newest, oldest]
    assert rows[1] == {
        "id": oldest,
        "ticker": tickers.symbol,
        "feature_name": "momentum_5m",
        "start_date": "2025-01-02",
        "end_date": "2025-06-30",
        "bars_used": 48_750,
        "overall_grade": "F",
        "status_label": "Exploratory",
        "overall_passed": False,
        "mean_oos_sharpe": 0.65,
        "best_threshold": 1.0,
        "best_cost_bps": 2.5,
        "flip_sign": False,
        "regime_gate_enabled": True,
        "created_at_ms": OLDEST_MS,
    }
    assert (rows[0]["created_at_ms"], rows[0]["overall_grade"]) == (NEWEST_MS, "B")


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
