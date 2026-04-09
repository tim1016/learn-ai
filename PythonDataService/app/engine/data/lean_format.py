"""LEAN on-disk format reader and writer.

LEAN stores minute equity data as one zip file per day:

    data/equity/usa/minute/{ticker}/{YYYYMMDD}_trade.zip
    └── {YYYYMMDD}_{ticker}_minute_trade.csv
        Format (no header): ms_since_midnight,open,high,low,close,volume
        Prices are in deci-cents (price * 10000) as integers.
        Times are ms since midnight in exchange timezone (ET for US equities).

See Lean/Common/Data/Market/TradeBar.cs (_scaleFactor = 1/10000m).
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar

# LEAN's price scale factor: prices on disk are multiplied by 10000.
PRICE_SCALE = Decimal(10000)

# US equities are stored in Eastern Time.
EASTERN = ZoneInfo("America/New_York")


def _parse_csv_bytes(
    csv_bytes: bytes,
    symbol: str,
    trading_date: date,
) -> list[TradeBar]:
    """Parse a LEAN minute trade CSV into TradeBar objects.

    Args:
        csv_bytes: Raw CSV content (no header).
        symbol: Uppercase ticker symbol to stamp on each bar.
        trading_date: The date the bars belong to (from the filename).

    Returns:
        List of TradeBar objects, one per row, timezone-aware in ET.
    """
    bars: list[TradeBar] = []
    # Midnight ET on the trading date.
    midnight = datetime(
        trading_date.year,
        trading_date.month,
        trading_date.day,
        tzinfo=EASTERN,
    )

    # Decode and split. LEAN CSVs use Unix or Windows line endings; splitlines handles both.
    for line in csv_bytes.decode("ascii").splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 6:
            continue
        ms, o, h, l, c, v = parts
        # Bar start time: midnight + ms
        start = midnight + timedelta(milliseconds=int(ms))
        # LEAN minute bars have a 1-minute period.
        end = start + timedelta(minutes=1)
        bars.append(
            TradeBar(
                symbol=symbol,
                time=start,
                end_time=end,
                open=Decimal(o) / PRICE_SCALE,
                high=Decimal(h) / PRICE_SCALE,
                low=Decimal(l) / PRICE_SCALE,
                close=Decimal(c) / PRICE_SCALE,
                volume=int(v),
            )
        )
    return bars


class LeanMinuteDataReader:
    """Reads LEAN-format minute equity data from a directory tree.

    Expected layout under ``data_root``::

        data_root/
          equity/
            usa/
              minute/
                {symbol_lower}/
                  {YYYYMMDD}_trade.zip
    """

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)

    def _symbol_dir(self, symbol: str) -> Path:
        return (
            self.data_root
            / "equity"
            / "usa"
            / "minute"
            / symbol.lower()
        )

    def _zip_path(self, symbol: str, trading_date: date) -> Path:
        return (
            self._symbol_dir(symbol)
            / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
        )

    def iter_dates(self, symbol: str, start: date, end: date) -> Iterator[date]:
        """Yield trading dates in [start, end] for which a zip file exists."""
        current = start
        one_day = timedelta(days=1)
        while current <= end:
            if self._zip_path(symbol, current).exists():
                yield current
            current += one_day

    def read_day(self, symbol: str, trading_date: date) -> list[TradeBar]:
        """Read all minute bars for a single trading day."""
        zip_path = self._zip_path(symbol, trading_date)
        if not zip_path.exists():
            return []
        with zipfile.ZipFile(zip_path) as zf:
            # LEAN's filename convention: {YYYYMMDD}_{symbol}_minute_trade.csv
            expected = (
                f"{trading_date.strftime('%Y%m%d')}_"
                f"{symbol.lower()}_minute_trade.csv"
            )
            # Fall back to the first file in the archive if the name differs.
            names = zf.namelist()
            name = expected if expected in names else names[0]
            with zf.open(name) as f:
                return _parse_csv_bytes(f.read(), symbol.upper(), trading_date)

    def iter_bars(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> Iterator[TradeBar]:
        """Yield all minute bars in [start, end] in chronological order.

        Days without a corresponding zip file are skipped. Bars within a day
        are returned in the order they appear in the CSV (chronological by
        construction of the LEAN format).
        """
        for trading_date in self.iter_dates(symbol, start, end):
            yield from self.read_day(symbol, trading_date)


def write_lean_day_zip(
    output_root: Path | str,
    symbol: str,
    trading_date: date,
    bars: list[TradeBar],
) -> Path:
    """Write a list of TradeBars for one day to a LEAN-format zip.

    Args:
        output_root: Root directory (will create equity/usa/minute/{symbol}/).
        symbol: Ticker symbol (case-insensitive, lowercased in the path).
        trading_date: The trading date for this file.
        bars: TradeBars to write. Must all belong to ``trading_date`` in ET.

    Returns:
        Path to the written zip file.
    """
    out_dir = (
        Path(output_root) / "equity" / "usa" / "minute" / symbol.lower()
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    csv_name = (
        f"{trading_date.strftime('%Y%m%d')}_"
        f"{symbol.lower()}_minute_trade.csv"
    )
    midnight = datetime(
        trading_date.year,
        trading_date.month,
        trading_date.day,
        tzinfo=EASTERN,
    )
    lines: list[str] = []
    for bar in bars:
        bar_time_et = bar.time.astimezone(EASTERN)
        ms = int((bar_time_et - midnight).total_seconds() * 1000)
        lines.append(
            f"{ms},"
            f"{int(bar.open * PRICE_SCALE)},"
            f"{int(bar.high * PRICE_SCALE)},"
            f"{int(bar.low * PRICE_SCALE)},"
            f"{int(bar.close * PRICE_SCALE)},"
            f"{bar.volume}"
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(csv_name, "\n".join(lines))
    zip_path.write_bytes(buf.getvalue())
    return zip_path
