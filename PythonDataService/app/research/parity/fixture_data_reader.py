"""CSV-backed data reader for offline parity tests.

Reads an OHLCV CSV exported from QC Cloud's ``qb.history(...)`` (either
``Resolution.DAILY`` or ``Resolution.MINUTE``) and yields ``TradeBar``
records the backtest engine consumes via ``data_source_factory`` in
``app/research/runs/runner.py``. Storing prices as ``Decimal`` matches
the engine's internal precision and avoids float drift in long indicator
recursions.

Bar semantics:

- **Daily-resolution CSV** (``time`` column is ``YYYY-MM-DD``): ``time`` is
  anchored to the NYSE session open (``09:30 America/New_York``) and
  ``end_time`` to the close (``16:00 America/New_York``). Mirrors LEAN's
  daily-bar convention; matches what ``NEXT_BAR_OPEN`` keys off.
- **Minute-resolution CSV** (``time`` column is ``YYYY-MM-DD HH:MM:SS``):
  ``time`` is the literal minute start, ``end_time = time + 1 minute``.
  The captured times are NY-local (QC's convention); we attach the
  ``America/New_York`` tzinfo without shifting.

The resolution is auto-detected from the data: if any row has a non-zero
time-of-day component, the entire CSV is treated as minute resolution.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from app.engine.data.trade_bar import TradeBar

_NY = ZoneInfo("America/New_York")
_SESSION_OPEN = time(hour=9, minute=30)
_SESSION_CLOSE = time(hour=16, minute=0)


@dataclass(frozen=True)
class FixtureDataReader:
    """Yields ``TradeBar`` records read from an OHLCV CSV fixture.

    The CSV must have columns ``time,open,high,low,close,volume``. The
    ``time`` column may be either a date (``YYYY-MM-DD``) for daily bars
    or a full datetime (``YYYY-MM-DD HH:MM:SS``) for minute bars; the
    resolution is auto-detected.

    Matches the engine's ``data_source_factory`` contract:
    ``iter_bars(symbol, start, end) -> Iterator[TradeBar]``.
    """

    csv_path: Path
    symbol: str = "AAPL"
    _cached_frame: list = field(default_factory=list, repr=False, compare=False)

    def iter_bars(
        self,
        symbol: str,
        start: Date | None = None,
        end: Date | None = None,
    ) -> Iterator[TradeBar]:
        if symbol.upper() != self.symbol.upper():
            return iter(())
        frame = self._load()
        if start is not None:
            frame = frame[frame["date"] >= start]
        if end is not None:
            frame = frame[frame["date"] <= end]
        is_minute = self._is_minute_resolution(frame)
        return iter(self._to_trade_bars(frame, symbol.upper(), is_minute=is_minute))

    @property
    def is_minute_resolution(self) -> bool:
        """``True`` if the CSV's ``time`` column has any non-midnight values."""
        return self._is_minute_resolution(self._load())

    def _load(self) -> pd.DataFrame:
        frame = pd.read_csv(
            self.csv_path,
            dtype={
                "open": "float64",
                "high": "float64",
                "low": "float64",
                "close": "float64",
                "volume": "int64",
            },
        )
        # Preserve the full datetime when present; pandas naively parses
        # "YYYY-MM-DD HH:MM:SS" as a Timestamp at that wall-clock.
        frame["time_dt"] = pd.to_datetime(frame["time"], utc=False, errors="raise")
        frame["date"] = frame["time_dt"].dt.date
        return frame.sort_values("time_dt").reset_index(drop=True)

    @staticmethod
    def _is_minute_resolution(frame: pd.DataFrame) -> bool:
        # Any row with non-zero time-of-day component implies sub-daily.
        return bool(
            (
                (frame["time_dt"].dt.hour != 0) | (frame["time_dt"].dt.minute != 0) | (frame["time_dt"].dt.second != 0)
            ).any()
        )

    @staticmethod
    def _to_trade_bars(frame: pd.DataFrame, symbol: str, *, is_minute: bool) -> list[TradeBar]:
        bars: list[TradeBar] = []
        for row in frame.itertuples(index=False):
            if is_minute:
                # Minute bar: literal timestamp from CSV, attached to NY tz.
                # QC's qb.history returns timestamps in NY-local wall-clock
                # for US equities; preserve that without shifting.
                bar_start = row.time_dt.to_pydatetime().replace(tzinfo=_NY)
                bar_end = bar_start + timedelta(minutes=1)
            else:
                bar_start = datetime.combine(row.date, _SESSION_OPEN, tzinfo=_NY)
                bar_end = datetime.combine(row.date, _SESSION_CLOSE, tzinfo=_NY)
            bars.append(
                TradeBar(
                    symbol=symbol,
                    time=bar_start,
                    end_time=bar_end,
                    open=Decimal(str(row.open)),
                    high=Decimal(str(row.high)),
                    low=Decimal(str(row.low)),
                    close=Decimal(str(row.close)),
                    volume=int(row.volume),
                )
            )
        return bars

    def trading_dates(self) -> list[Date]:
        """Calendar dates present in the CSV, sorted ascending and de-duped."""
        return sorted(set(self._load()["date"]))

    def bar_open_by_date(self, symbol: str) -> dict[Date, Decimal]:
        """Map trading date → first bar's ``open`` for the configured symbol.

        For daily CSVs this is the daily session open; for minute CSVs it's
        the open of the first minute bar of the trading day (typically the
        09:30 ET bar). The reconciler's daily audit step uses this; the
        minute audit step uses :py:meth:`find_bar_containing` instead.
        """
        if symbol.upper() != self.symbol.upper():
            return {}
        frame = self._load()
        first_per_day = frame.groupby("date", sort=True).first()
        return {d: Decimal(str(row.open)) for d, row in first_per_day.iterrows()}

    def find_bar_containing(self, symbol: str, fill_time_ms: int) -> TradeBar | None:
        """Return the bar whose ``[time, end_time)`` contains ``fill_time_ms``.

        Used by the reconciler's fixture-audit step for minute-resolution
        fixtures: a QC fill at 09:31:17 ET should look up the 09:31 minute
        bar and check that the fill price falls within ``[low, high]``.

        For daily CSVs, returns the daily bar for the fill's NY-local date
        (since the daily bar's window covers the whole session).
        """
        if symbol.upper() != self.symbol.upper():
            return None
        fill_dt = datetime.fromtimestamp(fill_time_ms / 1000, tz=_NY)
        frame = self._load()
        is_minute = self._is_minute_resolution(frame)
        # Filter to the fill's date first (much smaller search space)
        same_day = frame[frame["date"] == fill_dt.date()]
        if same_day.empty:
            return None
        bars = self._to_trade_bars(same_day, symbol.upper(), is_minute=is_minute)
        for bar in bars:
            if bar.time <= fill_dt < bar.end_time:
                return bar
        return None


def fixture_data_source_factory(csv_path: Path, *, symbol: str = "AAPL"):
    """Adapter producing a ``data_source_factory`` callable for ``run_strategy_spec``.

    The runner signature is ``(symbol, start_date, end_date) -> reader``. The
    ``FixtureDataReader`` ignores ``(start_date, end_date)`` for construction
    and applies the window inside ``iter_bars``; we close over the CSV path
    and rebuild the reader per call so the factory is stateless.
    """
    reader = FixtureDataReader(csv_path=Path(csv_path), symbol=symbol)

    def _factory(_symbol: str, _start: Date, _end: Date) -> FixtureDataReader:
        return reader

    return _factory


__all__ = ["FixtureDataReader", "fixture_data_source_factory"]
