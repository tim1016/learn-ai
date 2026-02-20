from __future__ import annotations

from typing import Any, Dict, List

import numpy as np


class MockDataProvider:
    """Deterministic fake data provider for testing.

    Generates a sine-wave-based price series with controlled noise
    so that LSTM tests have a predictable, learnable pattern.
    """

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed

    def fetch_ohlcv(
        self,
        ticker: str,
        from_date: str,
        to_date: str,
        timespan: str = "day",
        multiplier: int = 1,
    ) -> List[Dict[str, Any]]:
        rng = np.random.default_rng(self._seed)
        num_days = 504  # ~2 years of trading days
        base_ts = 1640995200000  # 2022-01-01 UTC in ms

        bars: List[Dict[str, Any]] = []
        base_price = 150.0
        for i in range(num_days):
            price = base_price + 20 * np.sin(2 * np.pi * i / 252) + 0.02 * i
            noise = rng.normal(0, 0.5)
            close = price + noise
            high = close + abs(rng.normal(0, 1.0))
            low = close - abs(rng.normal(0, 1.0))
            open_price = close + rng.normal(0, 0.3)

            bars.append(
                {
                    "timestamp": base_ts + i * 86400000,
                    "open": round(float(open_price), 2),
                    "high": round(float(high), 2),
                    "low": round(float(low), 2),
                    "close": round(float(close), 2),
                    "volume": int(rng.integers(500000, 5000000)),
                    "vwap": None,
                    "transactions": None,
                }
            )
        return bars
