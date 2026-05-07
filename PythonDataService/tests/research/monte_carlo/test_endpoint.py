"""HTTP-boundary tests for ``/api/research/strategy-runs/monte-carlo``.

Same pattern as Phase A/C endpoint suites — `httpx` over
`ASGITransport`, dependency overrides for the data source and
artifacts root. The artifacts-root override forces every test to use
its own ``tmp_path``, which is also where the parent-run fixture
persists its run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.main import app
from app.research.runs import RunRequest, run_strategy_spec, save_run
from app.routers.research_runs import (
    get_artifacts_root,
    get_data_source_factory,
)


def _build_test_spec() -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": 5, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": 10, "source": "close"},
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
    )


@pytest.fixture
def configured_app(tmp_path: Path):
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    def root() -> Path:
        return tmp_path

    app.dependency_overrides[get_data_source_factory] = lambda: factory
    app.dependency_overrides[get_artifacts_root] = root
    yield app
    app.dependency_overrides.pop(get_data_source_factory, None)
    app.dependency_overrides.pop(get_artifacts_root, None)


@pytest.fixture
def parent_run_id(configured_app, tmp_path: Path):
    """Persist a real parent run under tmp_path; yield its run_id."""
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    ledger, result = run_strategy_spec(
        RunRequest(
            spec=_build_test_spec(),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 12, 31),
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
    )
    save_run(ledger, result, root=tmp_path)
    if not result.trades:
        pytest.skip("synthetic series produced zero trades on parent run")
    return ledger.run_id


@pytest.fixture
async def client(configured_app):
    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------
async def test_post_reshuffle_creates_persisted_monte_carlo(
    client, parent_run_id, tmp_path: Path
):
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 200,
            "random_seed": 42,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "config" in body and "result" in body

    mc_id = body["config"]["monte_carlo_id"]
    assert body["result"]["status"] == "completed"
    assert body["result"]["simulation_count"] == 200
    assert len(body["result"]["equity_bands"]) > 0

    # Persisted under tmp_path/monte-carlo/<mc_id>/.
    assert (tmp_path / "monte-carlo" / mc_id / "config.json").is_file()
    assert (tmp_path / "monte-carlo" / mc_id / "result.json").is_file()


async def test_post_resample_with_breach_thresholds(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "resample",
            "simulation_count": 200,
            "random_seed": 7,
            "breach_thresholds": [0.05, 0.10, 0.20],
        },
    )
    assert response.status_code == 200, response.text
    breaches = response.json()["result"]["breach_probabilities"]
    assert [b["threshold"] for b in breaches] == [0.05, 0.10, 0.20]
    for b in breaches:
        assert 0.0 <= b["probability"] <= 1.0


async def test_post_then_get_round_trips(client, parent_run_id):
    posted = (
        await client.post(
            "/api/research/strategy-runs/monte-carlo",
            json={
                "parent_run_id": parent_run_id,
                "method": "reshuffle",
                "simulation_count": 100,
            },
        )
    ).json()
    mc_id = posted["config"]["monte_carlo_id"]

    fetched = await client.get(f"/api/research/strategy-runs/monte-carlo/{mc_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["config"] == posted["config"]


async def test_response_timestamps_are_int64_ms(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 100,
        },
    )
    body = response.json()
    assert isinstance(body["config"]["created_at_ms"], int)
    assert isinstance(body["result"]["created_at_ms"], int)
    assert isinstance(body["result"]["completed_at_ms"], int)


# ---------------------------------------------------------------------------
# Validation errors.
# ---------------------------------------------------------------------------
async def test_post_unknown_method_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "not_a_method",
            "simulation_count": 100,
        },
    )
    assert response.status_code == 422


async def test_post_zero_simulation_count_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 0,
        },
    )
    assert response.status_code == 422


async def test_post_negative_random_seed_returns_422(client, parent_run_id):
    """Pydantic ``Field(ge=0)`` rejects negative seeds at the wire
    boundary before they reach ``numpy.random.default_rng`` (which
    would raise ``ValueError`` and surface as 500). Regression for
    PR #112 Codex P1.
    """
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 100,
            "random_seed": -1,
        },
    )
    assert response.status_code == 422


async def test_post_simulation_count_above_cap_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 100_000,  # above the 10k cap
        },
    )
    assert response.status_code == 422


async def test_post_missing_parent_run_persists_failed_mc(
    client, configured_app, tmp_path: Path
):
    """A non-existent parent_run_id is a *failed* MC, not a 4xx — the
    runner persists the failure for discoverability (Phase A's
    failed-runs-are-first-class contract carried forward).
    """
    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/api/research/strategy-runs/monte-carlo",
            json={
                "parent_run_id": "deadbeefdeadbeefdeadbeefdeadbeef",
                "method": "reshuffle",
                "simulation_count": 100,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["status"] == "failed"
    assert "parent run not found" in (body["result"]["failure_reason"] or "")
    # Persisted alongside successful runs.
    mc_id = body["config"]["monte_carlo_id"]
    assert (tmp_path / "monte-carlo" / mc_id / "config.json").is_file()


# ---------------------------------------------------------------------------
# GET single + 404.
# ---------------------------------------------------------------------------
async def test_get_missing_returns_404(client):
    response = await client.get(
        "/api/research/strategy-runs/monte-carlo/" + "f" * 32
    )
    assert response.status_code == 404


async def test_get_path_traversal_id_returns_400(client):
    response = await client.get(
        "/api/research/strategy-runs/monte-carlo/..%2Fetc%2Fpasswd"
    )
    assert response.status_code in {400, 404}, response.text


# ---------------------------------------------------------------------------
# Listing + filters.
# ---------------------------------------------------------------------------
async def test_list_empty(client):
    response = await client.get("/api/research/strategy-runs/monte-carlo")
    assert response.status_code == 200
    assert response.json() == {"monte_carlos": []}


async def test_list_filter_by_parent_run_id(client, parent_run_id):
    a = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 50,
        },
    )
    # An MC against a different parent (failed but persisted, has its own mc_id).
    await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": "deadbeefdeadbeefdeadbeefdeadbeef",
            "method": "reshuffle",
            "simulation_count": 50,
        },
    )

    response = await client.get(
        "/api/research/strategy-runs/monte-carlo",
        params={"parent_run_id": parent_run_id},
    )
    items = response.json()["monte_carlos"]
    assert [c["monte_carlo_id"] for c in items] == [
        a.json()["config"]["monte_carlo_id"]
    ]


async def test_list_filter_by_method(client, parent_run_id):
    rs = await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "reshuffle",
            "simulation_count": 50,
        },
    )
    await client.post(
        "/api/research/strategy-runs/monte-carlo",
        json={
            "parent_run_id": parent_run_id,
            "method": "resample",
            "simulation_count": 50,
        },
    )

    response = await client.get(
        "/api/research/strategy-runs/monte-carlo",
        params={"method": "reshuffle"},
    )
    items = response.json()["monte_carlos"]
    assert [c["monte_carlo_id"] for c in items] == [
        rs.json()["config"]["monte_carlo_id"]
    ]


# ---------------------------------------------------------------------------
# Path-resolution check: literal /monte-carlo beats /{run_id}.
# ---------------------------------------------------------------------------
async def test_monte_carlo_path_does_not_clash_with_run_id_route(client):
    response = await client.get("/api/research/strategy-runs/monte-carlo")
    assert response.status_code == 200
    assert "monte_carlos" in response.json()
