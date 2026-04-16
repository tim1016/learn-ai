"""TradeBarConsolidator — mirrors LEAN's Common/Data/Consolidators/TradeBarConsolidator.cs.

Aggregates fine-resolution TradeBars (e.g., minute) into coarser bars
(e.g., 15 minute). Implements the same rounding and firing semantics as
LEAN so consolidated bar boundaries match exactly.

Key behaviors reproduced from LEAN:
  * ``bar.time`` is floor-rounded to the period (``dateTime.Ticks % interval.Ticks``).
    With a 15-minute period, bars align to wall-clock :00 :15 :30 :45 — this
    happens to coincide with US equity market open at 09:30 but the alignment
    is purely based on the absolute time, not session start.
  * A consolidated bar fires when a new input bar arrives that belongs to a
    later rounded bar start (``GetRoundedBarTime(input) > working_bar.time``).
  * Bars are closed on the left: ``[start, start + period)``. A minute bar
    whose start equals ``start + period`` belongs to the *next* consolidated
    bar, not the current one.
  * The consolidated bar's ``close`` is the close of the last input bar it
    contained, its ``high``/``low`` are the max/min across all input bars,
    its ``volume`` is the sum, and its ``end_time`` is the ``end_time`` of
    the last contained input bar.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from app.engine.data.trade_bar import TradeBar


def _floor_to_period(dt: datetime, period: timedelta) -> datetime:
    """Floor-round a timezone-aware datetime to a period.

    Mirrors LEAN's ``dateTime.Ticks - (dateTime.Ticks % interval.Ticks)``.
    Uses the Unix epoch as the zero reference, which is equivalent modulo
    any period that divides 1 day evenly (all periods we care about).
    """
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    period_seconds = int(period.total_seconds())
    delta_seconds = int(delta.total_seconds())
    # Floor by whole seconds (LEAN periods we care about are whole seconds).
    floored_seconds = (delta_seconds // period_seconds) * period_seconds
    return epoch + timedelta(seconds=floored_seconds)


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
        self._last_emit: datetime | None = None

    def update(self, bar: TradeBar) -> TradeBar | None:
        """Feed a new input bar.

        Returns a consolidated bar if one fired as a result of this input,
        otherwise None. The fired bar is *not* the one this input started
        (that stays working until the next period boundary) — it's the
        previously-working bar that just got closed out.
        """
        rounded_start = _floor_to_period(bar.time, self.period)
        fired: TradeBar | None = None

        # Does this input bar trigger the working bar to fire?
        if self._working is not None:
            working_start: datetime = self._working["time"]
            # Fire when: we have at least period worth of data AND the input
            # belongs to a later rounded bar than the working one.
            # LEAN check (PeriodCountConsolidatorBase):
            #   data.Time - _workingBar.Time >= _period && GetRoundedBarTime(data) > _lastEmit
            time_diff = bar.time - working_start
            if time_diff >= self.period and (self._last_emit is None or rounded_start > self._last_emit):
                fired = self._emit_working()

        # Start a new working bar if needed
        if self._working is None:
            self._working = {
                "symbol": bar.symbol,
                "time": rounded_start,
                "end_time": bar.end_time,
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
            w["end_time"] = bar.end_time

        if fired is not None and self.on_data_consolidated is not None:
            self.on_data_consolidated(fired)
        return fired

    def _emit_working(self) -> TradeBar:
        """Close out the working bar, emit it, and clear the working slot."""
        assert self._working is not None
        w = self._working
        fired = TradeBar(
            symbol=w["symbol"],
            time=w["time"],
            end_time=w["end_time"],
            open=w["open"],
            high=w["high"],
            low=w["low"],
            close=w["close"],
            volume=w["volume"],
        )
        self._last_emit = w["time"]
        self._working = None
        return fired

    def scan(self, current_time: datetime) -> TradeBar | None:
        """Optionally close out the working bar at end-of-stream.

        Note: LEAN does not normally emit partial bars — the working bar is
        only closed when the next period's data arrives. We expose this hook
        for tests and end-of-backtest cleanup, but the default SPY strategy
        does not call it (and LEAN does not rely on trailing partial bars
        to generate signals).
        """
        if self._working is None:
            return None
        working_start = self._working["time"]
        if current_time - working_start >= self.period:
            fired = self._emit_working()
            if self.on_data_consolidated is not None:
                self.on_data_consolidated(fired)
            return fired
        return None
