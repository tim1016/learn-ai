"""Tests for GET /api/engine/bars — the shared-bar-store charting endpoint.

Includes the golden equality gate: the endpoint's bars must equal the
``chart_bars`` a live engine run reports for the same policy + window,
because both read the same roots through the same reader and the same
consolidator. If this test breaks, the run-report price chart no longer
shows what the engine consumed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests._helpers.lean_store import seed_store_day

DAY_ONE = date(2026, 1, 5)  # Monday
DAY_TWO = date(2026, 1, 6)  # Tuesday


@pytest.fixture
def store(monkeypatch, tmp_path: Path) -> Path:
    """Point the bar store at a tmp cache with two seeded SPY days."""
    monkeypatch.setenv("LEAN_DATA_ROOT", str(tmp_path / "no-reference-mount"))
    monkeypatch.setenv("LEAN_DATA_CACHE", str(tmp_path / "store"))
    policy_root = tmp_path / "store" / "polygon-adjusted"
    seed_store_day(policy_root, "SPY", DAY_ONE)
    seed_store_day(policy_root, "SPY", DAY_TWO)
    return policy_root


async def _get_bars(client: AsyncClient, **overrides) -> dict:
    params = {
        "symbol": "SPY",
        "from_date": DAY_ONE.isoformat(),
        "to_date": DAY_TWO.isoformat(),
        "adjusted": True,
        "session": "regular",
        "timespan": "minute",
        "multiplier": 15,
    }
    params.update(overrides)
    response = await client.get("/api/engine/bars", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_bars_consolidate_to_strategy_timeframe(store, client):
    payload = await _get_bars(client)

    assert payload["policy_key"] == "polygon-adjusted"
    assert payload["count"] == len(payload["bars"]) > 0
    # 390 RTH minutes per day → 26 15-minute bars per day, 2 days.
    assert payload["count"] == 52
    for bar in payload["bars"]:
        # Exchange-aligned 15-minute starts (temporal-rigor bar alignment).
        assert bar["t"] % (15 * 60_000) == 0
    first = payload["bars"][0]
    open_ms = int(datetime(2026, 1, 5, 14, 30, tzinfo=UTC).timestamp() * 1000)
    assert first["t"] == open_ms
    assert payload["coverage"]["is_complete"] is True
    assert payload["coverage"]["missing_days"] == []


@pytest.mark.asyncio
async def test_bars_policy_separation_raw_tree_is_empty(store, client):
    payload = await _get_bars(client, adjusted=False)

    assert payload["policy_key"] == "polygon-raw"
    assert payload["count"] == 0
    assert payload["coverage"]["available_days"] == 0
    assert payload["coverage"]["missing_days"] == [DAY_ONE.isoformat(), DAY_TWO.isoformat()]


@pytest.mark.asyncio
async def test_bars_missing_days_surface_in_coverage_not_500(store, client):
    payload = await _get_bars(client, to_date=date(2026, 1, 7).isoformat())

    assert payload["coverage"]["is_complete"] is False
    assert payload["coverage"]["missing_days"] == [date(2026, 1, 7).isoformat()]
    # The seeded days still chart.
    assert payload["count"] == 52


@pytest.mark.asyncio
async def test_bars_rejects_path_unsafe_symbol(store, client):
    response = await client.get(
        "/api/engine/bars",
        params={"symbol": "a/../b", "from_date": "2026-01-05", "to_date": "2026-01-06"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_bars_rejects_inverted_window(store, client):
    response = await client.get(
        "/api/engine/bars",
        params={"symbol": "SPY", "from_date": "2026-01-06", "to_date": "2026-01-05"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.slow
async def test_bars_endpoint_equals_live_run_chart_bars(store):
    """Golden gate: /bars output == the live run's transient chart_bars."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=120.0) as client:
        run = await client.post(
            "/api/engine/backtest",
            json={
                "strategy_name": "spy_ema_crossover",
                "from_date": DAY_ONE.isoformat(),
                "to_date": DAY_TWO.isoformat(),
                "params": {"symbol": "SPY"},
                "auto_fetch": False,
            },
        )
        assert run.status_code == 200, run.text
        run_payload = run.json()
        assert run_payload["success"] is True, run_payload.get("error")
        assert run_payload["chart_bars"], "engine produced no chart bars — fixture setup broken"

        bars = await _get_bars(client)

    assert bars["bars"] == run_payload["chart_bars"]
