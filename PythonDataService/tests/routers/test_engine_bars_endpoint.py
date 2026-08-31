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

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.data_lake.types import polygon_mode_for
from app.main import app
from tests._helpers.lake_fixture import seed_lake_daily, seed_lake_minute_day

DAY_ONE = date(2026, 1, 5)  # Monday
DAY_TWO = date(2026, 1, 6)  # Tuesday


@pytest.fixture
def store(monkeypatch, tmp_path: Path) -> Path:
    """Seed the lake's adjusted-mode root with two SPY days.

    Before #1893 this seeded the policy store's ``polygon-adjusted`` cache
    subtree and turned the lake off. The store is gone; ``resolve_data_roots``
    now answers with the lake root for the run's adjustment mode, so the same
    two days are seeded there instead. What the tests below assert —
    consolidation to the strategy timeframe, exchange-aligned bar starts,
    coverage, and equality with a live run's ``chart_bars`` — is unchanged.
    """
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    adjusted_root = write_root / lake_subpath(polygon_mode_for(True))
    seed_lake_minute_day(adjusted_root, "SPY", DAY_ONE)
    seed_lake_minute_day(adjusted_root, "SPY", DAY_TWO)
    seed_lake_daily(adjusted_root, "SPY", [DAY_ONE, DAY_TWO])
    return adjusted_root


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
                "strategy_name": "ema_crossover_signal",
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
