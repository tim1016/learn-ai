"""Read-only diagnostic surface for the shared MarketDataFeed.

Exposes feed health and active subscription count so the slice is
demoable without any bot running.

Endpoints
---------
GET /api/market-data-feed/health
    Returns ``FeedHealth`` (connected / stale / last_bar_ms /
    active_subscription_count / reason / observed_at_ms).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.marketdata.feed import FeedHealth
from app.marketdata.ibkr_feed import get_market_data_feed

logger = logging.getLogger(__name__)

router = APIRouter(tags=["market-data-feed"])


@router.get(
    "/health",
    response_model=FeedHealth,
    summary="Shared IBKR market-data feed health",
    description=(
        "Returns the current health snapshot for the process-level shared "
        "MarketDataFeed: connection state, stale flag, last bar timestamp, "
        "and active subscription count.  Returns 503 when the feed has not "
        "been installed (IBKR broker disabled)."
    ),
)
async def get_feed_health() -> FeedHealth:
    feed = get_market_data_feed()
    if feed is None:
        raise HTTPException(
            status_code=503,
            detail="MarketDataFeed not available (IBKR broker disabled or not yet started).",
        )
    return feed.health()
