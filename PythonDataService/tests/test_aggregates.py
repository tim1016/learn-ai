"""Tests for the aggregates endpoint (/api/aggregates/fetch)"""

from unittest.mock import patch

import pytest


@pytest.mark.anyio
async def test_fetch_aggregates_success(client):
    """Successful aggregate fetch returns sanitized data"""
    mock_raw = [
        {"timestamp": 1704067200000, "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0, "volume": 1000000.0},
    ]

    with (
        patch("app.routers.aggregates.polygon_client") as mock_polygon,
        patch("app.routers.aggregates.sanitizer") as mock_sanitizer,
    ):
        mock_polygon.fetch_aggregates.return_value = mock_raw
        mock_sanitizer.sanitize_aggregates.return_value = {
            "data": [
                {
                    "timestamp": 1704067200000,
                    "open": 150.0,
                    "high": 155.0,
                    "low": 148.0,
                    "close": 153.0,
                    "volume": 1000000.0,
                }
            ],
            "summary": {
                "original_count": 1,
                "cleaned_count": 1,
                "removed_count": 0,
                "removal_percentage": 0.0,
            },
        }

        response = await client.post(
            "/api/aggregates/fetch",
            json={
                "ticker": "AAPL",
                "multiplier": 1,
                "timespan": "day",
                "from_date": "2024-01-01",
                "to_date": "2024-01-31",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["ticker"] == "AAPL"
    assert data["data_type"] == "aggregates"
    assert len(data["data"]) == 1
    assert data["summary"]["original_count"] == 1


@pytest.mark.anyio
async def test_fetch_aggregates_empty_ticker_returns_422(client):
    """Empty ticker should be rejected by Pydantic validation"""
    response = await client.post(
        "/api/aggregates/fetch",
        json={
            "ticker": "",
            "multiplier": 1,
            "timespan": "day",
            "from_date": "2024-01-01",
            "to_date": "2024-01-31",
        },
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_fetch_aggregates_invalid_timespan_returns_422(client):
    """Invalid timespan should fail Pydantic validation"""
    response = await client.post(
        "/api/aggregates/fetch",
        json={
            "ticker": "AAPL",
            "multiplier": 1,
            "timespan": "invalid",
            "from_date": "2024-01-01",
            "to_date": "2024-01-31",
        },
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_fetch_aggregates_polygon_error_returns_500(client):
    """Polygon API error should return 500"""
    with patch("app.routers.aggregates.polygon_client") as mock_polygon:
        mock_polygon.fetch_aggregates.side_effect = Exception("Polygon rate limit")

        response = await client.post(
            "/api/aggregates/fetch",
            json={
                "ticker": "AAPL",
                "multiplier": 1,
                "timespan": "day",
                "from_date": "2024-01-01",
                "to_date": "2024-01-31",
            },
        )

    assert response.status_code == 500
    assert "Polygon rate limit" in response.json()["detail"]
