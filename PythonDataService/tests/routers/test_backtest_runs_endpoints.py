"""The Python-owned backtest-run reads and verbs (PRD #1929) over HTTP."""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.records import record_from_payload
from app.research.persistence.db import with_connection
from tests.research.backtest_runs.payloads import ENTRY_MS, engine_payload, lean_payload, trade


def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _symbol() -> str:
    return f"T{uuid.uuid4().hex[:6].upper()}"


async def _seed(payload: dict) -> int:
    return (await with_connection(repo.insert_run, record_from_payload(payload))).run_id


async def test_history_lists_newest_first_filters_by_engine_and_keeps_the_graphql_field_names(client) -> None:
    _requires_ephemeral_db()
    symbol = _symbol()
    engine_id = await _seed(engine_payload(symbol=symbol))
    lean_id = await _seed(lean_payload(f"lean-{symbol}", symbol=symbol, trades=[trade(synthetic=True)]))

    async with client as c:
        listed = await c.get("/api/research/backtest-runs", params={"limit": 500})
        assert listed.status_code == 200, listed.text
        rows = [row for row in listed.json() if row["symbol"] == symbol]
        assert [row["id"] for row in rows] == [lean_id, engine_id]
        lean_row, engine_row = rows
        assert set(engine_row) == {
            "id", "source", "engine", "strategyName", "symbol", "leanRunId", "parameters", "startDate", "endDate",
            "executedAt", "totalTrades", "totalPnL", "commissionPerOrder", "brokeragePolicy", "notes", "dataPolicy",
            "verdictGrade", "verdictSignal", "parityGroupId", "hasSyntheticExit",
        }
        assert engine_row["engine"] == "PYTHON" and engine_row["source"] == "engine" and engine_row["totalPnL"] == 20.0
        assert engine_row["startDate"] == "2025-01-06" and engine_row["endDate"] == "2025-01-10"
        assert json.loads(engine_row["parameters"]) == {"symbol": symbol, "gap_bps": 0.0}
        assert engine_row["dataPolicy"]["input_bars"] == {"timespan": "minute", "multiplier": 1}
        assert lean_row["engine"] == "LEAN" and lean_row["hasSyntheticExit"] is True and lean_row["leanRunId"] == f"lean-{symbol}"

        python_only = await c.get("/api/research/backtest-runs", params={"engine": "PYTHON", "limit": 500})
        assert [row["id"] for row in python_only.json() if row["symbol"] == symbol] == [engine_id]
        lean_only = await c.get("/api/research/backtest-runs", params={"engine": "LEAN", "limit": 500})
        assert [row["id"] for row in lean_only.json() if row["symbol"] == symbol] == [lean_id]
        assert (await c.get("/api/research/backtest-runs", params={"engine": "QC"})).status_code == 422
        assert (await c.get("/api/research/backtest-runs", params={"limit": 0})).status_code == 422


async def test_the_run_report_serves_the_stored_envelopes_as_the_producers_wrote_them(client) -> None:
    _requires_ephemeral_db()
    symbol = _symbol()
    group = f"pg-{symbol}"
    run_id = await _seed(engine_payload(symbol=symbol, parity_group_id=group, requested_engine="both"))
    await with_connection(repo.create_parity_verdict, parity_group_id=group, left_run_id=run_id, status="pending", verdict_json='{"status":"pending"}')

    async with client as c:
        detail = await c.get(f"/api/research/backtest-runs/{run_id}")
        assert detail.status_code == 200, detail.text
        run = detail.json()
        assert run["id"] == run_id and run["engine"] == "PYTHON" and run["requestedEngine"] == "both"
        assert run["strategyName"] == "ema_crossover_signal" and run["symbol"] == symbol
        assert (run["totalPnL"], run["initialCash"], run["finalEquity"], run["totalFees"]) == (20.0, 100_000.0, 100_020.0, 0.0)
        assert (run["maxDrawdown"], run["sharpeRatio"], run["sortinoRatio"], run["profitFactor"]) == (0.0257, 1.43, 2.59, 2.0)
        assert json.loads(run["leanStatisticsJson"])["portfolio"]["sharpe_ratio"] == 1.54
        assert json.loads(run["verdictJson"])["grade"] == "A" and run["verdictGrade"] == "A"
        assert run["equityCurve"]["realized"]["points"][-1] == {"t": 1_736_179_200_000, "e": 100_020.0}
        assert run["equityCurve"]["mark_to_market"]["cadence"] == "strategy_bar_close"
        assert run["validationAnalytics"]["engine"] == "python" and set(run["validationAnalytics"]["analytics"]) == {"horizons", "timing_cells"}
        assert run["metricDocumentation"][0]["variant_id"] == "sharpe.platform.v1"
        assert run["dataPolicy"]["strategy_bars"] == {"timespan": "minute", "multiplier": 15}
        assert json.loads(run["insightSummaryJson"]) == {"total": 1}
        [persisted] = run["trades"]
        assert persisted["entryTimestamp"] == ENTRY_MS and persisted["pnL"] == 20.0
        assert persisted["pnlPts"] == pytest.approx(2.0) and persisted["pnlPct"] == pytest.approx(2.0 / 710.0)
        assert run["tradesTruncated"] is False
        [verdict] = run["parityVerdicts"]
        assert verdict["status"] == "pending" and json.loads(verdict["verdictJson"]) == {"status": "pending"} and verdict["createdAt"] > 0

        missing = await c.get("/api/research/backtest-runs/999999999")
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "BACKTEST_RUN_NOT_FOUND"


