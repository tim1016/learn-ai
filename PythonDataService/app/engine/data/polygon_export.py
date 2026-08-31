"""Polygon → LEAN-format zip exporter.

Bridge between Polygon's minute aggregates (as returned by
``app.services.polygon_client.PolygonClientService.fetch_aggregates``)
and the LEAN minute-bar zip format consumed by ``LeanMinuteDataReader``.

The exporter accepts any iterable of Polygon-style bar dicts (so the
caller may inject bars fetched directly, loaded from Postgres once a
cache layer exists, or replayed from a test fixture) and writes one
``{YYYYMMDD}_trade.zip`` per distinct Eastern-time trading day under
``{output_root}/equity/usa/minute/{symbol}/``.

Each input bar is expected to look like::

    {
        "timestamp": 1712826000000,  # start-of-bar, ms since epoch UTC
        "open": 515.34,
        "high": 515.40,
        "low":  515.30,
        "close": 515.34,
        "volume": 12345,
    }

Bars outside regular trading hours are kept — callers that want
RTH-only data should filter upstream. This matches LEAN's behavior,
which stores the full session and filters at the consolidator.
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
