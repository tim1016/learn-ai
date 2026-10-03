"""HTTP-boundary tests for ``/api/research/strategy-runs/walk-forward``.

Same testing pattern as Phase A's ``test_endpoint.py`` — `httpx`
over `ASGITransport`, dependency overrides for the data source and
artifacts root.
"""

from __future__ import annotations

import json
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
from app.research.walk_forward.splits import date_str_to_ms
from app.routers.research_runs import (
    get_artifacts_root,
    get_data_source_factory,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS


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
        "start_ms": date_str_to_ms("2024-01-02"),
        "end_ms": date_str_to_ms("2024-02-22"),
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
async def test_post_chronological_creates_persisted_walk_forward(client, tmp_path: Path):
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.6})
    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "config" in payload and "result" in payload

    wf_id = payload["config"]["walk_forward_id"]
    assert payload["result"]["status"] == "completed"
    assert len(payload["result"]["folds"]) == 1

    # Persisted under tmp_path/walk-forward/<wf_id>/.
    assert (tmp_path / "walk-forward" / wf_id / "config.json").is_file()
    assert (tmp_path / "walk-forward" / wf_id / "result.json").is_file()


async def test_post_accepts_integer_json_tokens_for_float_costs(client) -> None:
    body = _request_body(
        split_policy={"kind": "chronological", "train_pct": 0.6},
        initial_cash=100_000,
        commission_per_order=0,
        slippage_per_share=0,
    )

    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)

    assert response.status_code == 200, response.text


async def test_post_rolling_creates_multiple_folds(client):
    body = _request_body(
        split_policy={
            "kind": "rolling",
            "train_days": 10,
            "test_days": 5,
            "step_days": 5,
        }
    )
    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)
    assert response.status_code == 200, response.text
    folds = response.json()["result"]["folds"]
    assert len(folds) >= 5


async def test_post_with_parent_run_computes_oos_retention(client):
    parent = await client.post(
        "/api/research/strategy-runs",
        json={
            "spec": _spec_dict(),
            "start_date": "2024-01-02",
            "end_date": "2024-02-22",
            "initial_cash": 100_000.0,
            "fill_mode": "signal_bar_close",
            "commission_per_order": 0.0,
        },
    )
    parent_payload = parent.json()
    parent_id = parent_payload["ledger"]["run_id"]
    parent_sharpe = parent_payload["result"]["metrics"]["sharpe_ratio"]
    if parent_sharpe in (None, 0):
        pytest.skip("synthetic parent run produced no finite Sharpe")

    body = _request_body(
        split_policy={
            "kind": "rolling",
            "train_days": 10,
            "test_days": 5,
            "step_days": 5,
        },
        parent_run_id=parent_id,
    )
    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    if result["mean_oos_sharpe"] is None:
        pytest.skip("synthetic folds produced no finite mean OOS Sharpe")
    if parent_sharpe <= 0:
        # A non-positive parent Sharpe has no retention to report: the ratio
        # used to come back sign-flipped (PRD #1925 pre-existing correction).
        assert result["oos_retention"] is None
    else:
        assert result["oos_retention"] == pytest.approx(
            result["mean_oos_sharpe"] / parent_sharpe,
            abs=1e-12,
            rel=0,
        )


async def test_post_then_get_round_trips(client):
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.7})
    posted = (await client.post("/api/research/strategy-runs/walk-forward", json=body)).json()
    wf_id = posted["config"]["walk_forward_id"]

    fetched = await client.get(f"/api/research/strategy-runs/walk-forward/{wf_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["config"] == posted["config"]


# ---------------------------------------------------------------------------
# Validation errors.
# ---------------------------------------------------------------------------
async def test_post_invalid_timestamp_returns_422(client):
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.7})
    body["start_ms"] = "not-a-timestamp"
    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)
    assert response.status_code == 422


_WALK_FORWARD = "/api/research/strategy-runs/walk-forward"
_ENGINE_CHART = "/api/engine/chart"


def _engine_chart_body() -> dict:
    return {
        "strategy_name": "ema_crossover_signal",
        "parameters": {"symbol": "TEST"},
        "symbol": "TEST",
        "from_ms_utc": date_str_to_ms("2024-01-02"),
        "to_ms_utc": date_str_to_ms("2024-02-22"),
    }


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (_WALK_FORWARD, "start_ms"),
        (_WALK_FORWARD, "end_ms"),
        (_ENGINE_CHART, "from_ms_utc"),
        (_ENGINE_CHART, "to_ms_utc"),
    ],
)
async def test_post_over_ceiling_timestamp_returns_422(client, path: str, field: str):
    """An instant past ``MAX_TIMESTAMP_MS`` is refused at the schema boundary
    (ADR 0022 (g)), not carried into datetime conversion to overflow (#2771)."""
    if path == _WALK_FORWARD:
        body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.7})
    else:
        body = _engine_chart_body()
    body[field] = MAX_TIMESTAMP_MS + 1
    response = await client.post(path, json=body)
    assert response.status_code == 422, response.text


