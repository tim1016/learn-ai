"""Tests for the /api/strategy/analyze endpoint (router-level integration)."""
from __future__ import annotations

import math

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Bull call spread (basic debit spread)
# ---------------------------------------------------------------------------

class TestStrategyEndpoint:
    @pytest.mark.asyncio
    async def test_bull_call_spread_returns_200(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [
                {"strike": 100, "option_type": "call", "position": "long",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
                {"strike": 105, "option_type": "call", "position": "short",
                 "premium": 2.0, "iv": 0.23, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 102,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["success"] is True
        assert data["symbol"] == "AAPL"
        assert data["strategy_cost"] == pytest.approx(3.0)
        assert data["max_profit"] == pytest.approx(2.0, abs=0.1)
        assert data["max_loss"] == pytest.approx(-3.0, abs=0.1)
        assert len(data["breakevens"]) == 1
        assert data["breakevens"][0] == pytest.approx(103.0, abs=0.2)
        assert 0.0 <= data["pop"] <= 1.0
        assert math.isfinite(data["expected_value"])
        assert len(data["curve"]) == 300
        assert "delta" in data["greeks"]
        assert "gamma" in data["greeks"]

    @pytest.mark.asyncio
    async def test_iron_condor_returns_correct_structure(self, client: AsyncClient):
        payload = {
            "symbol": "SPY",
            "legs": [
                {"strike": 90, "option_type": "put", "position": "long",
                 "premium": 0.50, "iv": 0.35, "quantity": 1},
                {"strike": 95, "option_type": "put", "position": "short",
                 "premium": 2.0, "iv": 0.30, "quantity": 1},
                {"strike": 105, "option_type": "call", "position": "short",
                 "premium": 2.0, "iv": 0.25, "quantity": 1},
                {"strike": 110, "option_type": "call", "position": "long",
                 "premium": 0.50, "iv": 0.22, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 200

        data = response.json()
        assert data["success"] is True
        assert data["strategy_cost"] == pytest.approx(-3.0)  # credit
        assert len(data["breakevens"]) == 2
        assert data["max_profit"] == pytest.approx(3.0, abs=0.1)
        assert data["max_loss"] == pytest.approx(-2.0, abs=0.1)


# ---------------------------------------------------------------------------
# Validation: Pydantic model enforcement
# ---------------------------------------------------------------------------

class TestStrategyEndpointValidation:
    @pytest.mark.asyncio
    async def test_missing_legs_returns_422(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_option_type_returns_422(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [
                {"strike": 100, "option_type": "butterfly", "position": "long",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_position_returns_422(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [
                {"strike": 100, "option_type": "call", "position": "neutral",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_strike_returns_422(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [
                {"strike": -10, "option_type": "call", "position": "long",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_zero_spot_price_returns_422(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [
                {"strike": 100, "option_type": "call", "position": "long",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 0,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_symbol_returns_422(self, client: AsyncClient):
        payload = {
            "legs": [
                {"strike": 100, "option_type": "call", "position": "long",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_custom_curve_points(self, client: AsyncClient):
        payload = {
            "symbol": "AAPL",
            "legs": [
                {"strike": 100, "option_type": "call", "position": "long",
                 "premium": 5.0, "iv": 0.25, "quantity": 1},
            ],
            "expiration_date": "2026-12-31",
            "spot_price": 100,
            "curve_points": 100,
        }
        response = await client.post("/api/strategy/analyze", json=payload)
        assert response.status_code == 200
        assert len(response.json()["curve"]) == 100
