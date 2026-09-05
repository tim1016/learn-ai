"""The Python-owned Recency Chart reads and mutations (PRD #1927) over HTTP."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.research.recency import repository as repo
from app.research.recency.runner import RecencyRunSnapshot, RecencyTradeSnapshot


def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(symbol: str) -> tuple[str, int]:
    launch_id = f"launch-{uuid.uuid4().hex[:10]}"
    await with_connection(repo.create_launch, launch_id=launch_id, config_json="{}", expected_runs=1)
    trade = RecencyTradeSnapshot(fingerprint=f"fp-{symbol}", entry_ms=1_000, exit_ms=2_000, pnl_pts=2.0, pnl_pct=0.02, quantity=10, pnl=20.0, holding_sessions=1, is_synthetic_exit=False, signal_reason="")
    snapshot = RecencyRunSnapshot(launch_id=launch_id, symbol=symbol, strategy_key="sma_crossover", params={"short_window": 2.0}, params_hash="hash1", total_pnl=20.0, sharpe=None, trades=[trade], study_id=None)
    outcome = await with_connection(repo.persist_snapshot, snapshot)
    assert outcome.recency_run_id is not None
    return launch_id, outcome.recency_run_id


async def test_trades_and_hero_read_back_as_json_numbers_and_the_hero_honours_entry_inside_the_window(client) -> None:
    _requires_ephemeral_db()
    symbol = f"T{uuid.uuid4().hex[:6].upper()}"
    _, run_id = await _seed(symbol)

    async with client as c:
        trades = await c.get("/api/research/recency/trades", params={"from_ms": 0, "to_ms": 5_000, "symbols": [symbol]})
        assert trades.status_code == 200, trades.text
        [trade] = trades.json()
        assert trade["recency_run_id"] == run_id and trade["pnl"] == 20.0 and isinstance(trade["pnl"], float)
        assert trade["sharpe"] is None and trade["memberships"] == [{"recency_run_id": run_id, "study_id": None, "created_at_ms": trade["memberships"][0]["created_at_ms"]}]

        hero = await c.get("/api/research/recency/hero", params={"from_ms": 0, "to_ms": 5_000, "symbols": [symbol]})
        assert hero.json() == {"heroes": [{"recency_run_id": run_id, "symbol": symbol, "strategy_key": "sma_crossover", "params_hash": "hash1", "total_pnl": 20.0}]}
        # Overlapping but entered before the window: drawn, not a hero candidate.
        late_window = await c.get("/api/research/recency/hero", params={"from_ms": 1_500, "to_ms": 5_000, "symbols": [symbol]})
        assert late_window.json() == {"heroes": []}
        drawn = await c.get("/api/research/recency/trades", params={"from_ms": 1_500, "to_ms": 5_000, "symbols": [symbol]})
        assert len(drawn.json()) == 1

        inverted = await c.get("/api/research/recency/hero", params={"from_ms": 10, "to_ms": 5})
        assert inverted.status_code == 400
        oversized = await c.get("/api/research/recency/trades", params={"from_ms": 0, "to_ms": 2**63})
        assert oversized.status_code == 422  # beyond int64: refused at the edge, not an asyncpg error inside the read


async def test_soft_delete_and_restore_verbs_replace_the_graphql_mutations(client) -> None:
    _requires_ephemeral_db()
    symbol = f"T{uuid.uuid4().hex[:6].upper()}"
    launch_id, run_id = await _seed(symbol)

    async with client as c:
        deleted = await c.post(f"/api/research/recency/runs/{run_id}/soft-delete")
        assert deleted.status_code == 200 and deleted.json() == {"recency_run_id": run_id}
        assert (await c.get("/api/research/recency/trades", params={"from_ms": 0, "to_ms": 5_000, "symbols": [symbol]})).json() == []
        restored = await c.post(f"/api/research/recency/runs/{run_id}/restore")
        assert restored.status_code == 200
        assert len((await c.get("/api/research/recency/trades", params={"from_ms": 0, "to_ms": 5_000, "symbols": [symbol]})).json()) == 1

        launch_gone = await c.post(f"/api/research/recency/launches/{launch_id}/soft-delete")
        assert launch_gone.json() == {"launch_id": launch_id}
        assert (await c.get("/api/research/recency/trades", params={"from_ms": 0, "to_ms": 5_000, "symbols": [symbol]})).json() == []
        assert (await c.post(f"/api/research/recency/launches/{launch_id}/restore")).status_code == 200

        missing = await c.post("/api/research/recency/runs/2147000000/soft-delete")
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "RECENCY_RUN_NOT_FOUND"


async def test_a_redelivered_job_id_is_acknowledged_only_while_its_worker_still_holds_the_job(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The durable launch keeps its first configuration (D20). While the job is live a redelivery is acknowledged
    without a second thread; a closed job and a changed grid are refused; an unknown answer is a 503."""
    _requires_ephemeral_db()
    dispatched: list[str] = []
    monkeypatch.setattr("app.routers.jobs.run_in_thread", lambda job_id, work, **kwargs: dispatched.append(job_id))
    live: list[bool | None] = [True]
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: live[0])
    body = {
        "jobId": f"job-{uuid.uuid4().hex[:10]}",
        "strategies": [{"strategyKey": "ema_crossover_signal", "paramRanges": {"gap_bps": {"type": "value_list", "values": [2.0]}}}],
        "symbols": ["SPY"],
        "windowStartMs": 0,
        "windowEndMs": 1,
    }

    async with client as c:
        first = await c.post("/api/jobs-internal/recency-chart", json=body)
        assert first.status_code == 202, first.text
        while_live = await c.post("/api/jobs-internal/recency-chart", json=body)
        assert while_live.status_code == 202, while_live.text
        live[0] = None
        unknown = await c.post("/api/jobs-internal/recency-chart", json=body)
        assert unknown.status_code == 503, unknown.text
        live[0] = False
        closed = await c.post("/api/jobs-internal/recency-chart", json=body)
        assert closed.status_code == 409 and "no longer running" in closed.json()["detail"], closed.text
        changed = await c.post("/api/jobs-internal/recency-chart", json={**body, "windowEndMs": 2})

    assert changed.status_code == 409, changed.text
    assert "different configuration" in changed.json()["detail"]
    assert dispatched == [body["jobId"]]  # one worker; a redelivery never starts another
