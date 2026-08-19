"""TradeBar dataclass mirroring LEAN's Common/Data/Market/TradeBar.cs.

LEAN uses `decimal` throughout for prices to avoid float drift over long
indicator recursions. We mirror this with `Decimal` for prices while keeping
volume as an int and timestamps as canonical ``int64`` Unix milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.utils.timestamps import require_timestamp_ms


@dataclass(frozen=True, init=False)
class TradeBar:
    """A single OHLCV bar.

    Attributes:
        symbol: Ticker symbol (uppercase).
        start_ms: Bar start time, Unix milliseconds UTC.
        end_ms: Bar end time, Unix milliseconds UTC. For a 1-minute bar,
            ``end_ms = start_ms + 60_000``. For consolidated bars, ``end_ms``
            is the end of the last minute bar included in the consolidation.
        open: Opening price.
        high: High price.
        low: Low price.
        close: Closing price.
        volume: Traded volume (shares for equities).
    """

    symbol: str
    start_ms: int
    end_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __init__(
        self,
        *,
        symbol: str,
        open: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
        time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> None:
        """Create a numeric engine bar.

        ``time`` and ``end_time`` are temporary input-boundary aliases for
        historical readers being migrated. They are converted immediately and
        are never retained on the bar, so all consumers receive only ms UTC.
        New callers must supply ``start_ms`` and ``end_ms``.
        """
        start_ms = require_timestamp_ms(start_ms, time, "TradeBar start_ms")
        end_ms = require_timestamp_ms(end_ms, end_time, "TradeBar end_ms")
        if end_ms < start_ms:
            raise ValueError("TradeBar end_ms must not precede start_ms")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "start_ms", start_ms)
        object.__setattr__(self, "end_ms", end_ms)
        object.__setattr__(self, "open", open)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)

    @property
    def period_seconds(self) -> float:
        return (self.end_ms - self.start_ms) / 1000
