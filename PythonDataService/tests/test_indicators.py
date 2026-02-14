"""Tests for the /api/indicators/calculate endpoint"""
import pytest
from tests.conftest import make_sample_bars


@pytest.mark.anyio
async def test_calculate_sma_returns_success(client):
    bars = make_sample_bars(30)
    response = await client.post("/api/indicators/calculate", json={
        "ticker": "AAPL",
        "bars": bars,
        "indicators": [{"name": "sma", "window": 10}],
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["ticker"] == "AAPL"
    assert len(data["indicators"]) == 1
    assert data["indicators"][0]["name"] == "sma"
    assert data["indicators"][0]["window"] == 10
    assert len(data["indicators"][0]["data"]) > 0


@pytest.mark.anyio
async def test_calculate_multiple_indicators(client):
    bars = make_sample_bars(30)
    response = await client.post("/api/indicators/calculate", json={
        "ticker": "MSFT",
        "bars": bars,
        "indicators": [
            {"name": "sma", "window": 5},
            {"name": "ema", "window": 10},
            {"name": "rsi", "window": 14},
        ],
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["indicators"]) == 3
    names = [ind["name"] for ind in data["indicators"]]
    assert "sma" in names
    assert "ema" in names
    assert "rsi" in names


@pytest.mark.anyio
async def test_calculate_invalid_indicator_name(client):
    bars = make_sample_bars(10)
    response = await client.post("/api/indicators/calculate", json={
        "ticker": "AAPL",
        "bars": bars,
        "indicators": [{"name": "invalid_indicator", "window": 10}],
    })
    assert response.status_code == 422  # Pydantic validation error


@pytest.mark.anyio
async def test_calculate_empty_bars_rejected(client):
    response = await client.post("/api/indicators/calculate", json={
        "ticker": "AAPL",
        "bars": [],
        "indicators": [{"name": "sma", "window": 10}],
    })
    assert response.status_code == 422


@pytest.mark.anyio
async def test_calculate_empty_indicators_rejected(client):
    bars = make_sample_bars(10)
    response = await client.post("/api/indicators/calculate", json={
        "ticker": "AAPL",
        "bars": bars,
        "indicators": [],
    })
    assert response.status_code == 422
