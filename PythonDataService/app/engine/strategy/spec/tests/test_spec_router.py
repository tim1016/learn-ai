"""Endpoint tests for the spec-strategy FastAPI router.

Uses ``httpx.AsyncClient`` with ``ASGITransport`` so the tests run
in-process against the FastAPI app without a live HTTP server. The
data-source dependency is overridden with a synthetic minute-bar
reader so tests don't need access to the LEAN data archive.

Coverage:
  * GET  /api/spec-strategy/fixtures lists the canonical fixtures on disk
  * GET  /api/spec-strategy/fixtures/sma_crossover round-trips through
    StrategySpec validation
  * POST /api/spec-strategy/backtest runs SMA spec on synthetic data and
    matches the in-process parity test's trade sequence
  * POST /api/spec-strategy/backtest returns 400 for malformed specs
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.strategy.spec.tests._parity_helpers import (
    SYMBOL,
    FakeDataReader,
    build_minute_bars,
    closes_for_sma,
)
from app.main import app
from app.routers.spec_strategy import get_data_source_factory

# ---------------------------------------------------------------------------
# Test client harness — override the data-source factory with a synthetic
# reader. The factory is invoked per-request inside the endpoint, so we
# need to override the dependency before the request runs.
# ---------------------------------------------------------------------------
_SMA_CLOSES_NUM_BARS = 800


def _make_synthetic_factory():
    """Build a (symbol, start, end) -> FakeDataReader factory.

    Symbol passed in by the request is overridden to match the
    synthetic stream's TEST symbol so the engine reads the right bars.
    """
    closes = closes_for_sma(_SMA_CLOSES_NUM_BARS)
    bars = build_minute_bars(closes)
    reader = FakeDataReader(bars=bars)

    def _factory(symbol: str, start, end):
        return reader

    return _factory


async def _client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------
async def test_fixtures_endpoints() -> None:
    async with await _client() as client:
        list_resp = await client.get("/api/spec-strategy/fixtures")
        detail_resp = await client.get("/api/spec-strategy/fixtures/sma_crossover")
        unknown_resp = await client.get("/api/spec-strategy/fixtures/does_not_exist")

    assert list_resp.status_code == 200, "fixtures list endpoint answers 200"
    items = list_resp.json()
    names = {item["name"] for item in items}
    assert names == {
        "spy_ema_crossover",
        "spy_ema_normalized_gap",
        "ema_crossover_2_bps",
        "sma_crossover",
        "rsi_mean_reversion",
        "deployment_validation",
    }, (
        f"fixtures list endpoint: unexpected fixture names: {names}"
    )
    by_name = {item["name"]: item for item in items}
    assert (
        by_name["spy_ema_crossover"]["path"]
        == "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json"
    ), "fixtures list endpoint: spy_ema_crossover names its spec path"

    assert detail_resp.status_code == 200, "fixture detail endpoint answers 200"
    spec = detail_resp.json()
    assert spec["name"].startswith("SMA"), "fixture detail endpoint: sma_crossover spec name starts with SMA"
    assert spec["symbols"] == ["SPY"], "fixture detail endpoint: sma_crossover spec trades SPY"

    assert unknown_resp.status_code == 404, "fixture detail endpoint: an unknown fixture returns 404"


async def test_backtest_runs_sma_spec_on_synthetic_data() -> None:
    """End-to-end: load canonical SMA fixture, override data source with
    a synthetic stream, POST to /backtest, and assert the trade count
    matches the in-process parity test's expectation."""
    app.dependency_overrides[get_data_source_factory] = _make_synthetic_factory

    try:
        async with await _client() as client:
            spec_resp = await client.get("/api/spec-strategy/fixtures/sma_crossover")
            assert spec_resp.status_code == 200
            spec_payload = spec_resp.json()
            # Override symbol to match the synthetic stream.
            spec_payload["symbols"] = [SYMBOL]

            resp = await client.post(
                "/api/spec-strategy/backtest",
                json={
                    "spec": spec_payload,
                    "start_date": "2024-01-02",
                    "end_date": "2024-12-31",
                    "initial_cash": 100000.0,
                    "fill_mode": "signal_bar_close",
                    "commission_per_order": 0.0,
                },
            )
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is True, body
    # The parity test's in-process run produced 11 trades on this exact
    # synthetic stream — the endpoint must reproduce that.
    assert body["total_trades"] == 11, f"expected 11 trades, got {body['total_trades']}"
    # Indicator snapshots present on each trade.
    assert all(t["indicators"] for t in body["trades"])

    # entry_time / exit_time on the wire are int64 ms UTC, not ISO
    # strings. Verify the wire shape and that the values are roughly
    # in the expected millisecond range.
    first = body["trades"][0]
    assert isinstance(first["entry_time"], int), (
        f"entry_time should be int64 ms UTC, got {type(first['entry_time']).__name__}: "
        f"{first['entry_time']!r}"
    )
    assert isinstance(first["exit_time"], int)
    # 13-digit ms-since-epoch (~year 2001 onwards). The synthetic data
    # starts in 2024, so anything below year 2001 indicates a unit bug.
    assert first["entry_time"] > 1_000_000_000_000, (
        f"entry_time looks like seconds, not ms: {first['entry_time']}"
    )
    assert first["exit_time"] > first["entry_time"], (
        f"exit_time {first['exit_time']} should be after entry_time {first['entry_time']}"
    )


