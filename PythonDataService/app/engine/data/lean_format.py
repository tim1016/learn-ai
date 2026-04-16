"""LEAN on-disk format reader and writer.

LEAN stores minute equity data as one zip file per day:

    data/equity/usa/minute/{ticker}/{YYYYMMDD}_trade.zip
    └── {YYYYMMDD}_{ticker}_minute_trade.csv
        Format (no header): ms_since_midnight,open,high,low,close,volume
        Prices are in deci-cents (price * 10000) as integers.
        Times are ms since midnight in exchange timezone (ET for US equities).

Daily equity data uses a different layout — one zip per symbol covering the
entire history:

    data/equity/usa/daily/{ticker}.zip
    └── {ticker}.csv
        Format (no header): "YYYYMMDD HH:MM",open,high,low,close,volume
        Timestamp column is always "YYYYMMDD 00:00" — session start midnight.
        Prices are in deci-cents, same scale factor as minute.

See Lean/Common/Data/Market/TradeBar.cs (_scaleFactor = 1/10000m) and
Lean/Common/Util/LeanData.cs (GenerateZipFilePath / GenerateZipEntryName).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator, Sequence
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
    """Reads LEAN-format minute equity data from one or more directory trees.

    Expected layout under each root::

        data_root/
          equity/
            usa/
              minute/
                {symbol_lower}/
                  {YYYYMMDD}_trade.zip

    The reader accepts either a single root (backward-compat) or a sequence
    of roots. When multiple roots are supplied the reader looks for each
    day's zip in the order they were given, using the first hit. This lets
    the engine overlay a writable Polygon-sourced cache on top of a
    read-only LEAN reference mount without changing any calling code.
    """

    def __init__(
        self,
        data_root: Path | str | Sequence[Path | str],
    ) -> None:
        # Normalize to a list of Paths while preserving order.
        if isinstance(data_root, (str, Path)):
            roots: list[Path] = [Path(data_root)]
        else:
            roots = [Path(r) for r in data_root]
            if not roots:
                raise ValueError("LeanMinuteDataReader requires at least one root")
        self.data_roots: list[Path] = roots
        # Preserved for backward compatibility with any code that reads the
        # ``data_root`` attribute (tests, logging). Points at the first root.
        self.data_root: Path = roots[0]

    def _symbol_dir(self, root: Path, symbol: str) -> Path:
        return root / "equity" / "usa" / "minute" / symbol.lower()

    def _zip_path(self, symbol: str, trading_date: date) -> Path:
        """Return the first existing zip across roots, or the first root's
        candidate path (non-existent) when no root has the file."""
        filename = f"{trading_date.strftime('%Y%m%d')}_trade.zip"
        candidates = [self._symbol_dir(r, symbol) / filename for r in self.data_roots]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def iter_dates(self, symbol: str, start: date, end: date) -> Iterator[date]:
        """Yield trading dates in [start, end] for which any root has a zip."""
        current = start
        one_day = timedelta(days=1)
        filename_fmt = "%Y%m%d"
        while current <= end:
            filename = f"{current.strftime(filename_fmt)}_trade.zip"
            for root in self.data_roots:
                if (self._symbol_dir(root, symbol) / filename).exists():
                    yield current
                    break
            current += one_day

    def read_day(self, symbol: str, trading_date: date) -> list[TradeBar]:
        """Read all minute bars for a single trading day."""
        zip_path = self._zip_path(symbol, trading_date)
        if not zip_path.exists():
            return []
        with zipfile.ZipFile(zip_path) as zf:
            # LEAN's filename convention: {YYYYMMDD}_{symbol}_minute_trade.csv
            expected = f"{trading_date.strftime('%Y%m%d')}_{symbol.lower()}_minute_trade.csv"
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


def _parse_daily_csv_bytes(
    csv_bytes: bytes,
    symbol: str,
) -> list[TradeBar]:
    """Parse a LEAN daily trade CSV into TradeBar objects.

    Each row has the form::

        YYYYMMDD HH:MM,open,high,low,close,volume

    The timestamp column is always ``YYYYMMDD 00:00`` — session start midnight
    in exchange time (ET). We emit a bar with ``time`` at session start and
    ``end_time`` exactly 24 hours later so that ``end_time`` marks session
    rollover — consistent with how LEAN surfaces daily bars to algorithms.
    Prices are decoded from the same deci-cent scale used for minute bars.
    """
    bars: list[TradeBar] = []
    symbol_upper = symbol.upper()
    for line in csv_bytes.decode("ascii").splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 6:
            continue
        ts, o, h, l, c, v = parts
        # ts is "YYYYMMDD HH:MM"; splitting on space gives date | time.
        date_str, _, _time_str = ts.partition(" ")
        if len(date_str) != 8:
            continue
        year = int(date_str[0:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        start = datetime(year, month, day, tzinfo=EASTERN)
        end = start + timedelta(days=1)
        bars.append(
            TradeBar(
                symbol=symbol_upper,
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


class LeanDailyDataReader:
    """Reads LEAN-format daily equity data from one or more directory trees.

    Expected layout under each root::

        data_root/
          equity/
            usa/
              daily/
                {symbol_lower}.zip
                └── {symbol_lower}.csv   (entire history for the symbol)

    Unlike :class:`LeanMinuteDataReader` where each trading day is a separate
    file, LEAN stores the full history for a symbol in a single zip. That
    changes the multi-root semantics:

    * For minute, different days can live under different roots and a
      day-by-day overlay works. First root with a hit wins per day.
    * For daily, each root has *at most one* zip per symbol, but those zips
      can cover different (possibly overlapping) date ranges. We therefore
      **merge** rows across roots, keyed by date. When the same date appears
      in multiple roots the earlier root wins, matching the
      ``LeanMinuteDataReader`` convention of "reference mount takes precedence
      over cache" when both have data for the same day.
    """

    def __init__(
        self,
        data_root: Path | str | Sequence[Path | str],
    ) -> None:
        if isinstance(data_root, (str, Path)):
            roots: list[Path] = [Path(data_root)]
        else:
            roots = [Path(r) for r in data_root]
            if not roots:
                raise ValueError("LeanDailyDataReader requires at least one root")
        self.data_roots: list[Path] = roots
        self.data_root: Path = roots[0]
        # Cache of parsed history per symbol: {symbol_upper: list[TradeBar]}.
        # Lazily populated on first access and kept for the life of the
        # reader instance; a backtest typically touches one symbol repeatedly.
        self._history_cache: dict[str, list[TradeBar]] = {}

    def _zip_path(self, root: Path, symbol: str) -> Path:
        return root / "equity" / "usa" / "daily" / f"{symbol.lower()}.zip"

    def _read_zip(self, zip_path: Path, symbol: str) -> list[TradeBar]:
        if not zip_path.exists():
            return []
        with zipfile.ZipFile(zip_path) as zf:
            expected = f"{symbol.lower()}.csv"
            names = zf.namelist()
            name = expected if expected in names else names[0]
            with zf.open(name) as f:
                return _parse_daily_csv_bytes(f.read(), symbol)

    def _load_history(self, symbol: str) -> list[TradeBar]:
        """Load the full history for ``symbol``, merging across roots.

        Results are cached per symbol. Earlier roots take precedence on
        same-date conflicts. The returned list is sorted by bar time.
        """
        key = symbol.upper()
        if key in self._history_cache:
            return self._history_cache[key]

        merged: dict[date, TradeBar] = {}
        for root in self.data_roots:
            zip_path = self._zip_path(root, symbol)
            for bar in self._read_zip(zip_path, symbol):
                bar_date = bar.time.date()
                # Earlier roots win — only insert if not already present.
                if bar_date not in merged:
                    merged[bar_date] = bar
        history = [merged[d] for d in sorted(merged.keys())]
        self._history_cache[key] = history
        return history

    def available_dates(self, symbol: str) -> list[date]:
        """Return the sorted list of trading dates present for ``symbol``."""
        return [bar.time.date() for bar in self._load_history(symbol)]

    def iter_bars(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> Iterator[TradeBar]:
        """Yield daily bars in [start, end] in chronological order.

        Dates without a corresponding row are silently skipped; this matches
        the minute reader's "absent file → empty" behavior.
        """
        for bar in self._load_history(symbol):
            bar_date = bar.time.date()
            if bar_date < start:
                continue
            if bar_date > end:
                break
            yield bar


def write_lean_daily_zip(
    output_root: Path | str,
    symbol: str,
    bars: Sequence[TradeBar],
    *,
    merge_existing: bool = True,
) -> Path:
    """Write a full-history daily zip for ``symbol`` in LEAN format.

    Args:
        output_root: Root directory (will create ``equity/usa/daily/``).
        symbol: Ticker symbol (case-insensitive; lowercased in the path).
        bars: Daily TradeBars to write. Must all be daily-resolution bars
            in ET; ``time`` is expected at 00:00 ET for each trading date.
        merge_existing: If ``True`` (the default) and a zip already exists
            at the target path, existing rows are read first and then
            unioned with ``bars`` by date. Newer (``bars``-provided) values
            win on conflict — matching the cache's "Polygon is the source
            of truth for whatever we just fetched" convention.

    Returns:
        Path to the written zip file.
    """
    out_dir = Path(output_root) / "equity" / "usa" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{symbol.lower()}.zip"
    csv_name = f"{symbol.lower()}.csv"

    # Merge with any existing file so partial-range fetches don't clobber
    # previously cached history.
    merged: dict[date, TradeBar] = {}
    if merge_existing and zip_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            name = csv_name if csv_name in names else names[0]
            with zf.open(name) as f:
                for existing in _parse_daily_csv_bytes(f.read(), symbol):
                    merged[existing.time.date()] = existing
    # New bars overwrite existing rows for the same date.
    for bar in bars:
        merged[bar.time.date()] = bar

    lines: list[str] = []
    for bar_date in sorted(merged.keys()):
        bar = merged[bar_date]
        ts = f"{bar_date.strftime('%Y%m%d')} 00:00"
        lines.append(
            f"{ts},"
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
    out_dir = Path(output_root) / "equity" / "usa" / "minute" / symbol.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    csv_name = f"{trading_date.strftime('%Y%m%d')}_{symbol.lower()}_minute_trade.csv"
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
