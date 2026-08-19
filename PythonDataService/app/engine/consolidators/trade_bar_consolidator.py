"""TradeBarConsolidator — mirrors LEAN's Common/Data/Consolidators/TradeBarConsolidator.cs.

Aggregates fine-resolution TradeBars (e.g., minute) into coarser bars
(e.g., 15 minute). Implements the same rounding and firing semantics as
LEAN so consolidated bar boundaries match exactly.

Key behaviors reproduced from LEAN:
  * ``bar.start_ms`` is floor-rounded to the period (``dateTime.Ticks % interval.Ticks``).
    LEAN's reference consolidator floors a naive, exchange-local ``DateTime``
    — its inputs already carry ET wall time with no UTC conversion in the
    loop. This port floors the equivalent America/New_York wall-clock
    reading instead of the raw UTC ms (see ``_floor_to_period_ms``), so a
    15-minute period aligns to wall-clock :00 :15 :30 :45 ET and a
    one-day-or-longer period aligns to ET midnight, not UTC midnight.
  * A consolidated bar fires when a new input bar arrives that belongs to a
    later rounded bar start (``GetRoundedBarTime(input) > working_bar.start_ms``).
  * Bars are closed on the left: ``[start, start + period)``. A minute bar
    whose start equals ``start + period`` belongs to the *next* consolidated
    bar, not the current one.
  * The consolidated bar's ``close`` is the close of the last input bar it
    contained, its ``high``/``low`` are the max/min across all input bars,
    its ``volume`` is the sum, and its ``end_ms`` is the ``end_ms`` of
    the last contained input bar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.utils.timestamps import ny_datetime, to_ms_utc

_EASTERN = ZoneInfo("America/New_York")
_EPOCH_NAIVE = datetime(1970, 1, 1)


def _floor_to_period_ms(timestamp_ms: int, period: timedelta) -> int:
    """Floor-round a Unix-millisecond timestamp to a whole-millisecond period,
    aligned to the America/New_York wall clock rather than raw UTC ms.

    Read the ET wall-clock reading for ``timestamp_ms``, floor it as if it
    were itself an epoch offset, then convert the floored wall-clock
    reading back to ``int64 ms UTC`` — this is what LEAN's floor of a
    naive, already-exchange-local ``DateTime`` amounts to. For any period
    under one day this is numerically identical to flooring the absolute
    UTC ms directly (the ET-UTC offset is always a whole number of hours,
    hence always a whole multiple of any period that evenly divides an
    hour). For a period of one day or longer it is not: flooring raw UTC ms
    lands on UTC midnight, mislabeling a session's bars with the previous
    ET trading date.
    """
    period_ms = int(period.total_seconds() * 1000)
    if period_ms <= 0:
        raise ValueError("period must contain at least one millisecond")
    naive_et = ny_datetime(timestamp_ms).replace(tzinfo=None)
    naive_et_ms = int((naive_et - _EPOCH_NAIVE).total_seconds() * 1000)
    floored_naive_et_ms = (naive_et_ms // period_ms) * period_ms
    floored_naive_et = _EPOCH_NAIVE + timedelta(milliseconds=floored_naive_et_ms)
    return to_ms_utc(floored_naive_et.replace(tzinfo=_EASTERN))


class TradeBarConsolidator:
    """Consolidates TradeBars into larger-period TradeBars.

    Usage::

        consolidator = TradeBarConsolidator(timedelta(minutes=15))
        consolidator.on_data_consolidated = lambda bar: strategy.on_bar(bar)
        for minute_bar in stream:
            consolidator.update(minute_bar)
        consolidator.scan(final_time)  # flush trailing partial bar at end
    """

    def __init__(self, period: timedelta) -> None:
        if period.total_seconds() <= 0:
            raise ValueError("period must be positive")
        self.period = period
        self.on_data_consolidated: Callable[[TradeBar], None] | None = None
        self._working: dict | None = None
        self._last_emit_ms: int | None = None

    def update(self, bar: TradeBar) -> TradeBar | None:
        """Feed a new input bar.

        Returns a consolidated bar if one fired as a result of this input,
        otherwise None. The fired bar is *not* the one this input started
        (that stays working until the next period boundary) — it's the
        previously-working bar that just got closed out.
        """
        rounded_start_ms = _floor_to_period_ms(bar.start_ms, self.period)
        fired: TradeBar | None = None

        # Does this input bar trigger the working bar to fire?
        if self._working is not None:
            working_start_ms: int = self._working["start_ms"]
            # Fire when: we have at least period worth of data AND the input
            # belongs to a later rounded bar than the working one.
            # LEAN check (PeriodCountConsolidatorBase):
            #   data.Time - _workingBar.Time >= _period && GetRoundedBarTime(data) > _lastEmit
            time_diff_ms = bar.start_ms - working_start_ms
            period_ms = int(self.period.total_seconds() * 1000)
            if time_diff_ms >= period_ms and (
                self._last_emit_ms is None or rounded_start_ms > self._last_emit_ms
            ):
                fired = self._emit_working()

        # Start a new working bar if needed
        if self._working is None:
            self._working = {
                "symbol": bar.symbol,
                "start_ms": rounded_start_ms,
                "end_ms": bar.end_ms,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        else:
            # Aggregate into existing working bar
            w = self._working
            if bar.high > w["high"]:
                w["high"] = bar.high
            if bar.low < w["low"]:
                w["low"] = bar.low
            w["close"] = bar.close
            w["volume"] += bar.volume
            w["end_ms"] = bar.end_ms

        if fired is not None and self.on_data_consolidated is not None:
            self.on_data_consolidated(fired)
        return fired

    def _emit_working(self) -> TradeBar:
        """Close out the working bar, emit it, and clear the working slot."""
        assert self._working is not None
        w = self._working
        fired = TradeBar(
            symbol=w["symbol"],
            start_ms=w["start_ms"],
            end_ms=w["end_ms"],
            open=w["open"],
            high=w["high"],
            low=w["low"],
            close=w["close"],
            volume=w["volume"],
        )
        self._last_emit_ms = w["start_ms"]
        self._working = None
        return fired

    def scan(self, current_time_ms: int) -> TradeBar | None:
        """Optionally close out the working bar at end-of-stream.

        Note: LEAN does not normally emit partial bars — the working bar is
        only closed when the next period's data arrives. We expose this hook
        for tests and end-of-backtest cleanup, but the default SPY strategy
        does not call it (and LEAN does not rely on trailing partial bars
        to generate signals).
        """
        if self._working is None:
            return None
        working_start_ms = self._working["start_ms"]
        if current_time_ms - working_start_ms >= int(self.period.total_seconds() * 1000):
            fired = self._emit_working()
            if self.on_data_consolidated is not None:
                self.on_data_consolidated(fired)
            return fired
        return None
