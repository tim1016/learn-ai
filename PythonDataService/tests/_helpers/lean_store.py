"""Helpers for seeding a policy-keyed LEAN bar store in tests.

Generates deterministic regular-session minute bars and writes them
through the canonical zip writer, so store-backed tests exercise the
same bytes-on-disk contract as production.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar

EASTERN = ZoneInfo("America/New_York")


def make_minute_bars(symbol: str, trading_date: date, *, count: int = 390) -> list[TradeBar]:
    """Deterministic RTH minute bars starting at 09:30 ET.

    Prices follow a fixed sawtooth of the bar index so any two calls
    with the same arguments produce identical bars (and therefore
    identical zips through the deterministic writer).
    """
    open_et = datetime(trading_date.year, trading_date.month, trading_date.day, 9, 30, tzinfo=EASTERN)
    bars: list[TradeBar] = []
    for i in range(count):
        start = open_et + timedelta(minutes=i)
        base = Decimal(500) + Decimal(i % 40) / 10
        bars.append(
            TradeBar(
                symbol=symbol.upper(),
                time=start,
                end_time=start + timedelta(minutes=1),
                open=base,
                high=base + Decimal("0.5"),
                low=base - Decimal("0.5"),
                close=base + Decimal("0.25"),
                volume=1_000 + i,
            )
        )
    return bars


def seed_store_day(root: Path, symbol: str, trading_date: date, *, count: int = 390) -> Path:
    """Write one deterministic day zip into a store root; returns the zip path."""
    return write_lean_day_zip(root, symbol, trading_date, make_minute_bars(symbol, trading_date, count=count))
