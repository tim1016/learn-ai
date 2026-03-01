"""Tests for the research REST endpoint."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_run_feature_success(client: AsyncClient) -> None:
    """POST /api/research/run-feature with valid data returns 200."""
    base_ts = 1704117000000
    payload = {
        "ticker": "TEST",
        "feature_name": "momentum_5m",
        "bars": [
            {
                "timestamp": base_ts + i * 60_000,
                "open": 100.0 + i * 0.01,
                "high": 101.0 + i * 0.01,
                "low": 99.0 + i * 0.01,
                "close": 100.5 + i * 0.01,
                "volume": 1_000_000.0,
            }
            for i in range(200)
        ],
        "start_date": "2024-01-01",
        "end_date": "2024-01-01",
    }

    response = await client.post("/api/research/run-feature", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "TEST"
    assert data["feature_name"] == "momentum_5m"
    assert "mean_ic" in data
    assert "ic_t_stat" in data
    assert "adf_pvalue" in data
    assert "passed_validation" in data
    assert isinstance(data["quantile_bins"], list)


@pytest.mark.asyncio
async def test_run_feature_too_few_bars(client: AsyncClient) -> None:
    """Too few bars should return success=false with error message."""
    payload = {
        "ticker": "TEST",
        "feature_name": "momentum_5m",
        "bars": [
            {"timestamp": i * 60_000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1e6}
            for i in range(10)
        ],
        "start_date": "2024-01-01",
        "end_date": "2024-01-01",
    }

    response = await client.post("/api/research/run-feature", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["error"] is not None


@pytest.mark.asyncio
async def test_run_feature_invalid_body(client: AsyncClient) -> None:
    """Missing required fields should return 422."""
    response = await client.post("/api/research/run-feature", json={"ticker": "TEST"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_features(client: AsyncClient) -> None:
    """GET /api/research/features should return available features."""
    response = await client.get("/api/research/features")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 10
    names = {f["name"] for f in data}
    assert "5-Minute Momentum" in names


@pytest.mark.asyncio
async def test_get_documentation(client: AsyncClient) -> None:
    """GET /api/research/documentation should return full docs."""
    response = await client.get("/api/research/documentation")

    assert response.status_code == 200
    data = response.json()
    assert "target" in data
    assert "features" in data
    assert "validation" in data
