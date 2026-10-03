"""HTTP-boundary tests for ``/api/research/strategy-runs``.

Uses ``httpx.AsyncClient`` over ``ASGITransport`` per the project's
testing rules — ``TestClient`` is for sync routes, and we want the
same transport pytest uses for the rest of the FastAPI suite.

The data-source dependency is overridden to inject a synthetic
``FakeDataReader`` so the endpoint runs hermetic in milliseconds.
The artifacts-root dependency is overridden to a per-test ``tmp_path``
so two tests don't trample each other's persisted runs.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.main import app
from app.routers.research_runs import (
    get_artifacts_root,
    get_data_source_factory,
)


def _spec_dict(*, fast_period: int = 5, slow_period: int = 10) -> dict:
    return {
        "schema_version": "1.0",
        "name": "TEST EMA crossover",
        "symbols": ["TEST"],
        "resolution": {"period_minutes": 15},
        "indicators": [
            {"id": "fast", "kind": "EMA", "period": fast_period, "source": "close"},
            {"id": "slow", "kind": "EMA", "period": slow_period, "source": "close"},
            {"id": "rsi", "kind": "RSI", "period": 14, "source": "close", "ma_type": "wilders"},
        ],
        "entry": {
            "logic": "AND",
            "conditions": [
                {"kind": "FreshCross", "left": "fast", "right": "slow", "direction": "up"},
                {
                    "kind": "IndicatorComparison",
                    "left": {
                        "kind": "Subtract",
                        "left": {"kind": "IndicatorRef", "indicator": "fast"},
                        "right": {"kind": "IndicatorRef", "indicator": "slow"},
                    },
                    "op": ">=",
                    "right": {"kind": "Const", "value": 0.20},
                },
                {"kind": "IndicatorBetween", "indicator": "rsi", "lo": 50, "hi": 70, "inclusive": True},
            ],
            "size": {"kind": "SetHoldings", "fraction": 1.0},
            "pyramiding": 1,
        },
        "position": {"kind": "EQUITY_LONG"},
        "survival": [],
        "exit": {
            "logic": "OR",
            "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 5}],
        },
        "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
    }


def _request_body(spec: dict | None = None) -> dict:
    return {
        "spec": spec or _spec_dict(),
        "start_date": "2024-01-02",
        "end_date": "2024-12-31",
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
    }


@pytest.fixture
def configured_app(tmp_path: Path):
    """Override the data-source and artifacts-root dependencies for the test.

    Yields the FastAPI ``app`` ready for ``ASGITransport`` use; cleans
    up overrides on teardown so other tests in the same session aren't
    polluted.
    """
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def fake_factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    def fake_root() -> Path:
        return tmp_path

    app.dependency_overrides[get_data_source_factory] = lambda: fake_factory
    app.dependency_overrides[get_artifacts_root] = fake_root
    yield app
    app.dependency_overrides.pop(get_data_source_factory, None)
    app.dependency_overrides.pop(get_artifacts_root, None)


@pytest.fixture
async def client(configured_app):
    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# POST — happy path.
# ---------------------------------------------------------------------------
async def test_post_creates_persisted_run(client, tmp_path: Path):
    response = await client.post("/api/research/strategy-runs", json=_request_body())
    assert response.status_code == 200, response.text

    body = response.json()
    assert "ledger" in body and "result" in body

    ledger = body["ledger"]
    result = body["result"]
    assert ledger["status"] == "completed"
    assert ledger["run_id"] == result["run_id"]
    assert ledger["symbol"] == "TEST"
    assert ledger["strategy_spec_hash"]  # non-empty
    assert ledger["result_hash"]
    assert ledger["trade_log_hash"]
    assert ledger["metrics_hash"]

    # Persisted to the tmp artifacts root.
    run_dir = tmp_path / ledger["run_id"]
    assert (run_dir / "ledger.json").is_file()
    assert (run_dir / "result.json").is_file()


# ---------------------------------------------------------------------------
# POST — validation errors.
# ---------------------------------------------------------------------------
async def test_post_invalid_date_returns_400(client):
    body = _request_body()
    body["start_date"] = "not-a-date"
    response = await client.post("/api/research/strategy-runs", json=body)
    assert response.status_code == 400
    assert "start_date" in response.json()["detail"]


@pytest.mark.parametrize("fill_mode", ["signal_bar_close", "next_bar_open", "decision_minute_open"])
async def test_post_runs_under_every_fill_mode_a_research_request_may_name(client, fill_mode: str):
    body = _request_body()
    body["fill_mode"] = fill_mode
    response = await client.post("/api/research/strategy-runs", json=body)

    assert response.status_code == 200, response.text
    ledger = response.json()["ledger"]
    assert ledger["status"] == "completed"
    assert ledger["fill_mode"] == fill_mode


# ``next_session_open`` is a real mode the runner runs for parity callers; the route does not offer it.
# ``open`` is the engine backtest's short name, which names nothing among three "...open" modes.
@pytest.mark.parametrize("fill_mode", ["magic", "next_session_open", "open"])
async def test_post_invalid_fill_mode_returns_400(client, fill_mode: str):
    body = _request_body()
    body["fill_mode"] = fill_mode
    response = await client.post("/api/research/strategy-runs", json=body)
    assert response.status_code == 400
    assert "signal_bar_close, next_bar_open or decision_minute_open" in response.json()["detail"]


async def test_post_start_after_end_returns_400(client):
    body = _request_body()
    body["start_date"] = "2024-12-31"
    body["end_date"] = "2024-01-02"
    response = await client.post("/api/research/strategy-runs", json=body)
    assert response.status_code == 400


async def test_post_malformed_spec_returns_422(client):
    """Pydantic validation on the embedded spec runs before our endpoint —
    a structurally-broken spec gets FastAPI's 422, not our 400."""
    body = _request_body()
    body["spec"]["symbols"] = []  # Phase 1 boundary: single symbol required
    response = await client.post("/api/research/strategy-runs", json=body)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST — failed runs are first-class.
