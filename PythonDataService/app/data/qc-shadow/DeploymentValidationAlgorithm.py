"""DeploymentValidationAlgorithm — QuantConnect (LEAN) audit copy.

This is the committed audit copy for the ``deployment_validation`` paper
deployment gate. Upload this file to QuantConnect Cloud and bind the resulting
backtest id to the live deployment form.

The canonical Python implementation is
``PythonDataService/app/engine/strategy/algorithms/deployment_validation.py``.
This QC copy mirrors the deployment-validation intent: start detecting 15
minutes after the session's scheduled open, require two consecutive green
minute bars (``close > open``), enter on the next bar, submit liquidation on
the fifth bar, reset detection after exits, allow multiple trades per day,
and stop/flatten 15 minutes before the session's scheduled close. On a
regular session that resolves to 09:45/15:45 ET; on an NYSE half day (13:00
ET close) both cutoffs shift so the stop/flatten barrier stays reachable
within the session (#1672) — derived from ``Securities[symbol].Exchange.Hours``
rather than a fixed wall-clock literal.

LEAN note: this audit copy submits entries with ``SetHoldings`` from ``OnData``
after the two-bar confirmation. It is the committed source artifact bound to the
QuantConnect backtest gate; exact fill-price parity is checked by the Python
engine, whose deployment run uses ``fill_mode=next_bar_open``.
"""

# ruff: noqa: F403, F405
from AlgorithmImports import *  # noqa: F401
from datetime import date, datetime, timedelta


class DeploymentValidationAlgorithm(QCAlgorithm):  # type: ignore[name-defined]
    DETECTION_START_OFFSET = timedelta(minutes=15)
    STOP_AND_FLATTEN_OFFSET = timedelta(minutes=15)
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
        self._detection_start = None
        self._stop_and_flatten = None

    def _reset_detection(self) -> None:
        self._green_streak = 0
        self._entry_pending = False

    def _reset_day(self) -> None:
        self._reset_detection()
        self._stopped_for_day = False

    def _session_window(self, bar_date: date) -> tuple[datetime, datetime]:
        """Return ``(detection_start, stop_and_flatten)`` for ``bar_date``.

        Derived from the exchange's own scheduled session hours, not a fixed
        wall-clock literal, so an NYSE half day gets a real, reachable
        stop/flatten barrier (#1672).
        """
        midnight = datetime(bar_date.year, bar_date.month, bar_date.day)
        hours = self.Securities[self.spy].Exchange.Hours
        session_open = hours.GetNextMarketOpen(midnight, False)
        session_close = hours.GetNextMarketClose(midnight, False)
        return session_open + self.DETECTION_START_OFFSET, session_close - self.STOP_AND_FLATTEN_OFFSET

    def OnData(self, slice) -> None:
        bar = slice.Bars.get(self.spy)
        if bar is None:
            return

        bar_date = bar.EndTime.date()
        if self._current_date is None or bar_date != self._current_date:
            self._current_date = bar_date
            self._reset_day()
            self._detection_start, self._stop_and_flatten = self._session_window(bar_date)

        if bar.EndTime >= self._stop_and_flatten:
            self._stopped_for_day = True
            self._reset_detection()
            if self._in_position or self._entry_pending or self.Portfolio[self.spy].Invested:
                self.Liquidate(self.spy)
                self._in_position = False
                self._bars_until_exit = 0
            return

        if self._stopped_for_day or bar.EndTime < self._detection_start:
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
