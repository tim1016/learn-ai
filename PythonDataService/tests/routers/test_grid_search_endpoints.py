"""Grid Search HTTP boundary (PRD #1926 "Testing decisions — HTTP")."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
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


async def test_a_cells_curve_is_read_by_its_hash_on_the_searchs_capital_and_a_cell_without_one_is_named(client, lake, monkeypatch) -> None:
    _requires_ephemeral_db()
    monkeypatch.setattr(grid_search_router, "run_in_thread", lambda job_id, work, **kwargs: None)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)
    job = f"job-http-curves-{id(monkeypatch)}"
    days = [session_close_ms_utc(day) for day in SESSIONS[:3]]

    async with client as c:
        launched = await c.post("/api/jobs-internal/grid-search", json={**_body(initial_cash=50_000.0), "jobId": job})
        assert launched.status_code == 202, launched.text
        search_id = launched.json()["search_id"]

        def engine(candidate) -> CellResult:
            # One cell keeps its curve; the other is recorded without one, as a cell from before cells kept theirs.
            kept = candidate.params["short_window"] == 2.0
            curve = [(days[0], 50_500.0), (days[1], 49_995.0), (days[2], 51_005.0)] if kept else None
            return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=3, sharpe_ratio=1.0, daily_equity=curve)

        await asyncio.to_thread(service.execute, search_id, job_id=job, execute_cell=engine)

        cells = f"/api/research/grid-search/{search_id}/cells"
        page = (await c.get(cells)).json()
        by_window = {cell["params"]["short_window"]: cell for cell in page["cells"]}
        drawn, undrawn = by_window[2.0]["params_hash"], by_window[3.0]["params_hash"]
        assert (by_window[2.0]["has_curve"], by_window[3.0]["has_curve"], page["without_curve"]) == (True, False, 1)

        curves = await c.get(f"{cells}/curves", params=[("params_hash", undrawn), ("params_hash", drawn), ("params_hash", "no-such-cell"), ("params_hash", drawn)])
        assert curves.status_code == 200, curves.text
        body = curves.json()
        assert body["sessions_ms"] == days and body["not_recorded"] == [undrawn, "no-such-cell"]
        (curve,) = body["curves"]
        assert (curve["params_hash"], curve["first_session"]) == (drawn, 0)
        # On the search's own starting capital, 50,000: 50,500 is +1%.
        assert curve["cumulative_return"] == pytest.approx([0.01, -0.0001, 0.0201], abs=1e-12, rel=0)
        assert curve["drawdown"] == pytest.approx([0.0, 49_995.0 / 50_500.0 - 1.0, 0.0], abs=1e-12, rel=0)
        assert curve["deepest_drawdown"] == pytest.approx(49_995.0 / 50_500.0 - 1.0, abs=1e-12, rel=0)

        assert (await c.get("/api/research/grid-search/no-such-search/cells/curves", params={"params_hash": drawn})).status_code == 404
        assert (await c.get(f"{cells}/curves")).status_code == 422
        too_many = [("params_hash", f"cell-{index}") for index in range(grid_search_router.MAX_CURVES + 1)]
        assert (await c.get(f"{cells}/curves", params=too_many)).status_code == 422
        await c.delete(f"/api/research/grid-search/{search_id}")


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


# ── Research-mode forwarding and idempotent dispatch (#3049) ─────────────


async def test_research_mode_forwards_the_dispatch_and_never_binds(client, monkeypatch) -> None:
    """In research mode the API hands the dispatch over and prepares, stamps and runs nothing (#3049)."""
    import respx
    from httpx import Response as HttpxResponse

    from app.config import settings

    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "research")
    monkeypatch.setattr(settings, "RESEARCH_RUNTIME_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("RESEARCH_RUNTIME", raising=False)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")

    def never_prepares(*args, **kwargs):
        raise AssertionError("the forwarding API never prepares a launch")

    monkeypatch.setattr(service, "prepare_launch", never_prepares)

    def never_runs(job_id, work, **kwargs):
        raise AssertionError("the forwarding API never starts a worker thread")

    monkeypatch.setattr(grid_search_router, "run_in_thread", never_runs)

    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        route = mock.post("/api/jobs-internal/grid-search").mock(
            return_value=HttpxResponse(202, json={"job_id": "job-fwd", "search_id": "search-runtime", "status": "queued"})
        )
        async with client as c:
            launched = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": "job-fwd"})

    assert launched.status_code == 202, launched.text
    assert launched.json() == {"job_id": "job-fwd", "search_id": "search-runtime", "status": "queued"}
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["X-Data-Plane-Control-Secret"] == "sec", "the authenticated internal boundary"


async def test_a_live_redelivery_joins_the_first_worker_and_never_claims_twice(client, lake, monkeypatch) -> None:
    _requires_ephemeral_db()
    job = f"job-redelivery-{id(monkeypatch)}"
    runs: list[str] = []
    stamps: list[str] = []

    def fake_run_in_thread(job_id, work, **kwargs):
        runs.append(job_id)

    async def spy_stamp(*, job_id, kind, record_id):
        stamps.append(job_id)

    monkeypatch.setattr(grid_search_router, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(grid_search_router.records, "stamp_research_dispatch", spy_stamp)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)

    async with client as c:
        first = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": job})
        assert first.status_code == 202, first.text
        search_id = first.json()["search_id"]

        redelivered = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": job})

    assert redelivered.status_code == 202
    assert redelivered.json() == {"job_id": job, "search_id": search_id, "status": "queued"}
    assert runs == [job], "exactly one worker, however often the transport retries"
    assert stamps == [job], "the redelivery neither stamps nor binds again"