async def _post_sma_backtest(fill_mode: str):
    """POST the canonical SMA fixture on the synthetic stream under ``fill_mode``."""
    app.dependency_overrides[get_data_source_factory] = _make_synthetic_factory
    try:
        async with await _client() as client:
            spec_payload = (await client.get("/api/spec-strategy/fixtures/sma_crossover")).json()
            spec_payload["symbols"] = [SYMBOL]
            return await client.post(
                "/api/spec-strategy/backtest",
                json={
                    "spec": spec_payload,
                    "start_date": "2024-01-02",
                    "end_date": "2024-12-31",
                    "fill_mode": fill_mode,
                },
            )
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)


async def test_backtest_runs_under_every_fill_mode_a_research_request_may_name() -> None:
    """Each mode fills the same entries at its own price (#2599)."""
    entries: dict[str, list[float]] = {}
    for fill_mode in ("signal_bar_close", "next_bar_open", "decision_minute_open"):
        resp = await _post_sma_backtest(fill_mode)
        assert resp.status_code == 200, f"{fill_mode}: {resp.text}"
        body = resp.json()
        assert body["success"] is True, body
        entries[fill_mode] = [trade["entry_price"] for trade in body["trades"]]

    assert all(entries.values())
    assert entries["decision_minute_open"] != entries["signal_bar_close"]
    assert entries["decision_minute_open"] != entries["next_bar_open"]


# ``open`` is the engine backtest's short name, which names nothing among three "...open" modes.
@pytest.mark.parametrize("fill_mode", ["magic", "next_session_open", "open"])
async def test_backtest_rejects_an_unknown_fill_mode_naming_the_modes(fill_mode: str) -> None:
    resp = await _post_sma_backtest(fill_mode)

    assert resp.status_code == 400, resp.text
    assert "signal_bar_close, next_bar_open or decision_minute_open" in resp.json()["detail"]


# A spec that schema-validates but that the run cannot honor must surface as
# 400, not as a 200 + ``success=false``. Otherwise API consumers cannot
# distinguish a client-side spec mistake from a transient run failure.
@pytest.mark.parametrize(
    ("edit_spec", "detail_part"),
    [
        # Replace the SetHoldings sizing with a Phase-2 FixedContracts value. The
        # schema accepts it (forward-compat); the evaluator's constructor raises
        # NotImplementedError (an entry that never fires used to return a 200 with
        # zero trades), and the route turns that into 4xx.
        pytest.param(
            lambda spec: spec["entry"].update(size={"kind": "FixedContracts", "value": 1}),
            "unsupported feature",
            id="unsupported_spec_feature",
        ),
        # This route has no prediction set to pair a ``predictions`` block with;
        # that is the caller's spec, so 400 with the reason, not an unhandled
        # ValueError.
        pytest.param(
            lambda spec: spec.update(predictions=[{"id": "p", "prediction_set_id": "ps_probe"}]),
            "declares predictions",
            id="declares_predictions_it_cannot_load",
        ),
    ],
)
async def test_backtest_answers_400_for_a_spec_the_run_cannot_honor(
    edit_spec: Callable[[dict], object], detail_part: str
) -> None:
    app.dependency_overrides[get_data_source_factory] = _make_synthetic_factory

    try:
        async with await _client() as client:
            spec_resp = await client.get("/api/spec-strategy/fixtures/sma_crossover")
            assert spec_resp.status_code == 200, "the canonical fixture is served"
            spec_payload = spec_resp.json()
            spec_payload["symbols"] = [SYMBOL]
            edit_spec(spec_payload)

            resp = await client.post(
                "/api/spec-strategy/backtest",
                json={
                    "spec": spec_payload,
                    "start_date": "2024-01-02",
                    "end_date": "2024-12-31",
                    "initial_cash": 100000.0,
                },
            )
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)

    assert resp.status_code == 400, f"expected 400 for a spec the run cannot honor, got {resp.status_code}: {resp.text}"
    assert detail_part in resp.json()["detail"], "the 400 names the reason"


async def test_a_data_source_that_cannot_be_built_is_reported_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The failure was returned to the caller and left no trace in the service's own log."""

    def _factory_that_fails(symbol: str, start: date, end: date) -> object:
        raise RuntimeError("the lake is down")

    app.dependency_overrides[get_data_source_factory] = lambda: _factory_that_fails
    try:
        async with await _client() as client:
            spec_payload = (await client.get("/api/spec-strategy/fixtures/sma_crossover")).json()
            spec_payload["symbols"] = [SYMBOL]
            with caplog.at_level("ERROR", logger="app.routers.spec_strategy"):
                resp = await client.post(
                    "/api/spec-strategy/backtest",
                    json={
                        "spec": spec_payload,
                        "start_date": "2024-01-02",
                        "end_date": "2024-12-31",
                        "initial_cash": 100000.0,
                    },
                )
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)

    body = resp.json()
    assert (resp.status_code, body["success"]) == (200, False)
    assert "data source unavailable: the lake is down" in body["error"]
    assert any(record.exc_info and "the lake is down" in str(record.exc_info[1]) for record in caplog.records)


async def test_backtest_rejects_malformed_spec() -> None:
    """A spec with an unknown condition kind must be rejected with 4xx
    (FastAPI surfaces the Pydantic ValidationError as 422)."""
    async with await _client() as client:
        resp = await client.post(
            "/api/spec-strategy/backtest",
            json={
                "spec": {
                    "schema_version": "1.0",
                    "name": "broken",
                    "symbols": ["SPY"],
                    "resolution": {"period_minutes": 15},
                    "indicators": [],
                    "entry": {
                        "logic": "AND",
                        "conditions": [{"kind": "MysteryCondition"}],
                        "size": {"kind": "SetHoldings", "fraction": 1.0},
                    },
                    "exit": {"logic": "OR", "conditions": []},
                },
                "start_date": "2024-01-02",
                "end_date": "2024-12-31",
            },
        )
    assert resp.status_code in (400, 422), f"expected 4xx, got {resp.status_code}: {resp.text}"
