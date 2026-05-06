"""HTTP-boundary tests for ``/api/research/strategy-runs/walk-forward``.

Same testing pattern as Phase A's ``test_endpoint.py`` — `httpx`
over `ASGITransport`, dependency overrides for the data source and
artifacts root.
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


def _spec_dict() -> dict:
    return {
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


def _request_body(split_policy: dict, **overrides) -> dict:
    body = {
        "spec": _spec_dict(),
        "start_date": "2024-01-02",
        "end_date": "2024-02-22",
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
        "split_policy": split_policy,
    }
    body.update(overrides)
    return body


@pytest.fixture
def configured_app(tmp_path: Path):
    bars = build_minute_bars(closes_for_spy_ema(5000))

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
async def client(configured_app):
    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------
async def test_post_chronological_creates_persisted_walk_forward(
    client, tmp_path: Path
):
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.6})
    response = await client.post(
        "/api/research/strategy-runs/walk-forward", json=body
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "config" in payload and "result" in payload

    wf_id = payload["config"]["walk_forward_id"]
    assert payload["result"]["status"] == "completed"
    assert len(payload["result"]["folds"]) == 1

    # Persisted under tmp_path/walk-forward/<wf_id>/.
    assert (tmp_path / "walk-forward" / wf_id / "config.json").is_file()
    assert (tmp_path / "walk-forward" / wf_id / "result.json").is_file()


async def test_post_rolling_creates_multiple_folds(client):
    body = _request_body(
        split_policy={
            "kind": "rolling",
            "train_days": 10,
            "test_days": 5,
            "step_days": 5,
        }
    )
    response = await client.post(
        "/api/research/strategy-runs/walk-forward", json=body
    )
    assert response.status_code == 200, response.text
    folds = response.json()["result"]["folds"]
    assert len(folds) >= 5


async def test_post_then_get_round_trips(client):
    body = _request_body(split_policy={"kind": "chronological"})
    posted = (
        await client.post("/api/research/strategy-runs/walk-forward", json=body)
    ).json()
    wf_id = posted["config"]["walk_forward_id"]

    fetched = await client.get(f"/api/research/strategy-runs/walk-forward/{wf_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["config"] == posted["config"]


async def test_response_timestamps_are_int64_ms(client):
    body = _request_body(split_policy={"kind": "chronological"})
    response = await client.post(
        "/api/research/strategy-runs/walk-forward", json=body
    )
    payload = response.json()
    assert isinstance(payload["config"]["start_ms"], int)
    assert isinstance(payload["config"]["end_ms"], int)
    assert isinstance(payload["config"]["created_at_ms"], int)
    assert isinstance(payload["result"]["created_at_ms"], int)
    for fold in payload["result"]["folds"]:
        assert isinstance(fold["train_start_ms"], int)
        assert isinstance(fold["test_end_ms"], int)


# ---------------------------------------------------------------------------
# Validation errors.
# ---------------------------------------------------------------------------
async def test_post_invalid_date_returns_400(client):
    body = _request_body(split_policy={"kind": "chronological"})
    body["start_date"] = "not-a-date"
    response = await client.post(
        "/api/research/strategy-runs/walk-forward", json=body
    )
    assert response.status_code == 400


async def test_post_unknown_split_kind_returns_400(client):
    body = _request_body(split_policy={"kind": "totally_made_up"})
    response = await client.post(
        "/api/research/strategy-runs/walk-forward", json=body
    )
    # Pydantic 422 — the SplitPolicySpec literal-field validation
    # rejects unknown kinds before the route body runs.
    assert response.status_code == 422, response.text


async def test_post_window_too_short_persists_failed_walk_forward(
    client, tmp_path: Path
):
    body = _request_body(
        split_policy={
            "kind": "rolling",
            "train_days": 30,
            "test_days": 15,
            "step_days": 7,
        },
    )
    body["end_date"] = "2024-01-05"  # 3-day window can't fit a 30+15-day fold

    response = await client.post(
        "/api/research/strategy-runs/walk-forward", json=body
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["folds"] == []
    assert "too short" in (payload["result"]["failure_reason"] or "")
    # Failed WFs are first-class — persisted so they appear in listings.
    wf_id = payload["config"]["walk_forward_id"]
    assert (tmp_path / "walk-forward" / wf_id / "config.json").is_file()


# ---------------------------------------------------------------------------
# GET single + 404.
# ---------------------------------------------------------------------------
async def test_get_missing_walk_forward_returns_404(client):
    response = await client.get(
        "/api/research/strategy-runs/walk-forward/" + "f" * 32
    )
    assert response.status_code == 404


async def test_get_path_traversal_id_returns_400(client):
    response = await client.get(
        "/api/research/strategy-runs/walk-forward/..%2Fetc%2Fpasswd"
    )
    assert response.status_code in {400, 404}, response.text


# ---------------------------------------------------------------------------
# Listing + filters.
# ---------------------------------------------------------------------------
async def test_list_empty_returns_empty(client):
    response = await client.get("/api/research/strategy-runs/walk-forward")
    assert response.status_code == 200
    assert response.json() == {"walk_forwards": []}


async def test_list_returns_recent_first(client):
    body = _request_body(split_policy={"kind": "chronological"})
    await client.post("/api/research/strategy-runs/walk-forward", json=body)
    await client.post("/api/research/strategy-runs/walk-forward", json=body)

    response = await client.get("/api/research/strategy-runs/walk-forward")
    items = response.json()["walk_forwards"]
    assert len(items) == 2
    assert items[0]["created_at_ms"] >= items[1]["created_at_ms"]


async def test_list_filter_by_parent_run_id(client):
    parent_id = "p" * 32  # informational; not validated against an actual run
    a = await client.post(
        "/api/research/strategy-runs/walk-forward",
        json=_request_body(
            split_policy={"kind": "chronological"}, parent_run_id=parent_id
        ),
    )
    await client.post(
        "/api/research/strategy-runs/walk-forward",
        json=_request_body(split_policy={"kind": "chronological"}),
    )

    response = await client.get(
        "/api/research/strategy-runs/walk-forward",
        params={"parent_run_id": parent_id},
    )
    assert response.status_code == 200
    items = response.json()["walk_forwards"]
    assert [c["walk_forward_id"] for c in items] == [
        a.json()["config"]["walk_forward_id"]
    ]


# ---------------------------------------------------------------------------
# Path-resolution check: literal /walk-forward beats /{run_id} on the parent.
# ---------------------------------------------------------------------------
async def test_walk_forward_path_does_not_clash_with_run_id_route(client):
    """``GET /api/research/strategy-runs/walk-forward`` must hit the
    walk-forward listing endpoint, not be parsed as ``run_id=walk-forward``
    on the parent ``research_runs`` router.
    """
    response = await client.get("/api/research/strategy-runs/walk-forward")
    # If the parent route had won, ``walk-forward`` wouldn't match the
    # 32-char hex regex → 400. The walk-forward listing returns 200
    # with a ``walk_forwards`` envelope.
    assert response.status_code == 200
    assert "walk_forwards" in response.json()