async def test_post_start_ms_at_ceiling_passes_the_schema(client):
    """``start_ms == MAX_TIMESTAMP_MS`` is admissible; the handler, not the
    schema, refuses it because no later ``end_ms`` exists."""
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.7})
    body["start_ms"] = MAX_TIMESTAMP_MS
    body["end_ms"] = MAX_TIMESTAMP_MS
    response = await client.post(_WALK_FORWARD, json=body)
    assert response.status_code == 400, response.text
    assert "strictly before" in response.json()["detail"]


async def test_post_end_ms_at_ceiling_is_a_400_not_a_500(client):
    """``end_ms == MAX_TIMESTAMP_MS`` passes the schema, but the trading
    calendar cannot schedule a window ending in 9999: a request the service
    cannot serve, refused as a 400 before any schedule is built (#2771)."""
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.7})
    body["end_ms"] = MAX_TIMESTAMP_MS
    response = await client.post(_WALK_FORWARD, json=body)
    assert response.status_code == 400, response.text
    assert "cannot schedule past 2262-04-10" in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        _WALK_FORWARD,
        "/api/research/strategy-runs",
        "/api/research/strategy-runs/baselines",
        "/api/research/strategy-runs/monte-carlo",
    ],
)
async def test_list_since_ms_admits_the_ceiling_and_refuses_past_it(client, path: str):
    at_ceiling = await client.get(path, params={"since_ms": MAX_TIMESTAMP_MS})
    assert at_ceiling.status_code == 200, at_ceiling.text
    past_ceiling = await client.get(path, params={"since_ms": MAX_TIMESTAMP_MS + 1})
    assert past_ceiling.status_code == 422, past_ceiling.text


async def _stored_fill_mode_and_fold_entries(client, fill_mode: str) -> tuple[str, list[float]]:
    """Run a walk-forward under ``fill_mode``; return the mode it stored and the prices its fold runs entered at."""
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.6}, fill_mode=fill_mode)
    response = await client.post(_WALK_FORWARD, json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"]["status"] == "completed"

    stored = (await client.get(f"{_WALK_FORWARD}/{payload['config']['walk_forward_id']}")).json()["config"]["fill_mode"]
    entries: list[float] = []
    for fold in payload["result"]["folds"]:
        run = (await client.get(f"/api/research/strategy-runs/{fold['test_run_id']}")).json()
        assert run["ledger"]["fill_mode"] == stored
        entries += [trade["entry_price"] for trade in run["result"]["trades"]]
    return stored, entries


async def test_post_fills_its_folds_under_the_fill_mode_the_request_names(client):
    """Each mode enters the same folds at its own price, so the runner did not ignore it (#2599)."""
    entries: dict[str, list[float]] = {}
    for fill_mode in ("signal_bar_close", "next_bar_open", "decision_minute_open"):
        stored, entries[fill_mode] = await _stored_fill_mode_and_fold_entries(client, fill_mode)
        assert stored == fill_mode

    assert all(entries.values())
    assert entries["decision_minute_open"] != entries["signal_bar_close"]
    assert entries["decision_minute_open"] != entries["next_bar_open"]
    assert entries["signal_bar_close"] != entries["next_bar_open"]


async def test_post_stores_the_canonical_fill_mode_not_the_spelling_the_request_used(client):
    stored, entries = await _stored_fill_mode_and_fold_entries(client, "Decision-Minute-Open")
    _, canonical_entries = await _stored_fill_mode_and_fold_entries(client, "decision_minute_open")

    assert stored == "decision_minute_open"
    assert entries == canonical_entries


# ``next_session_open`` is a real mode this route does not run; ``open`` is the
# engine backtest's short name, which names nothing among three "...open" modes.
@pytest.mark.parametrize("fill_mode", ["magic", "next_session_open", "open"])
async def test_post_unknown_fill_mode_returns_400_naming_the_modes(client, fill_mode: str):
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.6}, fill_mode=fill_mode)
    response = await client.post(_WALK_FORWARD, json=body)

    assert response.status_code == 400, response.text
    assert "signal_bar_close, next_bar_open or decision_minute_open" in response.json()["detail"]


