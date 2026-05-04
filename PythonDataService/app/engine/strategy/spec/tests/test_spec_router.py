"""Endpoint tests for the spec-strategy FastAPI router.

Uses ``httpx.AsyncClient`` with ``ASGITransport`` so the tests run
in-process against the FastAPI app without a live HTTP server. The
data-source dependency is overridden with a synthetic minute-bar
reader so tests don't need access to the LEAN data archive.

Coverage:
  * GET  /api/spec-strategy/schema returns a draft-2020-12 JSON Schema
  * GET  /api/spec-strategy/fixtures lists the three canonical fixtures
  * GET  /api/spec-strategy/fixtures/sma_crossover round-trips through
    StrategySpec validation
  * POST /api/spec-strategy/backtest runs SMA spec on synthetic data and
    matches the in-process parity test's trade sequence
  * POST /api/spec-strategy/backtest returns 400 for malformed specs
"""

from __future__ import annotations

import asyncio
import sys

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
async def test_schema_endpoint() -> None:
    async with await _client() as client:
        resp = await client.get("/api/spec-strategy/schema")
    assert resp.status_code == 200
    schema = resp.json()
    assert "$defs" in schema
    assert "FreshCross" in schema["$defs"]
    assert "DrawdownFromPeak" in schema["$defs"]


async def test_fixtures_list_endpoint() -> None:
    async with await _client() as client:
        resp = await client.get("/api/spec-strategy/fixtures")
    assert resp.status_code == 200
    items = resp.json()
    names = {item["name"] for item in items}
    assert names == {"spy_ema_crossover", "sma_crossover", "rsi_mean_reversion"}, (
        f"unexpected fixture names: {names}"
    )


async def test_fixture_detail_endpoint() -> None:
    async with await _client() as client:
        resp = await client.get("/api/spec-strategy/fixtures/sma_crossover")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["name"].startswith("SMA")
    assert spec["symbols"] == ["SPY"]


async def test_fixture_detail_unknown_returns_404() -> None:
    async with await _client() as client:
        resp = await client.get("/api/spec-strategy/fixtures/does_not_exist")
    assert resp.status_code == 404


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


# ---------------------------------------------------------------------------
# Script entry point.
# ---------------------------------------------------------------------------
def run_all() -> None:
    failed = False
    tests = [
        ("schema endpoint", test_schema_endpoint),
        ("fixtures list endpoint", test_fixtures_list_endpoint),
        ("fixture detail endpoint", test_fixture_detail_endpoint),
        ("fixture detail unknown -> 404", test_fixture_detail_unknown_returns_404),
        ("backtest runs SMA spec on synthetic data", test_backtest_runs_sma_spec_on_synthetic_data),
        ("backtest rejects malformed spec", test_backtest_rejects_malformed_spec),
    ]
    for label, fn in tests:
        try:
            asyncio.run(fn())
            print(f"PASS: {label}")
        except AssertionError as e:
            failed = True
            print(f"FAIL: {label} — {e}")
        except Exception as e:
            failed = True
            print(f"ERROR: {label} — {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
