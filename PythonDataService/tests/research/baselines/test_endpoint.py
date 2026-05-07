"""HTTP-boundary tests for ``/api/research/strategy-runs/baselines``."""

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
    return ledger.run_id


@pytest.fixture
async def client(configured_app):
    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------
async def test_post_buy_and_hold_creates_persisted_baseline(
    client, parent_run_id, tmp_path: Path
):
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "buy_and_hold",
            "sample_count": 1,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    bid = body["config"]["baseline_id"]
    assert body["result"]["status"] == "completed"
    assert (tmp_path / "baselines" / bid / "config.json").is_file()


async def test_post_random_ema_runs_n_baselines(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "random_ema_windows",
            "sample_count": 5,
            "random_seed": 42,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["result"]["baselines"]) == 5


async def test_post_then_get_round_trips(client, parent_run_id):
    posted = (
        await client.post(
            "/api/research/strategy-runs/baselines",
            json={
                "parent_run_id": parent_run_id,
                "method": "buy_and_hold",
                "sample_count": 1,
            },
        )
    ).json()
    bid = posted["config"]["baseline_id"]

    fetched = await client.get(f"/api/research/strategy-runs/baselines/{bid}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["config"] == posted["config"]


# ---------------------------------------------------------------------------
# Validation errors.
# ---------------------------------------------------------------------------
async def test_post_unknown_method_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "not_a_method",
            "sample_count": 1,
        },
    )
    assert response.status_code == 422


async def test_post_negative_seed_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "buy_and_hold",
            "sample_count": 1,
            "random_seed": -1,
        },
    )
    assert response.status_code == 422


async def test_post_sample_count_above_cap_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "buy_and_hold",
            "sample_count": 1000,
        },
    )
    assert response.status_code == 422


async def test_post_omitted_sample_count_defaults_per_method(client, parent_run_id):
    """Regression: ``buy_and_hold`` defaults to 1 (not 30).

    The flat default of 30 used to apply to every method. Buy-and-
    hold is deterministic and parameter-free, so 30 reps just
    duplicate work and inflate ``N`` in the small-sample p-value's
    ``(1 + count) / (N + 1)`` denominator. ``random_ema_windows``
    keeps its 30-default since 30 random pairs give a stable null
    distribution.
    """
    bh_response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "buy_and_hold",
            # sample_count omitted on purpose.
        },
    )
    assert bh_response.status_code == 200, bh_response.text
    bh_body = bh_response.json()
    assert bh_body["config"]["sample_count"] == 1
    assert len(bh_body["result"]["baselines"]) == 1

    rema_response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "random_ema_windows",
            # sample_count omitted on purpose.
        },
    )
    assert rema_response.status_code == 200, rema_response.text
    rema_body = rema_response.json()
    assert rema_body["config"]["sample_count"] == 30


async def test_post_zero_sample_count_returns_422(client, parent_run_id):
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "buy_and_hold",
            "sample_count": 0,
        },
    )
    assert response.status_code == 422


async def test_post_missing_parent_run_persists_failed_baseline(
    client, tmp_path: Path
):
    """Same first-class-failure pattern as Phase A/C/D."""
    response = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": "deadbeefdeadbeefdeadbeefdeadbeef",
            "method": "buy_and_hold",
            "sample_count": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["status"] == "failed"
    assert "parent run not found" in (body["result"]["failure_reason"] or "")
    bid = body["config"]["baseline_id"]
    assert (tmp_path / "baselines" / bid / "config.json").is_file()


# ---------------------------------------------------------------------------
# GET single + 404.
# ---------------------------------------------------------------------------
async def test_get_missing_returns_404(client):
    response = await client.get(
        "/api/research/strategy-runs/baselines/" + "f" * 32
    )
    assert response.status_code == 404


async def test_get_path_traversal_id_returns_400(client):
    response = await client.get(
        "/api/research/strategy-runs/baselines/..%2Fetc%2Fpasswd"
    )
    assert response.status_code in {400, 404}


# ---------------------------------------------------------------------------
# Listing.
# ---------------------------------------------------------------------------
async def test_list_empty(client):
    response = await client.get("/api/research/strategy-runs/baselines")
    assert response.status_code == 200
    assert response.json() == {"baselines": []}


async def test_list_filter_by_parent_run_id(client, parent_run_id):
    a = await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": parent_run_id,
            "method": "buy_and_hold",
            "sample_count": 1,
        },
    )
    # Failed baseline against a different parent (still persisted).
    await client.post(
        "/api/research/strategy-runs/baselines",
        json={
            "parent_run_id": "deadbeefdeadbeefdeadbeefdeadbeef",
            "method": "buy_and_hold",
            "sample_count": 1,
        },
    )

    response = await client.get(
        "/api/research/strategy-runs/baselines",
        params={"parent_run_id": parent_run_id},
    )
    items = response.json()["baselines"]
    assert [c["baseline_id"] for c in items] == [
        a.json()["config"]["baseline_id"]
    ]


# ---------------------------------------------------------------------------
# Path-resolution check.
# ---------------------------------------------------------------------------
async def test_baselines_path_does_not_clash_with_run_id_route(client):
    response = await client.get("/api/research/strategy-runs/baselines")
    assert response.status_code == 200
    assert "baselines" in response.json()
