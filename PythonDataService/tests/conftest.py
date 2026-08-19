"""Shared test fixtures and helpers"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

# Patch env before importing app
os.environ.setdefault("POLYGON_API_KEY", "test-key-for-testing")
# Router tests exercise control endpoints without modeling the local
# data-plane shared-secret hop; opt out explicitly here while dedicated
# security tests monkeypatch this off to prove production fail-closed behavior.
os.environ.setdefault("DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", "true")

from app.main import app


@pytest.fixture(autouse=True)
def _clerk_market_liveness_defaults_tradable(monkeypatch: pytest.MonkeyPatch):
    """The Alpaca Clerk's submission-boundary liveness recheck (#1671,
    ``runtime.py::_execute_effect``) fails closed by default — correct in
    production, but it silently stalls any pre-existing test that drives a
    real ENTER through ``SqliteAlpacaClerkFacade`` without first configuring
    live market-liveness evidence (the unconfigured process-global store
    resolves to UNKNOWN, so every such ENTER is rejected before it ever
    reaches the broker). Default every test to a fresh TRADABLE fact; a test
    that specifically exercises the gate overrides this with its own
    ``monkeypatch.setattr(clerk_runtime, "market_liveness_fact", ...)``,
    which always wins over this fixture (later patches win)."""
    import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
    from app.schemas.market_liveness import MarketClockLivenessEvidence
    from app.services.market_liveness import compose_market_liveness

    def _tradable(symbol: str, observed_at_ms: int):
        return compose_market_liveness(
            symbol,
            now_ms=observed_at_ms,
            market_clock=MarketClockLivenessEvidence(
                state="OPEN",
                source="test.default-tradable-clock",
                observed_at_ms=observed_at_ms,
                vendor_timestamp_ms=observed_at_ms,
            ),
            connected=True,
            connection_changed_at_ms=observed_at_ms,
            symbol_status=None,
        )

    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _tradable)


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
        bars.append(
            {
                "timestamp": 1704067200000 + i * 86400000,  # daily from 2024-01-01
                "open": price,
                "high": price + 2.0,
                "low": price - 1.0,
                "close": price + 1.0,
                "volume": 1000000.0 + i * 10000,
            }
        )
    return bars
