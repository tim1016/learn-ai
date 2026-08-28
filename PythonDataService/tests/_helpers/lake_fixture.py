"""Helpers for seeding a fixture data lake in tests.

The artifacts are written by the lake's own writers
(:mod:`app.data_lake.lean_writer`, :mod:`app.data_lake.derived_quote`,
:mod:`app.data_lake.derived_daily`) at the paths the lake's own path
policy dictates, so a seeded fixture lake is byte-for-byte the shape a
real ``ensure_data`` run produces — just without Postgres or Polygon.

The minute bars come from :func:`tests._helpers.lean_store.make_minute_bars`,
the same generator that seeds the policy-keyed bar store, so a test can
seed both substrates from one source of truth and compare them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from app.data_lake.derived_daily import aggregate_minute_to_daily, build_daily_zip_bytes
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
)
from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import datetime_at_ms
from tests._helpers.lean_store import make_minute_bars

EASTERN = ZoneInfo("America/New_York")


def to_lake_bars(bars: list[TradeBar]) -> list[MinuteTradeBar]:
    """Convert engine ``TradeBar``s into the lake writer's input type.

    ``TradeBar`` carries int64 ms UTC; ``MinuteTradeBar`` carries the
    ET wall clock the LEAN CSV encodes. The conversion is the same one
    the lake's Polygon fetcher performs, kept here so the fixture and
    the production writer agree on bar-start semantics.
    """
    return [
        MinuteTradeBar(
            bar_start_et=datetime_at_ms(bar.start_ms, tz=EASTERN),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in bars
    ]


def _write(lake_root: Path, relative: Path, payload: bytes) -> Path:
    target = lake_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def seed_lake_minute_day(
    lake_root: Path,
    symbol: str,
    trading_date: date,
    *,
    count: int = 390,
    with_quote: bool = True,
) -> tuple[Path, Path | None]:
    """Write one day's minute-trade (and optionally quote) artifact.

    Returns ``(trade_zip_path, quote_zip_path_or_None)``.
    """
    lake_bars = to_lake_bars(make_minute_bars(symbol, trading_date, count=count))
    yyyymmdd = trading_date.strftime("%Y%m%d")

    trade_relative = Path(
        *LeanMinuteBarPath(
            market="usa",
            symbol=symbol,
            trading_date=trading_date,
            data_type="trade",
        )
        .relative_path()
        .parts
    )
    trade_path = _write(
        lake_root,
        trade_relative,
        build_minute_trade_zip_bytes(symbol, yyyymmdd, lake_bars),
    )

    quote_path: Path | None = None
    if with_quote:
        quote_relative = Path(
            *LeanMinuteBarPath(
                market="usa",
                symbol=symbol,
                trading_date=trading_date,
                data_type="quote",
            )
            .relative_path()
            .parts
        )
        quote_path = _write(
            lake_root,
            quote_relative,
            build_minute_quote_zip_bytes(symbol, yyyymmdd, lake_bars),
        )
    return trade_path, quote_path


def seed_lake_daily(
    lake_root: Path,
    symbol: str,
    trading_dates: list[date],
    *,
    count: int = 390,
) -> Path:
    """Write the symbol's daily artifact, aggregated from the same bars."""
    lake_bars: list[MinuteTradeBar] = []
    for trading_date in trading_dates:
        lake_bars.extend(to_lake_bars(make_minute_bars(symbol, trading_date, count=count)))
    relative = Path(*LeanDailyBarPath(market="usa", symbol=symbol).relative_path().parts)
    return _write(
        lake_root,
        relative,
        build_daily_zip_bytes(symbol=symbol, aggregates=aggregate_minute_to_daily(lake_bars)),
    )


def seed_lake_metadata(lake_root: Path) -> tuple[Path, Path]:
    """Write placeholder LEAN metadata databases at their lake paths.

    Content is irrelevant to the mount and manifest seams under test —
    only presence and hashability are — so these stay tiny rather than
    dragging the multi-megabyte real databases into a unit test.
    """
    market_hours = _write(
        lake_root,
        Path(*LeanMetadataPath(kind="market_hours").relative_path().parts),
        b'{"entries": {}}\n',
    )
    symbol_properties = _write(
        lake_root,
        Path(*LeanMetadataPath(kind="symbol_properties").relative_path().parts),
        b"usa,spy,equity,SPY,USD,1,0.01,1\n",
    )
    return market_hours, symbol_properties


def seed_lake_corporate_actions(
    lake_root: Path,
    symbol: str,
    *,
    factor_rows: str = "20260105,1,1\n",
    map_rows: str | None = None,
) -> tuple[Path, Path]:
    """Write the symbol's factor and map files at their lake paths.

    The contents are only ever hashed by the tests that use this, never
    parsed, so the rows stay minimally plausible rather than realistic.
    Returns ``(factor_file_path, map_file_path)``.
    """
    factor = _write(
        lake_root,
        Path(*LeanFactorFilePath(market="usa", symbol=symbol).relative_path().parts),
        factor_rows.encode("ascii"),
    )
    mapping = _write(
        lake_root,
        Path(*LeanMapFilePath(market="usa", symbol=symbol).relative_path().parts),
        (map_rows if map_rows is not None else f"19980102,{symbol.lower()}\n").encode("ascii"),
    )
    return factor, mapping


def seed_lake_window(
    lake_root: Path,
    symbol: str,
    trading_dates: list[date],
    *,
    count: int = 390,
    with_quote: bool = True,
    with_metadata: bool = True,
) -> list[Path]:
    """Seed a complete, runnable fixture lake; returns the trade zips."""
    trade_paths = [
        seed_lake_minute_day(lake_root, symbol, trading_date, count=count, with_quote=with_quote)[0]
        for trading_date in trading_dates
    ]
    seed_lake_daily(lake_root, symbol, trading_dates, count=count)
    if with_metadata:
        seed_lake_metadata(lake_root)
    return trade_paths