async def test_a_run_with_more_than_five_hundred_trades_reports_itself_truncated(client) -> None:
    _requires_ephemeral_db()
    symbol = _symbol()
    trades = [trade(number, entry_ms=ENTRY_MS + number * 60_000, exit_ms=ENTRY_MS + number * 60_000 + 30_000) for number in range(1, 502)]
    run_id = await _seed(engine_payload(symbol=symbol, trades=trades, total_trades=501))

    async with client as c:
        run = (await c.get(f"/api/research/backtest-runs/{run_id}")).json()

    assert run["tradesTruncated"] is True and run["totalTrades"] == 501 and len(run["trades"]) == 500
    assert run["trades"][0]["entryTimestamp"] < run["trades"][-1]["entryTimestamp"]


async def test_notes_round_trip_and_delete_refuses_a_live_recency_member(client) -> None:
    _requires_ephemeral_db()
    symbol = _symbol()
    run_id = await _seed(engine_payload(symbol=symbol))

    async with client as c:
        noted = await c.patch(f"/api/research/backtest-runs/{run_id}/notes", json={"notes": "keep"})
        assert noted.status_code == 200 and noted.json() == {"id": run_id, "notes": "keep"}
        assert (await c.get(f"/api/research/backtest-runs/{run_id}")).json()["notes"] == "keep"
        assert (await c.patch("/api/research/backtest-runs/999999999/notes", json={"notes": "x"})).status_code == 404

        async def _recency_member(conn, deleted_at_ms: int | None) -> int:
            launch_id = f"launch-{symbol}"
            await conn.execute(
                """
                INSERT INTO "RecencyLaunches" ("Id", "ConfigJson", "ExpectedRuns", "SucceededRuns", "FailedRuns", "Status", "CreatedAtMs")
                VALUES ($1, '{}'::jsonb, 1, 1, 0, 'COMPLETED', 1) ON CONFLICT ("Id") DO NOTHING
                """,
                launch_id,
            )
            return await conn.fetchval(
                """
                INSERT INTO "RecencyRuns" ("RecencyLaunchId", "StrategyKey", "Symbol", "ParamsJson", "ParamsHash", "StudyId", "TotalPnl", "CreatedAtMs", "DeletedAtMs")
                VALUES ($1, 'sma_crossover', $2, '{}'::jsonb, 'h', $3, 0, 1, $4) RETURNING "Id"
                """,
                launch_id,
                symbol,
                run_id,
                deleted_at_ms,
            )

        recency_run_id = await with_connection(_recency_member, None)
        refused = await c.delete(f"/api/research/backtest-runs/{run_id}")
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "RECENCY_MEMBER"

        await with_connection(lambda conn: conn.execute('UPDATE "RecencyRuns" SET "DeletedAtMs" = 1 WHERE "Id" = $1', recency_run_id))
        assert (await c.delete(f"/api/research/backtest-runs/{run_id}")).status_code == 204
        assert (await c.get(f"/api/research/backtest-runs/{run_id}")).status_code == 404
        assert (await c.delete(f"/api/research/backtest-runs/{run_id}")).status_code == 404
