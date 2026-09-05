"""Grid Search HTTP boundary (PRD #1926 "Testing decisions — HTTP")."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.lean_sidecar.trading_calendar import expected_sessions
from app.main import app
from app.research.grid_search import service
from app.research.grid_search.models import CellResult
from app.research.persistence import lifecycle
from app.routers import grid_search as grid_search_router
from tests._helpers.lean_store import seed_store_day

START, END = date(2025, 1, 6), date(2025, 1, 24)
SESSIONS = expected_sessions(START, END)
DAY_MS = 24 * 60 * 60 * 1000


def _body(**overrides) -> dict:
    body = {
        "strategy_key": "sma_crossover",
        "symbol": "SPY",
        "param_ranges": {
            "short_window": {"type": "value_list", "values": [2, 3]},
            "long_window": {"type": "value_list", "values": [5]},
            "resolution_minutes": {"type": "value_list", "values": [60]},
        },
        "start_ms": service.et_midnight_ms(START),
        "end_ms": service.et_midnight_ms(END) + DAY_MS,
        "measure": "sharpe_ratio",
        "min_trades": 1,
    }
    body.update(overrides)
    return body


@pytest.fixture
def lake(tmp_path: Path, monkeypatch) -> Path:
    for day in SESSIONS:
        seed_store_day(tmp_path, "SPY", day)
    monkeypatch.setattr(service, "resolve_data_roots", lambda **kwargs: [tmp_path])
    return tmp_path


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


# ── Preflight: every refusal is a clear client error before execution ────


async def test_preflight_reports_the_workload_and_run_up(client, lake) -> None:
    async with client as c:
        response = await c.post("/api/research/grid-search/preflight", json=_body())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["combinations"] == 2 and payload["total_backtests"] == 2
    assert payload["backtest_limit"] == 5000
    assert payload["run_up"]["carved_from_range"] is True
    assert payload["run_up"]["evaluation_start_ms"] == service.et_midnight_ms(SESSIONS[1])
    assert payload["estimated_seconds"] > 0


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"strategy_key": "unknown_strategy"}, "UNKNOWN_STRATEGY"),
        ({"strategy_key": "deployment_validation"}, "STRATEGY_NOT_SWEEPABLE"),
        ({"param_ranges": {"short_window": {"type": "value_list", "values": [2, 2]}}}, "GRID_INVALID"),
        ({"param_ranges": {"short_window": {"type": "low_high_step", "low": 2, "high": 200, "step": 1}, "long_window": {"type": "low_high_step", "low": 201, "high": 260, "step": 1}}}, "WORKLOAD_LIMIT"),
    ],
)
async def test_preflight_refusals_are_400_with_a_code(client, lake, overrides, code) -> None:
    async with client as c:
        response = await c.post("/api/research/grid-search/preflight", json=_body(**overrides))

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == code


async def test_a_degenerate_window_is_rejected_by_the_schema(client, lake) -> None:
    async with client as c:
        response = await c.post("/api/research/grid-search/preflight", json=_body(start_ms=5, end_ms=5))

    assert response.status_code == 422


async def test_missing_sessions_are_named_in_the_refusal(client, tmp_path: Path, monkeypatch) -> None:
    for day in SESSIONS[:-1]:
        seed_store_day(tmp_path, "SPY", day)
    monkeypatch.setattr(service, "resolve_data_roots", lambda **kwargs: [tmp_path])

    async with client as c:
        response = await c.post("/api/research/grid-search/preflight", json=_body())

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "DATA_MISSING" and SESSIONS[-1].isoformat() in detail["message"]


# ── Lifecycle over HTTP (ephemeral database) ─────────────────────────────


async def test_launch_lists_immediately_and_the_result_pages_on_the_server(client, lake, monkeypatch) -> None:
    _requires_ephemeral_db()
    ran: dict[str, object] = {}

    def fake_run_in_thread(job_id, work, **kwargs):
        ran["job_id"] = job_id
        ran["work"] = work
        return None

    monkeypatch.setattr(grid_search_router, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)

    async with client as c:
        launched = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": f"job-http-1-{id(monkeypatch)}"})
        assert launched.status_code == 202, launched.text
        search_id = launched.json()["search_id"]

        listed = await c.get("/api/research/grid-search", params={"job_id": f"job-http-1-{id(monkeypatch)}"})
        assert [row["id"] for row in listed.json()] == [search_id]
        assert listed.json()[0]["status"] == "queued"

        # Run the captured worker body synchronously with a fake engine.
        def engine(candidate) -> CellResult:
            return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=3, sharpe_ratio=candidate.params["short_window"], total_return_pct=1.0, net_profit=5.0)

        import asyncio

        await asyncio.to_thread(service.execute, search_id, job_id=f"job-http-1-{id(monkeypatch)}", execute_cell=engine)

        detail = await c.get(f"/api/research/grid-search/{search_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"
        assert detail.json()["leader_params"]["short_window"] == 3.0
        assert detail.json()["resumable"] is False and detail.json()["resume_refusal"] == "the search is complete"

        page = await c.get(f"/api/research/grid-search/{search_id}/cells", params={"sort_by": "sharpe_ratio", "direction": "desc", "page": 1, "page_size": 1})
        assert page.status_code == 200
        assert page.json()["total"] == 2 and page.json()["cells"][0]["is_leader"] is True
        assert page.json()["cells"][0]["eligible"] is True

        bad_sort = await c.get(f"/api/research/grid-search/{search_id}/cells", params={"sort_by": "params_json"})
        assert bad_sort.status_code == 400

        filtered = await c.get("/api/research/grid-search", params={"strategy_key": "rsi_mean_reversion"})
        assert all(row["id"] != search_id for row in filtered.json())

        deleted = await c.delete(f"/api/research/grid-search/{search_id}")
        assert deleted.status_code == 204
        assert (await c.get(f"/api/research/grid-search/{search_id}")).status_code == 404

    assert ran["job_id"] == f"job-http-1-{id(monkeypatch)}"


async def test_finish_of_a_completed_search_is_refused(client, lake, monkeypatch) -> None:
    _requires_ephemeral_db()
    monkeypatch.setattr(grid_search_router, "run_in_thread", lambda job_id, work, **kwargs: None)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: False)

    async with client as c:
        launched = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": "job-http-2"})
        search_id = launched.json()["search_id"]
        import asyncio

        await asyncio.to_thread(
            service.execute,
            search_id,
            job_id="job-http-2",
            execute_cell=lambda candidate: CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=3, sharpe_ratio=1.0),
        )

        resumed = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": "job-http-3", "resumeSearchId": search_id})

    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "NOT_RESUMABLE"
