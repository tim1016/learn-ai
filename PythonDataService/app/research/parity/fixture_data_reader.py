"""CSV-backed data reader for offline parity tests.

Reads a daily-OHLCV CSV (the shape of ``qb.history(symbol, Resolution.DAILY)``
exports captured from QC Cloud) and yields ``TradeBar`` records that the
backtest engine consumes via ``data_source_factory`` in
``app/research/runs/runner.py``. Storing prices as ``Decimal`` matches the
engine's internal precision and avoids float drift in long indicator
recursions.

Bar semantics — for the daily-resolution captures used by Phase 3 QC parity:
``time`` is anchored to the NYSE session open (``09:30 America/New_York``)
and ``end_time`` to the close (``16:00 America/New_York``). This mirrors
LEAN's daily-bar convention and is what the ``NEXT_BAR_OPEN`` fill model
keys off when computing the next session's open price.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time
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
    """Yields ``TradeBar`` records read from a daily-OHLCV CSV fixture.

    The CSV must have columns ``time,open,high,low,close,volume``. ``time``
    is parsed as a calendar date (no time component, no timezone), then
    anchored at the NYSE session open / close to produce timezone-aware
    ``datetime`` fields on the emitted ``TradeBar``.

    The reader matches the engine's ``data_source_factory`` contract:
    ``iter_bars(symbol, start, end) -> Iterator[TradeBar]``. A factory wired
    into ``run_strategy_spec`` returns one of these per ``(symbol, start, end)``
    triple (the fixture is keyed by symbol at construction time).
    """

    csv_path: Path
    symbol: str = "AAPL"

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
        return iter(self._to_trade_bars(frame, symbol.upper()))

    def _load(self) -> pd.DataFrame:
        frame = pd.read_csv(
            self.csv_path,
            dtype={"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "int64"},
        )
        frame["date"] = pd.to_datetime(frame["time"], utc=False).dt.date
        return frame.sort_values("date").reset_index(drop=True)

    @staticmethod
    def _to_trade_bars(frame: pd.DataFrame, symbol: str) -> list[TradeBar]:
        bars: list[TradeBar] = []
        for row in frame.itertuples(index=False):
            session_open = datetime.combine(row.date, _SESSION_OPEN, tzinfo=_NY)
            session_close = datetime.combine(row.date, _SESSION_CLOSE, tzinfo=_NY)
            bars.append(
                TradeBar(
                    symbol=symbol,
                    time=session_open,
                    end_time=session_close,
                    open=Decimal(str(row.open)),
                    high=Decimal(str(row.high)),
                    low=Decimal(str(row.low)),
                    close=Decimal(str(row.close)),
                    volume=int(row.volume),
                )
            )
        return bars

    def trading_dates(self) -> list[Date]:
        """Calendar dates present in the CSV, sorted ascending."""
        return list(self._load()["date"])

    def bar_open_by_date(self, symbol: str) -> dict[Date, Decimal]:
        """Map trading date → bar ``open`` for the configured symbol.

        Convenience for the reconciler's fixture-audit step (QC fills are
        compared against the open of the trading-date bar).
        """
        if symbol.upper() != self.symbol.upper():
            return {}
        frame = self._load()
        return {row.date: Decimal(str(row.open)) for row in frame.itertuples(index=False)}


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
