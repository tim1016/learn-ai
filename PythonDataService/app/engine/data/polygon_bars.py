"""Polygon minute aggregates -> LEAN ``TradeBar``s, grouped by trading day.

This module was ``polygon_export.py``, the Polygon -> LEAN-format zip
exporter: it wrote one ``{YYYYMMDD}_trade.zip`` per Eastern-time trading day
under ``{output_root}/equity/usa/minute/{symbol}/`` for the policy store.
#1893 retired that store and deleted the exporter, leaving the two pure
conversion helpers the writing was built on. It is named for what it does now
rather than for the function it used to hold, so a reader looking for an
exporter does not find a module that no longer has one.

Each input bar is expected to look like::

    {
        "timestamp": 1712826000000,  # start-of-bar, ms since epoch UTC
        "open": 515.34,
        "high": 515.40,
        "low":  515.30,
        "close": 515.34,
        "volume": 12345,
    }

Bars outside regular trading hours are kept -- callers that want RTH-only
data should filter upstream. This matches LEAN's behavior, which stores the
full session and filters at the consolidator.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import datetime_at_ms

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


def polygon_bar_to_trade_bar(symbol: str, raw: dict[str, Any]) -> TradeBar:
    """Convert a Polygon aggregate dict to an immutable ``TradeBar``.

    Polygon timestamps are UTC epoch milliseconds pointing at the bar's
    start. LEAN uses bar start time too, so no shifting is needed — we
    just retain the numeric start timestamp and add one minute.
    """
    ts_ms = int(raw["timestamp"])
    return TradeBar(
        symbol=symbol,
        start_ms=ts_ms,
        end_ms=ts_ms + 60_000,
        # Use str-constructed Decimals to avoid float→Decimal round-trip
        # artifacts that would corrupt the deci-cent integer encoding.
        open=Decimal(str(raw["open"])),
        high=Decimal(str(raw["high"])),
        low=Decimal(str(raw["low"])),
        close=Decimal(str(raw["close"])),
        volume=int(raw["volume"] or 0),
    )


def group_by_trading_date(
    bars: Iterable[TradeBar],
) -> dict[date, list[TradeBar]]:
    """Bucket bars by their Eastern-time trading date."""
    grouped: dict[date, list[TradeBar]] = defaultdict(list)
    for bar in bars:
        et_time = datetime_at_ms(bar.start_ms, tz=EASTERN)
        grouped[et_time.date()].append(bar)
    # Ensure each day's bars are chronologically sorted, even if the
    # input was out of order (e.g. from a Postgres query without ORDER BY).
    for day_bars in grouped.values():
        day_bars.sort(key=lambda b: b.start_ms)
    return grouped
