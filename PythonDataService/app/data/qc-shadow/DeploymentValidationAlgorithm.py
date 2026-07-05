"""DeploymentValidationAlgorithm — QuantConnect (LEAN) audit copy.

This is the committed audit copy for the ``deployment_validation`` paper
deployment gate. Upload this file to QuantConnect Cloud and bind the resulting
backtest id to the live deployment form.

The canonical Python implementation is
``PythonDataService/app/engine/strategy/algorithms/deployment_validation.py``.
This QC copy mirrors the deployment-validation intent: start detecting at the
09:45 ET minute close, require two consecutive green minute bars
(``close > open``), enter on the next bar, submit liquidation on the fifth bar,
reset detection after exits, allow multiple trades per day, and stop/flatten at
15:45 ET.

LEAN note: this audit copy submits entries with ``SetHoldings`` from ``OnData``
after the two-bar confirmation. It is the committed source artifact bound to the
QuantConnect backtest gate; exact fill-price parity is checked by the Python
engine, whose deployment run uses ``fill_mode=next_bar_open``.
"""

# ruff: noqa: F403, F405
from AlgorithmImports import *  # noqa: F401
from datetime import time


class DeploymentValidationAlgorithm(QCAlgorithm):  # type: ignore[name-defined]
    START_AFTER = time(9, 45)
    STOP_AND_FLATTEN = time(15, 45)
    EXIT_BAR_COUNT = 3

    def Initialize(self) -> None:
        self.SetStartDate(2024, 3, 28)
        self.SetEndDate(2026, 4, 15)
        self.SetCash(100000)

        self.spy = self.AddEquity("SPY", Resolution.Minute).Symbol  # type: ignore[name-defined]

        self._current_date = None
        self._green_streak = 0
        self._entry_pending = False
        self._in_position = False
        self._stopped_for_day = False
        self._bars_until_exit = 0

    def _reset_detection(self) -> None:
        self._green_streak = 0
        self._entry_pending = False

    def _reset_day(self) -> None:
        self._reset_detection()
        self._stopped_for_day = False

    def OnData(self, slice) -> None:
        bar = slice.Bars.get(self.spy)
        if bar is None:
            return

        bar_date = bar.EndTime.date()
        if self._current_date is None or bar_date != self._current_date:
            self._current_date = bar_date
            self._reset_day()

        bar_time = bar.EndTime.time()
        if bar_time >= self.STOP_AND_FLATTEN:
            self._stopped_for_day = True
            self._reset_detection()
            if self._in_position or self._entry_pending or self.Portfolio[self.spy].Invested:
                self.Liquidate(self.spy)
                self._in_position = False
                self._bars_until_exit = 0
            return

        if self._stopped_for_day or bar_time < self.START_AFTER:
            self._reset_detection()
            return

        if self._entry_pending and not self._in_position:
            self.SetHoldings(self.spy, 1.0)
            self._entry_pending = False
            self._in_position = True
            self._bars_until_exit = self.EXIT_BAR_COUNT

        if self._in_position:
            self._bars_until_exit -= 1
            if self._bars_until_exit <= 0:
                self.Liquidate(self.spy)
                self._in_position = False
                self._bars_until_exit = 0
                self._reset_detection()
            return

        if bar.Close > bar.Open:
            self._green_streak += 1
        else:
            self._green_streak = 0

        if self._green_streak >= 2:
            self._entry_pending = True
            self._green_streak = 0

    def OnEndOfAlgorithm(self) -> None:
        if self.Portfolio[self.spy].Invested:
            self.Liquidate(self.spy)
