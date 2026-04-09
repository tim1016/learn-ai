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
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")


def _polygon_bar_to_trade_bar(symbol: str, raw: dict[str, Any]) -> TradeBar:
    """Convert a Polygon aggregate dict to an immutable ``TradeBar``.

    Polygon timestamps are UTC epoch milliseconds pointing at the bar's
    start. LEAN uses bar start time too, so no shifting is needed — we
    just localize to Eastern and compute ``end_time = time + 1 minute``.
    """
    ts_ms = int(raw["timestamp"])
    start_utc = datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo("UTC"))
    start_et = start_utc.astimezone(EASTERN)
    end_et = start_et + timedelta(minutes=1)

    return TradeBar(
        symbol=symbol,
        time=start_et,
        end_time=end_et,
        # Use str-constructed Decimals to avoid float→Decimal round-trip
        # artifacts that would corrupt the deci-cent integer encoding.
        open=Decimal(str(raw["open"])),
        high=Decimal(str(raw["high"])),
        low=Decimal(str(raw["low"])),
        close=Decimal(str(raw["close"])),
        volume=int(raw["volume"] or 0),
    )


def _group_by_trading_date(
    bars: Iterable[TradeBar],
) -> dict[date, list[TradeBar]]:
    """Bucket bars by their Eastern-time trading date."""
    grouped: dict[date, list[TradeBar]] = defaultdict(list)
    for bar in bars:
        et_time = bar.time.astimezone(EASTERN)
        grouped[et_time.date()].append(bar)
    # Ensure each day's bars are chronologically sorted, even if the
    # input was out of order (e.g. from a Postgres query without ORDER BY).
    for day_bars in grouped.values():
        day_bars.sort(key=lambda b: b.time)
    return grouped


def export_polygon_bars_to_lean(
    output_root: Path | str,
    symbol: str,
    bars: Iterable[dict[str, Any]] | Sequence[dict[str, Any]],
) -> list[Path]:
    """Write Polygon-style minute bars to LEAN per-day zips.

    Args:
        output_root: Root of the LEAN ``Data`` directory. The function
            will create ``{output_root}/equity/usa/minute/{symbol}/``.
        symbol: Ticker (case preserved in the ``TradeBar.symbol`` field,
            lowercased in the zip path by ``write_lean_day_zip``).
        bars: Iterable of Polygon aggregate dicts.

    Returns:
        Sorted list of the zip file paths that were written (one per day).
    """
    trade_bars = [_polygon_bar_to_trade_bar(symbol, b) for b in bars]
    if not trade_bars:
        logger.warning("[LEAN EXPORT] No bars supplied for %s; nothing written", symbol)
        return []

    grouped = _group_by_trading_date(trade_bars)
    written: list[Path] = []
    for trading_date in sorted(grouped.keys()):
        day_bars = grouped[trading_date]
        zip_path = write_lean_day_zip(
            output_root=output_root,
            symbol=symbol,
            trading_date=trading_date,
            bars=day_bars,
        )
        written.append(zip_path)
        logger.info(
            "[LEAN EXPORT] Wrote %d bars to %s",
            len(day_bars),
            zip_path,
        )
    logger.info(
        "[LEAN EXPORT] %s: exported %d days (%d bars total)",
        symbol,
        len(written),
        len(trade_bars),
    )
    return written


def export_polygon_range_to_lean(
    polygon: Any,
    output_root: Path | str,
    symbol: str,
    from_date: str,
    to_date: str,
    adjusted: bool = True,
) -> list[Path]:
    """Fetch a date range from Polygon and write it to LEAN zips.

    Small convenience wrapper around ``fetch_bars_chunked`` +
    ``export_polygon_bars_to_lean``. Kept in this module (rather than the
    router) so it can be called from scripts and tests without spinning
    up FastAPI.
    """
    # Imported lazily to avoid pulling the dataset_service / Polygon
    # stack when the exporter is used in tests with pre-supplied bars.
    from app.services.dataset_service import fetch_bars_chunked

    bars = fetch_bars_chunked(
        polygon=polygon,
        ticker=symbol,
        from_date=from_date,
        to_date=to_date,
        timespan="minute",
        multiplier=1,
        adjusted=adjusted,
    )
    return export_polygon_bars_to_lean(output_root, symbol, bars)