# ---------------------------------------------------------------------------
async def test_post_unsupported_spec_feature_persists_failed_run(
    configured_app, tmp_path: Path
):
    """A spec that uses Phase-2 features (multi-symbol via OPTION_TEMPLATE
    or pyramiding>1) is rejected by ``SpecAlgorithm.__init__``. The runner
    catches that and emits a failed-status ledger; the endpoint persists
    it and returns 200. Clients introspect ``ledger.status``.
    """
    spec = _spec_dict()
    spec["entry"]["pyramiding"] = 2  # evaluator rejects this in __init__

    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post("/api/research/strategy-runs", json=_request_body(spec))

    assert response.status_code == 200
    ledger = response.json()["ledger"]
    assert ledger["status"] == "failed"
    assert ledger["failure_reason"] is not None
    assert "pyramiding" in ledger["failure_reason"]

    # Failed runs are persisted alongside successful ones.
    assert (tmp_path / ledger["run_id"] / "ledger.json").is_file()


# ---------------------------------------------------------------------------
# GET — single run.
# ---------------------------------------------------------------------------
async def test_get_missing_run_returns_404(client):
    """Valid run_id format but no such run on disk → 404."""
    response = await client.get(
        "/api/research/strategy-runs/deadbeefdeadbeefdeadbeefdeadbeef"
    )
    assert response.status_code == 404


async def test_get_path_traversal_run_id_returns_400(client):
    """Path traversal attempt is rejected by ``_run_dir`` and surfaces as 400.

    The artifacts root never sees a directory matching ``../etc/passwd``;
    the storage layer's regex rejects the run_id before any path
    concatenation. This test guards the defense at the HTTP boundary.
    """
    response = await client.get("/api/research/strategy-runs/..%2Fetc%2Fpasswd")
    # FastAPI / Starlette decode %2F so the handler sees ``../etc/passwd``;
    # the storage validation raises ValueError → translated to 400.
    assert response.status_code in {400, 404}, response.text
    if response.status_code == 400:
        assert "run_id" in response.json()["detail"]


