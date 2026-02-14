"""Tests for health and root endpoints"""
import pytest


@pytest.mark.anyio
async def test_health_returns_200(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "polygon-data-service"


@pytest.mark.anyio
async def test_root_returns_service_info(client):
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "Polygon Data Service"
    assert data["version"] == "1.0.0"
    assert "docs" in data
    assert "health" in data
