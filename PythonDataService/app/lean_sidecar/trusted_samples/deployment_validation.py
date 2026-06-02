"""Deployment-validation trusted template for the LEAN Sidecar Lab.

Companion to
``PythonDataService/app/engine/strategy/algorithms/deployment_validation.py``.
The rule is intentionally fixed: start at 09:45 ET, require two consecutive
green minute bars (close > open), enter on the following bar, submit the exit
on the fifth bar, reset detection state, and stop/re-flatten at 15:45 ET.
"""

from __future__ import annotations

DEPLOYMENT_VALIDATION_SOURCE = '''\
from AlgorithmImports import *
from datetime import time
from zoneinfo import ZoneInfo


_ET = ZoneInfo("America/New_York")


def _to_ms_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp() * 1000)


class MyAlgorithm(QCAlgorithm):
    """Two-green-minute deployment validation strategy."""

    START_AFTER = time(9, 45)
    STOP_AND_FLATTEN = time(15, 45)
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

        signal = "HOLD"
        bar_time = bar.EndTime.time()

        if bar_time >= self.STOP_AND_FLATTEN:
            self.stopped_for_day = True
            self._reset_detection()
            if self.in_trade or self.entry_pending or self.Portfolio[self.symbol].Invested:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_until_exit = 0
                signal = "SESSION_FLATTEN"
            self._write_state(bar, signal)
            return

        if self.stopped_for_day or bar_time < self.START_AFTER:
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
