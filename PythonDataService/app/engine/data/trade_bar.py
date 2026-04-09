"""TradeBar dataclass mirroring LEAN's Common/Data/Market/TradeBar.cs.

LEAN uses `decimal` throughout for prices to avoid float drift over long
indicator recursions. We mirror this with `Decimal` for prices while keeping
volume as an int and timestamps as timezone-aware datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TradeBar:
    """A single OHLCV bar.

    Attributes:
        symbol: Ticker symbol (uppercase).
        time: Bar start time, timezone-aware (exchange timezone).
        end_time: Bar end time, timezone-aware. For a 1-minute bar, end_time =
            time + 1 minute. For consolidated bars, end_time is the end_time of
            the last minute bar that was included in the consolidation.
        open: Opening price.
        high: High price.
        low: Low price.
        close: Closing price.
        volume: Traded volume (shares for equities).
    """

    symbol: str
    time: datetime
    end_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @property
    def period_seconds(self) -> float:
        return (self.end_time - self.time).total_seconds()
