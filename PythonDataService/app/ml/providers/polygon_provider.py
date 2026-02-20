from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)


class PolygonDataProvider:
    """MarketDataProvider implementation wrapping the existing PolygonClientService."""

    def __init__(self, client: PolygonClientService | None = None) -> None:
        self._client = client or PolygonClientService()

    def fetch_ohlcv(
        self,
        ticker: str,
        from_date: str,
        to_date: str,
        timespan: str = "day",
        multiplier: int = 1,
    ) -> List[Dict[str, Any]]:
        logger.info(f"[ML] Fetching OHLCV via Polygon: {ticker} {from_date} to {to_date}")
        return self._client.fetch_aggregates(
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_date=from_date,
            to_date=to_date,
        )
