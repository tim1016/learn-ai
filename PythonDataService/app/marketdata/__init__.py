"""Broker-neutral market-data plane.

The one sanctioned shared surface across execution brokers.  All
broker silo code (``app/broker/ibkr/``, ``app/broker/alpaca/``) stays
siloed; this package is the only place where market-data concerns cross
that boundary.

Public surface
--------------
- :class:`MarketDataBar`  — broker-neutral closed 1-minute bar
- :class:`FeedHealth`      — connected / stale / last_bar_ms snapshot
- :class:`MarketDataFeedError` — fatal feed error (connection death)
- :class:`MarketDataFeed`  — Protocol every implementation satisfies
"""

from app.marketdata.feed import FeedHealth, MarketDataBar, MarketDataFeed, MarketDataFeedError

__all__ = [
    "FeedHealth",
    "MarketDataBar",
    "MarketDataFeed",
    "MarketDataFeedError",
]
