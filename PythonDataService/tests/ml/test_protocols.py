from __future__ import annotations

from app.ml.protocols import MarketDataProvider
from app.ml.providers.mock_provider import MockDataProvider


def test_mock_provider_satisfies_protocol() -> None:
    """MockDataProvider structurally matches MarketDataProvider."""
    provider: MarketDataProvider = MockDataProvider()
    assert hasattr(provider, "fetch_ohlcv")


def test_mock_provider_returns_expected_shape() -> None:
    provider = MockDataProvider(seed=42)
    data = provider.fetch_ohlcv("AAPL", "2022-01-01", "2024-01-01")
    assert len(data) == 504
    required_keys = {"timestamp", "open", "high", "low", "close", "volume"}
    assert required_keys.issubset(data[0].keys())


def test_mock_provider_no_nulls_in_ohlcv() -> None:
    provider = MockDataProvider(seed=42)
    data = provider.fetch_ohlcv("AAPL", "2022-01-01", "2024-01-01")
    for bar in data:
        for key in ("open", "high", "low", "close", "volume"):
            assert bar[key] is not None


def test_mock_provider_deterministic() -> None:
    p1 = MockDataProvider(seed=42)
    p2 = MockDataProvider(seed=42)
    assert p1.fetch_ohlcv("X", "", "") == p2.fetch_ohlcv("X", "", "")


def test_mock_provider_different_seeds_differ() -> None:
    p1 = MockDataProvider(seed=42)
    p2 = MockDataProvider(seed=99)
    data1 = p1.fetch_ohlcv("X", "", "")
    data2 = p2.fetch_ohlcv("X", "", "")
    assert data1[0]["close"] != data2[0]["close"]
