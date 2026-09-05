"""Walk-Forward Study HTTP boundary (PRD #1925 "HTTP contract")."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.lean_sidecar.trading_calendar import expected_sessions
from app.main import app
from app.research.grid_search import service as sweeps
from app.research.grid_search.models import CellResult
from app.research.persistence import lifecycle
from app.research.walk_forward_study import service
from app.routers import walk_forward_study as study_router
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.lean_store import seed_store_day

START, END_EXCLUSIVE = date(2025, 1, 1), date(2025, 4, 1)
SESSIONS = expected_sessions(START, date(2025, 3, 31))


def _body(**overrides) -> dict:
    body = {
        "strategy_key": "sma_crossover",
        "symbol": "SPY",
        "param_ranges": {
            "short_window": {"type": "value_list", "values": [2, 3]},
            "long_window": {"type": "value_list", "values": [5]},
            "resolution_minutes": {"type": "value_list", "values": [60]},
        },
        "start_ms": et_midnight_ms(START),
        "end_ms": et_midnight_ms(END_EXCLUSIVE),
        "training_months": 1,
        "test_months": 1,
        "measure": "sharpe_ratio",
        "min_trades": 1,
    }
    body.update(overrides)
    return body


@pytest.fixture(scope="module")
def lake_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("lake")
    for day in SESSIONS:
        seed_store_day(root, "SPY", day, count=120)
    return root


@pytest.fixture
def lake(lake_root: Path, monkeypatch) -> Path:
    monkeypatch.setattr(sweeps, "resolve_data_roots", lambda **kwargs: [lake_root])
    monkeypatch.setattr(lifecycle, "resolve_data_roots", lambda **kwargs: [lake_root])
    return lake_root


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


def _fake_factory(row, spec):
    def execute(candidate) -> CellResult:
        # Training favours short=3 in fold 0 and short=2 in fold 1; each keeps most of its Sharpe out of sample.
        favourite = 3.0 if row.owner.fold_index == 0 else 2.0
        sharpe = (2.0 if candidate.params["short_window"] == favourite else 0.5) * (0.8 if row.owner.phase == "test" else 1.0)
        return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=3, sharpe_ratio=sharpe, total_return_pct=1.0, net_profit=5.0)

    return execute


async def test_preflight_reports_the_folds_and_the_workload(client, lake) -> None:
    async with client as c:
        response = await c.post("/api/research/walk-forward-studies/preflight", json=_body())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["combinations"] == 2 and payload["fold_count"] == 2 and payload["total_backtests"] == 8
    assert payload["backtest_limit"] == 5000 and payload["estimated_seconds"] > 0
    assert [fold["fold_index"] for fold in payload["folds"]] == [0, 1]
    assert payload["folds"][0]["train_start_ms"] == et_midnight_ms(date(2025, 1, 2))
    assert payload["folds"][0]["test_start_ms"] == payload["folds"][0]["train_end_ms"] == et_midnight_ms(date(2025, 2, 3))
    assert payload["folds"][1]["test_end_ms"] == et_midnight_ms(END_EXCLUSIVE)


async def test_a_range_that_does_not_make_whole_folds_is_refused_with_the_nearest_ends(client, lake) -> None:
    async with client as c:
        response = await c.post("/api/research/walk-forward-studies/preflight", json=_body(end_ms=et_midnight_ms(date(2025, 3, 15))))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FOLDS_INVALID"
    assert "2025-02-28" in response.json()["detail"]["message"]  # inclusive, as the form takes it


async def test_month_lengths_are_validated_by_the_schema(client, lake) -> None:
    async with client as c:
        response = await c.post("/api/research/walk-forward-studies/preflight", json=_body(training_months=0))
    assert response.status_code == 422


async def test_launch_lists_by_job_id_and_the_detail_carries_folds_and_the_verdict(client, lake, monkeypatch) -> None:
    _requires_ephemeral_db()
    job_id = f"job-wf-{uuid.uuid4().hex[:8]}"
    captured: dict[str, object] = {}
    monkeypatch.setattr(study_router, "run_in_thread", lambda jid, work, **kwargs: captured.setdefault("job_id", jid))
    monkeypatch.setattr(lifecycle, "job_is_live", lambda jid: True)

    async with client as c:
        launched = await c.post("/api/jobs-internal/walk-forward-study", json={**_body(), "jobId": job_id})
        assert launched.status_code == 202, launched.text
        study_id = launched.json()["study_id"]

        listed = await c.get("/api/research/walk-forward-studies", params={"job_id": job_id})
        assert [row["id"] for row in listed.json()] == [study_id]
        assert listed.json()[0]["status"] == "queued" and listed.json()[0]["fold_count"] == 2

        await asyncio.to_thread(service.execute, study_id, job_id=job_id, cell_executor=_fake_factory)

        detail = await c.get(f"/api/research/walk-forward-studies/{study_id}")
        assert detail.status_code == 200, detail.text
        payload = detail.json()
        assert payload["status"] == "completed" and payload["completed_folds"] == 2
        assert [fold["winner_params"]["short_window"] for fold in payload["folds"]] == [3.0, 2.0]
        assert all(fold["train_search_id"] and fold["test_search_id"] for fold in payload["folds"])
        assert payload["verdict"]["label"] == "still worked" and payload["verdict"]["based_on"] == "based on 2 of 2 folds"
        assert payload["winner_changes"] == 1
        assert payload["resumable"] is False and payload["resume_refusal"] == "the study is complete"

        # The fold sweeps read through the Grid Search surface; the winner's test cell is the evidence.
        cells = await c.get(f"/api/research/grid-search/{payload['folds'][0]['test_search_id']}/cells", params={"page_size": 10})
        assert cells.status_code == 200
        by_short = {cell["params"]["short_window"]: cell for cell in cells.json()["cells"]}
        assert by_short[3.0]["exploratory"] is False and by_short[2.0]["exploratory"] is True
        # ...but never in the Grid Search history, and never deletable or resumable there.
        history = await c.get("/api/research/grid-search", params={"job_id": job_id})
        assert history.json() == []
        owned = payload["folds"][0]["train_search_id"]
        refused = await c.delete(f"/api/research/grid-search/{owned}")
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "OWNED_BY_STUDY"
        grid_body = {k: v for k, v in _body().items() if k not in ("training_months", "test_months")}
        resumed_owned = await c.post("/api/jobs-internal/grid-search", json={**grid_body, "jobId": "job-owned", "resumeSearchId": owned})
        assert resumed_owned.status_code == 409 and resumed_owned.json()["detail"]["code"] == "OWNED_BY_STUDY"

        resumed = await c.post("/api/jobs-internal/walk-forward-study", json={**_body(), "jobId": "job-wf-finish", "resumeStudyId": study_id})
        assert resumed.status_code == 409 and resumed.json()["detail"]["code"] == "NOT_RESUMABLE"

        deleted = await c.delete(f"/api/research/walk-forward-studies/{study_id}")
        assert deleted.status_code == 204
        assert (await c.get(f"/api/research/walk-forward-studies/{study_id}")).status_code == 404
        assert (await c.get(f"/api/research/grid-search/{payload['folds'][0]['train_search_id']}")).status_code == 404

    assert captured["job_id"] == job_id
