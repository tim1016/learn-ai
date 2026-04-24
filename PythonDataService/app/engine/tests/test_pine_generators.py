"""Smoke tests for Pine v6 generators.

Layer 1: each generator produces a string containing the strategy's
Pine strategy() declaration and the expected entry-gate keywords.
Layer 2: the /api/engine/strategies/{name}/pine endpoint round-trips
user params through the generator and returns text/plain with an
attachment Content-Disposition.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.engine.pine_generators import (
    generate_strategy_a_pine,
    generate_strategy_b_pine,
    generate_strategy_c_pine,
)
from app.main import app
from app.routers.engine import (
    RsiRangeStrategyAParams,
    RsiRangeStrategyBParams,
    RsiRangeStrategyCParams,
)


def test_strategy_a_generator_embeds_user_params():
    pine = generate_strategy_a_pine(
        RsiRangeStrategyAParams(
            ema_fast_period=8,
            ema_slow_period=21,
            ema_gap_threshold=0.75,
            rsi_low_gate=40,
            rsi_high_gate=65,
            adx_exit_threshold=12,
        )
    )
    assert "//@version=6" in pine
    assert 'strategy("Strategy A' in pine
    assert "input.int(8," in pine
    assert "input.int(21," in pine
    assert "input.float(0.75," in pine
    assert "input.float(40.0," in pine
    assert "input.float(65.0," in pine
    assert "input.float(12.0," in pine
    assert "rsiInRange" in pine
    assert "emaGap > emaGapThreshold" in pine
    assert "macdLine > 0" in pine


def test_strategy_b_generator_embeds_user_params():
    pine = generate_strategy_b_pine(
        RsiRangeStrategyBParams(
            supertrend_atr_period=7,
            supertrend_multiplier=2.5,
            adx_entry_threshold=18,
            adx_exit_threshold=22,
        )
    )
    assert "//@version=6" in pine
    assert 'strategy("Strategy B' in pine
    assert "input.int(7," in pine
    assert "input.float(2.5," in pine
    assert "input.float(18.0," in pine
    assert "input.float(22.0," in pine
    assert "ta.supertrend" in pine
    assert "stIsLong = stDir == -1" in pine


def test_strategy_c_generator_embeds_user_params():
    pine = generate_strategy_c_pine(
        RsiRangeStrategyCParams(
            adx_entry_threshold=25,
            adx_exit_threshold=13,
            rsi_low_gate=35,
            rsi_high_gate=75,
        )
    )
    assert "//@version=6" in pine
    assert 'strategy("Strategy C' in pine
    assert "input.float(25.0," in pine
    assert "input.float(13.0," in pine
    assert "input.float(35.0," in pine
    assert "input.float(75.0," in pine
    assert "adxRising" in pine
    assert "rsiInRange" in pine


@pytest.mark.asyncio
async def test_pine_endpoint_returns_attachment():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/engine/strategies/rsi_range_a/pine",
            json={
                "symbol": "SPY",
                "ema_fast_period": 20,
                "ema_slow_period": 50,
                "ema_gap_threshold": 0.5,
                "macd_fast": 12,
                "macd_slow": 26,
                "macd_signal": 9,
                "rsi_period": 14,
                "rsi_low_gate": 38,
                "rsi_high_gate": 70,
                "adx_period": 14,
                "adx_exit_threshold": 15,
                "resolution_minutes": 15,
            },
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "rsi_range_a.pine" in resp.headers["content-disposition"]
    assert "//@version=6" in resp.text


@pytest.mark.asyncio
async def test_pine_endpoint_404_for_unknown_strategy():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/engine/strategies/no_such/pine", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pine_endpoint_404_for_strategy_without_template():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/engine/strategies/sma_crossover/pine",
            json={"symbol": "SPY", "short_window": 10, "long_window": 30, "resolution_minutes": 15},
        )
    assert resp.status_code == 404
    assert "No Pine script template" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_pine_endpoint_422_for_invalid_params():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/engine/strategies/rsi_range_a/pine",
            json={"ema_fast_period": "not-an-int"},
        )
    assert resp.status_code == 422
