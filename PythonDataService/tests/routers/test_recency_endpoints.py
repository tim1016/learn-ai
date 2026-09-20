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
    await with_connection(repo.claim_launch, launch_id=launch_id, job_id=launch_id)  # attempt 1, as the worker does
    trade = RecencyTradeSnapshot(fingerprint=f"fp-{symbol}", entry_ms=1_000, exit_ms=2_000, pnl_pts=2.0, pnl_pct=0.02, quantity=10, pnl=20.0, holding_sessions=1, is_synthetic_exit=False, signal_reason="")
    snapshot = RecencyRunSnapshot(launch_id=launch_id, symbol=symbol, strategy_key="sma_crossover", params={"short_window": 2.0}, params_hash="hash1", total_pnl=20.0, sharpe=None, trades=[trade], study_id=None)
    outcome = await with_connection(repo.persist_snapshot, snapshot, attempt=1)
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


def _stored_spec_json() -> str:
    """The snake_case spec shape ``create_launch`` stores (model_dump without aliases)."""
    import json

    return json.dumps(
        {
            "strategies": [{"strategy_key": "ema_crossover_signal", "param_ranges": {"gap_bps": {"type": "value_list", "values": [2.0]}}}],
            "symbols": ["SPY"],
            "window_start_ms": 0,
            "window_end_ms": 1,
        }
    )


async def _seed_launch(unique: str, *, status: str = "FAILED", deleted: bool = False) -> str:
    """A durable launch, created and claimed the way the worker does, then closed as ``status``."""
    launch_id = f"launch-{unique}"
    await with_connection(repo.create_launch, launch_id=launch_id, config_json=_stored_spec_json(), expected_runs=1)
    await with_connection(repo.claim_launch, launch_id=launch_id, job_id=f"job-{unique}")
    if status == "COMPLETED":
        await with_connection(repo.set_terminal_status, launch_id, status="COMPLETED", attempt=1, succeeded_runs=1, failed_runs=0)
    elif status == "FAILED":
        await with_connection(repo.set_terminal_status, launch_id, status="FAILED", attempt=1, succeeded_runs=0, failed_runs=1)
    if deleted:
        await with_connection(repo.set_launch_deleted, launch_id, deleted=True)
    return launch_id


def _resume_body(job_id: str, launch_id: str) -> dict:
    return {
        "jobId": job_id,
        "resumeLaunchId": launch_id,
        "strategies": [{"strategyKey": "ema_crossover_signal", "paramRanges": {"gap_bps": {"type": "value_list", "values": [2.0]}}}],
        "symbols": ["SPY"],
        "windowStartMs": 0,
        "windowEndMs": 1,
    }


async def test_a_resume_binds_a_new_job_to_the_existing_launch(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resume dispatch is a fresh job id for the old durable launch; the record
    is untouched until the captured worker's claim rebinds it (#1938)."""
    _requires_ephemeral_db()
    unique = uuid.uuid4().hex[:10]
    launch_id = await _seed_launch(unique)
    dispatched: list[str] = []
    monkeypatch.setattr("app.routers.jobs.run_in_thread", lambda job_id, work, **kwargs: dispatched.append(job_id))
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: False)
    new_job = f"job-{unique}-resume"

    async with client as c:
        response = await c.post("/api/jobs-internal/recency-chart", json=_resume_body(new_job, launch_id))

    assert response.status_code == 202, response.text
    assert response.json() == {"job_id": new_job, "launch_id": launch_id, "status": "queued"}
    assert dispatched == [new_job]  # one worker, under the new id
    row = await with_connection(repo.load_launch, launch_id)
    assert (row.attempt, row.job_id, row.status) == (1, f"job-{unique}", "FAILED")  # the worker's claim does the rebinding


async def test_a_resume_is_refused_for_an_unknown_completed_running_or_deleted_launch(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _requires_ephemeral_db()
    unique = uuid.uuid4().hex[:10]
    completed = await _seed_launch(f"{unique}c", status="COMPLETED")
    running = await _seed_launch(f"{unique}r", status="RUNNING")
    deleted = await _seed_launch(f"{unique}d", status="FAILED", deleted=True)
    monkeypatch.setattr("app.routers.jobs.run_in_thread", lambda job_id, work, **kwargs: None)
    live: list[bool | None] = [True]
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: live[0])

    async with client as c:
        missing = await c.post("/api/jobs-internal/recency-chart", json=_resume_body(f"job-{unique}m", f"launch-{unique}m"))
        assert missing.status_code == 404, missing.text
        assert missing.json()["detail"]["code"] == "RECENCY_LAUNCH_NOT_FOUND"

        still_running = await c.post("/api/jobs-internal/recency-chart", json=_resume_body(f"job-{unique}r", running))
        assert still_running.status_code == 409, still_running.text
        assert "still running" in still_running.json()["detail"]["message"]

        live[0] = False
        done = await c.post("/api/jobs-internal/recency-chart", json=_resume_body(f"job-{unique}c", completed))
        assert done.status_code == 409, done.text
        assert "complete" in done.json()["detail"]["message"]

        gone = await c.post("/api/jobs-internal/recency-chart", json=_resume_body(f"job-{unique}d", deleted))
        assert gone.status_code == 409, gone.text
        assert "restore" in gone.json()["detail"]["message"]
        assert gone.json()["detail"]["code"] == "NOT_RESUMABLE"


async def test_the_launches_list_presents_status_and_the_resume_gate(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _requires_ephemeral_db()
    unique = uuid.uuid4().hex[:10]
    done = await _seed_launch(f"{unique}c", status="COMPLETED")
    interrupted = await _seed_launch(f"{unique}r", status="RUNNING")
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: False)

    async with client as c:
        response = await c.get("/api/research/recency/launches", params={"limit": 100})

    assert response.status_code == 200, response.text
    rows = {row["launch_id"]: row for row in response.json()}
    assert rows[done]["status"] == "completed"
    assert rows[done]["resumable"] is False and "complete" in rows[done]["resume_refusal"]
    assert rows[interrupted]["status"] == "interrupted"
    assert rows[interrupted]["resumable"] is True and rows[interrupted]["resume_refusal"] is None
    assert rows[interrupted]["attempt"] == 1 and rows[interrupted]["expected_runs"] == 1
