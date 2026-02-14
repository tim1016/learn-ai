"""Shared test fixtures and helpers"""
import os
import pytest
from httpx import AsyncClient, ASGITransport

# Patch env before importing app
os.environ.setdefault("POLYGON_API_KEY", "test-key-for-testing")

from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_sample_bars(count: int = 30) -> list[dict]:
    """Create sample OHLCV bars for indicator tests."""
    bars = []
    base_price = 150.0
    for i in range(count):
        price = base_price + i * 0.5
        bars.append({
            "timestamp": 1704067200000 + i * 86400000,  # daily from 2024-01-01
            "open": price,
            "high": price + 2.0,
            "low": price - 1.0,
            "close": price + 1.0,
            "volume": 1000000.0 + i * 10000,
        })
    return bars
