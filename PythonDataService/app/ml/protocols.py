from __future__ import annotations

from typing import Any, Dict, List, Protocol


class MarketDataProvider(Protocol):
    """Protocol for fetching OHLCV market data.

    Implementations must return data as a list of dicts with keys:
    timestamp (Unix ms), open, high, low, close, volume
    """

    def fetch_ohlcv(
        self,
        ticker: str,
        from_date: str,
        to_date: str,
        timespan: str = "day",
        multiplier: int = 1,
    ) -> List[Dict[str, Any]]: ...
