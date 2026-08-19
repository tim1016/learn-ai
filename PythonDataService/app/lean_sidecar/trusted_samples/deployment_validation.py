"""Deployment-validation trusted template for the LEAN Sidecar Lab.

Companion to
``PythonDataService/app/engine/strategy/algorithms/deployment_validation.py``.
The rule: detection starts 15 minutes after the session's scheduled open and
stops/re-flattens 15 minutes before the session's scheduled close — 09:45 ET
/ 15:45 ET on a regular session, shifted on an NYSE half day so the barrier
stays reachable within the session (#1672) — require two consecutive green
minute bars (close > open), enter on the following bar, submit the exit on
the fifth bar, reset detection state.
"""

from __future__ import annotations

DEPLOYMENT_VALIDATION_SOURCE = '''\
from AlgorithmImports import *
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


_ET = ZoneInfo("America/New_York")


def _to_ms_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp() * 1000)


class MyAlgorithm(QCAlgorithm):
    """Two-green-minute deployment validation strategy."""

    DETECTION_START_OFFSET = timedelta(minutes=15)
    STOP_AND_FLATTEN_OFFSET = timedelta(minutes=15)
    EXIT_BAR_COUNT = 3

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        session = self.GetParameter("session") or "regular"
        adjustment = self.GetParameter("adjustment") or "raw"

        if adjustment != "raw":
            raise ValueError("adjustment=" + str(adjustment) + " not supported; only 'raw'")

        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)
        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)

        equity = self.AddEquity(
            symbol_str,
            Resolution.Minute,
            fillForward=False,
            extendedMarketHours=(session == "extended"),
        )
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.symbol = equity.Symbol

        self.current_date = None
        self.green_streak = 0
        self.entry_pending = False
        self.in_trade = False
        self.stopped_for_day = False
        self.bars_until_exit = 0
        self.detection_start = None
        self.stop_and_flatten = None

        self.SetBenchmark(lambda dt: 100)

        obs_path = self.ObjectStore.GetFilePath("observations.csv")
        with open(obs_path, "w") as f:
            f.write("ms_utc,open,high,low,close,volume\\n")
        self._obs_path = obs_path

        state_path = self.ObjectStore.GetFilePath("state.csv")
        with open(state_path, "w") as f:
            f.write("ts_ms_utc,open,close,green_streak,signal\\n")
        self._state_path = state_path

    def _reset_detection(self):
        self.green_streak = 0
        self.entry_pending = False

    def _reset_day(self):
        self._reset_detection()
        self.stopped_for_day = False

    def _session_window(self, bar_date):
        """Return (detection_start, stop_and_flatten) for bar_date.

        Derived from the exchange's own scheduled session hours, not a fixed
        wall-clock literal, so an NYSE half day gets a real, reachable
        stop/flatten barrier (#1672).
        """
        midnight = datetime(bar_date.year, bar_date.month, bar_date.day)
        hours = self.Securities[self.symbol].Exchange.Hours
        session_open = hours.GetNextMarketOpen(midnight, False)
        session_close = hours.GetNextMarketClose(midnight, False)
        return session_open + self.DETECTION_START_OFFSET, session_close - self.STOP_AND_FLATTEN_OFFSET

    def OnData(self, slice):
        bar = slice.Bars.get(self.symbol)
        if bar is None:
            return

        with open(self._obs_path, "a") as f:
            f.write(
                str(_to_ms_utc(bar.EndTime)) + ","
                + str(bar.Open) + ","
                + str(bar.High) + ","
                + str(bar.Low) + ","
                + str(bar.Close) + ","
                + str(bar.Volume) + "\\n"
            )

        bar_date = bar.EndTime.date()
        if self.current_date is None or bar_date != self.current_date:
            self.current_date = bar_date
            self._reset_day()
            self.detection_start, self.stop_and_flatten = self._session_window(bar_date)

        signal = "HOLD"

        if bar.EndTime >= self.stop_and_flatten:
            self.stopped_for_day = True
            self._reset_detection()
            if self.in_trade or self.entry_pending or self.Portfolio[self.symbol].Invested:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_until_exit = 0
                signal = "SESSION_FLATTEN"
            self._write_state(bar, signal)
            return

        if self.stopped_for_day or bar.EndTime < self.detection_start:
            self._reset_detection()
            self._write_state(bar, signal)
            return

        if self.entry_pending and not self.in_trade:
            self.SetHoldings(self.symbol, 1.0)
            self.entry_pending = False
            self.in_trade = True
            self.bars_until_exit = self.EXIT_BAR_COUNT
            signal = "ENTER"

        if self.in_trade:
            self.bars_until_exit -= 1
            if self.bars_until_exit <= 0:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_until_exit = 0
                self._reset_detection()
                signal = "EXIT"
            self._write_state(bar, signal)
            return

        green = bar.Close > bar.Open
        if green:
            self.green_streak += 1
        else:
            self.green_streak = 0

        if self.green_streak >= 2:
            self.entry_pending = True
            self.green_streak = 0
            signal = "ENTRY_QUEUED"

        self._write_state(bar, signal)

    def _write_state(self, bar, signal):
        with open(self._state_path, "a") as f:
            f.write(
                str(_to_ms_utc(bar.EndTime)) + ","
                + str(bar.Open) + ","
                + str(bar.Close) + ","
                + str(self.green_streak) + ","
                + signal + "\\n"
            )

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
