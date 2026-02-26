"""Shared fixtures for research module tests."""
from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def sample_bars_single_day() -> list[dict]:
    """Generate 200 1-minute bars within a single trading day.

    All bars share the same date so cross-day contamination logic can
    be tested with a separate multi-day fixture.
    """
    bars: list[dict] = []
    base_price = 150.0
    base_ts = 1704117000000  # 2024-01-01 13:50 UTC (inside trading hours)

    rng = np.random.default_rng(42)

    for i in range(200):
        noise = rng.normal(0, 0.3)
        trend = np.sin(i * 0.05) * 2
        price = base_price + trend + noise

        bars.append({
            "timestamp": base_ts + i * 60_000,  # 1-minute spacing
            "open": round(price - 0.05, 4),
            "high": round(price + 0.3, 4),
            "low": round(price - 0.3, 4),
            "close": round(price, 4),
            "volume": round(1_000_000 + rng.normal(0, 50_000), 2),
        })

    return bars


@pytest.fixture
def sample_bars_multi_day() -> list[dict]:
    """Generate bars spanning 3 trading days (50 bars per day).

    Timestamps jump across midnight boundaries to test cross-day masking.
    """
    bars: list[dict] = []
    rng = np.random.default_rng(123)

    day_starts = [
        1704117000000,   # 2024-01-01 13:50 UTC
        1704203400000,   # 2024-01-02 13:50 UTC
        1704289800000,   # 2024-01-03 13:50 UTC
    ]
    base_price = 150.0

    for day_start in day_starts:
        for i in range(50):
            noise = rng.normal(0, 0.2)
            price = base_price + i * 0.01 + noise
            bars.append({
                "timestamp": day_start + i * 60_000,
                "open": round(price - 0.05, 4),
                "high": round(price + 0.3, 4),
                "low": round(price - 0.3, 4),
                "close": round(price, 4),
                "volume": round(1_000_000 + rng.normal(0, 50_000), 2),
            })

    return bars