async def test_post_unknown_split_kind_returns_400(client):
    body = _request_body(split_policy={"kind": "totally_made_up"})
    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)
    # Pydantic 422 — the SplitPolicySpec literal-field validation
    # rejects unknown kinds before the route body runs.
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "split_policy",
    [
        {"kind": "rolling"},
        {
            "kind": "rolling",
            "train_days": 10,
            "test_days": 5,
            "step_days": 5,
            "unexpected": 1,
        },
        {
            "kind": "rolling",
            "train_days": True,
            "test_days": 5,
            "step_days": 5,
        },
        {
            "kind": "rolling",
            "train_days": "10",
            "test_days": 5,
            "step_days": 5,
        },
    ],
)
async def test_post_rejects_incomplete_extra_or_coerced_split_fields(
    client,
    split_policy: dict,
):
    response = await client.post(
        "/api/research/strategy-runs/walk-forward",
        json=_request_body(split_policy=split_policy),
    )

    assert response.status_code == 422, response.text


async def test_post_rejects_non_finite_money(client):
    body = _request_body(
        split_policy={"kind": "chronological", "train_pct": 0.7},
        initial_cash=float("inf"),
    )
    response = await client.post(
        "/api/research/strategy-runs/walk-forward",
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


async def test_post_window_too_short_persists_failed_walk_forward(client, tmp_path: Path):
    body = _request_body(
        split_policy={
            "kind": "rolling",
            "train_days": 30,
            "test_days": 15,
            "step_days": 7,
        },
    )
    body["end_ms"] = date_str_to_ms("2024-01-05")  # 3 days cannot fit a 30+15-day fold

    response = await client.post("/api/research/strategy-runs/walk-forward", json=body)
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
    response = await client.get("/api/research/strategy-runs/walk-forward/" + "f" * 32)
    assert response.status_code == 404


async def test_get_path_traversal_id_returns_400(client):
    response = await client.get("/api/research/strategy-runs/walk-forward/..%2Fetc%2Fpasswd")
    assert response.status_code in {400, 404}, response.text


# ---------------------------------------------------------------------------
# Listing + filters.
# ---------------------------------------------------------------------------
async def test_list_empty_returns_empty(client):
    response = await client.get("/api/research/strategy-runs/walk-forward")
    assert response.status_code == 200
    assert response.json() == {"walk_forwards": []}


async def test_list_requires_complete_protocol_filter_identity(client):
    response = await client.get(
        "/api/research/strategy-runs/walk-forward",
        params={"protocol_id": "spy-ema-normalized-gap"},
    )

    assert response.status_code == 400
    assert "must be provided together" in response.json()["detail"]


async def test_list_returns_recent_first(client):
    body = _request_body(split_policy={"kind": "chronological", "train_pct": 0.7})
    await client.post("/api/research/strategy-runs/walk-forward", json=body)
    await client.post("/api/research/strategy-runs/walk-forward", json=body)

    response = await client.get("/api/research/strategy-runs/walk-forward")
    items = response.json()["walk_forwards"]
    assert len(items) == 2
    assert items[0]["created_at_ms"] >= items[1]["created_at_ms"]


async def test_list_filter_by_parent_run_id(client):
    parent = await client.post(
        "/api/research/strategy-runs",
        json={
            "spec": _spec_dict(),
            "start_date": "2024-01-02",
            "end_date": "2024-02-22",
            "initial_cash": 100_000.0,
            "fill_mode": "signal_bar_close",
            "commission_per_order": 0.0,
        },
    )
    assert parent.status_code == 200, parent.text
    parent_id = parent.json()["ledger"]["run_id"]
    a = await client.post(
        "/api/research/strategy-runs/walk-forward",
        json=_request_body(
            split_policy={"kind": "chronological", "train_pct": 0.7},
            parent_run_id=parent_id,
        ),
    )
    await client.post(
        "/api/research/strategy-runs/walk-forward",
        json=_request_body(split_policy={"kind": "chronological", "train_pct": 0.7}),
    )

    response = await client.get(
        "/api/research/strategy-runs/walk-forward",
        params={"parent_run_id": parent_id},
    )
    assert response.status_code == 200
    items = response.json()["walk_forwards"]
    assert [c["walk_forward_id"] for c in items] == [a.json()["config"]["walk_forward_id"]]


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


async def test_engine_chart_to_ms_at_ceiling_is_a_400_not_a_500(client):
    """``to_ms_utc == MAX_TIMESTAMP_MS`` passes the schema, but a window ending
    in 9999 cannot become a nanosecond timestamp: a request the service cannot
    serve, refused as a 400 rather than a raw overflow 500 (#2771)."""
    body = _engine_chart_body()
    body["to_ms_utc"] = MAX_TIMESTAMP_MS
    response = await client.post(_ENGINE_CHART, json=body)
    assert response.status_code == 400, response.text