async def test_post_run_id_collision_returns_409(configured_app, tmp_path: Path, monkeypatch):
    """UUID4 collision (astronomically unlikely but explicitly handled).

    Forces two runs to share a ``run_id`` by monkey-patching ``uuid4``
    to a constant, then asserts the second POST gets 409 Conflict — not
    a generic 500. The contract should be readable from the client.
    """
    import uuid

    fixed = uuid.UUID("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setattr("app.research.runs.runner.uuid.uuid4", lambda: fixed)

    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        first = await c.post("/api/research/strategy-runs", json=_request_body())
        assert first.status_code == 200
        second = await c.post("/api/research/strategy-runs", json=_request_body())

    assert second.status_code == 409, second.text
    assert "collision" in second.json()["detail"]


# ---------------------------------------------------------------------------
# GET — listing.
# ---------------------------------------------------------------------------
async def test_list_empty_returns_empty_array(client):
    response = await client.get("/api/research/strategy-runs")
    assert response.status_code == 200
    assert response.json() == {"runs": []}


async def test_list_returns_recent_first(client):
    # Fire two POSTs; the second one should sort first.
    await client.post("/api/research/strategy-runs", json=_request_body())
    await client.post("/api/research/strategy-runs", json=_request_body())

    response = await client.get("/api/research/strategy-runs")
    runs = response.json()["runs"]
    assert len(runs) == 2
    assert runs[0]["created_at_ms"] >= runs[1]["created_at_ms"]


async def test_list_filter_by_parent_run_id(client):
    parent = await client.post("/api/research/strategy-runs", json=_request_body())
    parent_id = parent.json()["ledger"]["run_id"]

    body = _request_body()
    body["parent_run_id"] = parent_id
    child = await client.post("/api/research/strategy-runs", json=body)

    response = await client.get(
        "/api/research/strategy-runs", params={"parent_run_id": parent_id}
    )
    children = response.json()["runs"]
    assert [r["run_id"] for r in children] == [child.json()["ledger"]["run_id"]]


async def test_list_limit_truncates(client):
    for _ in range(3):
        await client.post("/api/research/strategy-runs", json=_request_body())

    response = await client.get("/api/research/strategy-runs", params={"limit": 2})
    assert len(response.json()["runs"]) == 2


# ---------------------------------------------------------------------------
# RunLedger.window_summary — stamped on every persisted run.
# ---------------------------------------------------------------------------
async def test_post_ledger_carries_window_summary(client):
    response = await client.post("/api/research/strategy-runs", json=_request_body())
    assert response.status_code == 200, response.text

    ledger = response.json()["ledger"]
    ws = ledger["window_summary"]
    assert ws is not None
    assert ws["requested_start_date"] == "2024-01-02"
    assert ws["requested_end_date"] == "2024-12-31"
    assert isinstance(ws["sessions_included"], list)
    # NYSE 2024 ran 252 trading sessions; ``[2024-01-02, 2024-12-31]``
    # inclusive covers every one of them (1/1 New Year's is the only
    # 2024 holiday before 1/2, and 12/31 is a regular session). Pin
    # the count + endpoints — wide ranges hide off-by-one bugs in the
    # exclusive-vs-inclusive end-date semantics this PR fixes.
    assert len(ws["sessions_included"]) == 252
    assert ws["sessions_included"][0] == "2024-01-02"
    assert ws["sessions_included"][-1] == "2024-12-31"


async def test_window_summary_round_trips_through_persist_load(client):
    """The ledger field survives save → load (JSON serialization round-trip)."""
    post = await client.post("/api/research/strategy-runs", json=_request_body())
    run_id = post.json()["ledger"]["run_id"]

    got = await client.get(f"/api/research/strategy-runs/{run_id}")
    assert got.status_code == 200, got.text
    assert got.json()["ledger"]["window_summary"] == post.json()["ledger"]["window_summary"]