async def test_a_dead_redelivery_is_refused_never_replayed_under_its_old_id(client, lake, monkeypatch) -> None:
    _requires_ephemeral_db()
    job = f"job-dead-redelivery-{id(monkeypatch)}"
    monkeypatch.setattr(grid_search_router, "run_in_thread", lambda job_id, work, **kwargs: None)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)

    # One `async with`: a closed httpx client cannot be reopened.
    async with client as c:
        first = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": job})
        assert first.status_code == 202, first.text

        monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: False)
        replay = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": job})

    assert replay.status_code == 409
    assert "cannot be redelivered" in replay.json()["detail"]


async def test_a_runtime_dispatch_runs_cells_through_the_gated_worker(client, lake, monkeypatch) -> None:
    """The runtime executes the search under the checkpoint lease contract, cells through the dispatch (#3049)."""
    _requires_ephemeral_db()
    import asyncio
    from types import SimpleNamespace

    import app.jobs.progress as progress_module
    from app.research.grid_search import dispatch as dispatch_module
    from app.research.persistence import ownership
    from tests.jobs.conftest import _FakeRedis

    job = f"job-runtime-{id(monkeypatch)}"
    built: dict[str, object] = {}
    fake_redis = _FakeRedis()

    async def no_stamp(*, job_id, kind, record_id):
        # The stamp needs a real authority connection; test_ownership owns it.
        return None

    monkeypatch.setattr(progress_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(ownership, "process_authority", lambda: SimpleNamespace(boot_generation=1, connected=True))
    monkeypatch.setattr(grid_search_router.records, "stamp_research_dispatch", no_stamp)
    monkeypatch.setenv("RESEARCH_RUNTIME", "1")

    def fake_runtime_cell_dispatch(row, spec, *, attempt, job_id, on_checkpoint, cancel_check, on_log=None, on_waiting=None, pool=None):
        built["attempt"] = attempt
        built["cancel_check"] = cancel_check

        class _Dispatch:
            def __call__(self, cells):
                # One child-shaped checkpoint rides the relay the gate trusts,
                # then each cell completes.
                on_checkpoint({"phase": "search", "cursor_kind": "durable_completions", "cursor": 1})
                for candidate in cells:
                    yield candidate, CellResult(
                        params_hash=candidate.params_hash,
                        params=dict(candidate.params),
                        status="completed",
                        total_trades=2,
                        sharpe_ratio=float(candidate.params["short_window"]),
                    )

            def cancel(self) -> None:
                pass

            def close(self) -> None:
                pass

        return _Dispatch()

    monkeypatch.setattr(dispatch_module, "runtime_cell_dispatch", fake_runtime_cell_dispatch)

    def fake_run_in_thread(job_id, work, **kwargs):
        built["job_id"] = job_id
        built["emit_renews_lease"] = kwargs.get("emit_renews_lease")

        class _Emit:
            def phase(self, phase):
                pass

            def progress(self, done, total, unit=None):
                pass

            def log(self, message):
                pass

        class _Cancel:
            def raise_if_cancelled(self):
                return None

        built["outcome"] = asyncio.to_thread(work, _Emit(), _Cancel())

    monkeypatch.setattr(grid_search_router, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)

    async with client as c:
        launched = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": job})
        assert launched.status_code == 202, launched.text
        outcome = await built["outcome"]
        assert outcome["status"] == "completed" and outcome["completed_cells"] == 2
        assert outcome["leader_params_hash"] is not None

    assert built["emit_renews_lease"] is False, "in the runtime, only accepted checkpoints renew the lease (#3041)"
    assert built["attempt"] == 1
    assert fake_redis.expires, "the gated run renewed the lease through accepted checkpoints"


async def test_a_live_resume_redelivery_joins_the_first_worker_before_any_refusal(client, lake, monkeypatch) -> None:
    """A transport retry of a live Finish joins the first worker instead of reading NOT_RESUMABLE (#3049)."""
    _requires_ephemeral_db()
    job = f"job-resume-redelivery-{id(monkeypatch)}"
    runs: list[str] = []

    monkeypatch.setattr(grid_search_router, "run_in_thread", lambda job_id, work, **kwargs: runs.append(job_id))
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)

    async with client as c:
        first = await c.post("/api/jobs-internal/grid-search", json={**_body(), "jobId": job})
        search_id = first.json()["search_id"]

        # A Finish carries the stored spec beside the id, as the Frontend sends it.
        redelivered = await c.post(
            "/api/jobs-internal/grid-search", json={**_body(), "jobId": job, "resumeSearchId": search_id}
        )

    assert redelivered.status_code == 202, redelivered.text
    assert redelivered.json() == {"job_id": job, "search_id": search_id, "status": "queued"}
    assert runs == [job], "the Finish redelivery claimed no second attempt"
