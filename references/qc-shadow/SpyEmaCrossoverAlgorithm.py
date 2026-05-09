"""SpyEmaCrossoverAlgorithm — QuantConnect (LEAN) port; observer copy.

This file is the *audit copy* (§ 3 of the spec) of the QC Cloud algorithm
that runs in parallel with the canonical Python implementation at
``PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py``.

This file is NOT executed by this repo's pytest. It is uploaded to QC
Cloud verbatim and run there as the QC algorithm. The QC Cloud workspace
must verify it is in sync with this file's SHA-256 (recorded in the run
ledger per § 10) before each run.

The QC Cloud copy is the *execution* copy; this checked-in audit copy is
the source of truth that PR review can reason about.

Strategy (line-for-line equivalent of the canonical Python copy):
  * 15-minute bars consolidated from minute SPY data.
  * Long-only EMA(5)/EMA(10) crossover with RSI(14) filter (Wilders).
  * Entry: fresh EMA5 > EMA10 crossover AND (ema5 - ema10) >= 0.20
           AND 50 <= RSI <= 70.
  * Position: SetHoldings(SPY, 1.0) — all-in on the signal bar.
  * Exit: after exactly 5 consolidated bars (75 minutes), Liquidate.

The QC Cloud algorithm intentionally does **not** open any IBKR
connection; it runs in QC's paper-trading simulator. Per § 5 of the
spec, the QC observer must never touch the IBKR paper account.
"""

# ruff: noqa: F403, F405
# QC Cloud injects the QCAlgorithm framework via this catch-all import.
# Outside QC Cloud the import fails — that's expected; this file is not
# meant to import inside this repo's interpreter, and the F403/F405
# noqa above acknowledges that names like QCAlgorithm /
# ExponentialMovingAverage / TradeBarConsolidator come from the star.
from AlgorithmImports import *  # noqa: F401


class SpyEmaCrossoverAlgorithm(QCAlgorithm):  # type: ignore[name-defined]
    def Initialize(self) -> None:
        # Defaults match the canonical Python algorithm's Initialize().
        # The exact dates are overridden by the backtest configuration
        # in QC Cloud — these are just the floor.
        self.SetStartDate(2024, 3, 28)
        self.SetEndDate(2026, 3, 27)
        self.SetCash(100000)

        self.spy = self.AddEquity("SPY", Resolution.Minute).Symbol  # type: ignore[name-defined]

        # Indicators — registered without auto-update so we can drive
        # them from the consolidator handler with the consolidated bar's
        # close, exactly like the canonical Python version.
        self._ema5 = ExponentialMovingAverage("EMA5", 5)  # type: ignore[name-defined]
        self._ema10 = ExponentialMovingAverage("EMA10", 10)  # type: ignore[name-defined]
        self._rsi14 = RelativeStrengthIndex(  # type: ignore[name-defined]
            "RSI14", 14, MovingAverageType.Wilders
        )

        self._prev_ema5_above_ema10 = False
        self._in_position = False
        self._bars_until_exit = 0
        self._pending_entry = None  # captured at signal time
        self._open_trade = None     # captured at fill time

        # 15-minute consolidator — feeds the strategy bar handler.
        consolidator = TradeBarConsolidator(timedelta(minutes=15))  # type: ignore[name-defined]
        consolidator.DataConsolidated += self._OnFifteenMinuteBar
        self.SubscriptionManager.AddConsolidator(self.spy, consolidator)

    def _OnFifteenMinuteBar(self, sender, bar) -> None:
        # Update indicators from the consolidated bar's close at EndTime.
        self._ema5.Update(bar.EndTime, bar.Close)
        self._ema10.Update(bar.EndTime, bar.Close)
        self._rsi14.Update(bar.EndTime, bar.Close)

        # Warmup guard.
        if not (self._ema5.IsReady and self._ema10.IsReady and self._rsi14.IsReady):
            if self._ema5.IsReady and self._ema10.IsReady:
                self._prev_ema5_above_ema10 = (
                    self._ema5.Current.Value > self._ema10.Current.Value
                )
            else:
                self._prev_ema5_above_ema10 = False
            return

        ema5_val = self._ema5.Current.Value
        ema10_val = self._ema10.Current.Value
        rsi_val = self._rsi14.Current.Value

        current_above = ema5_val > ema10_val
        ema_gap = ema5_val - ema10_val

        if self._in_position:
            self._bars_until_exit -= 1
            if self._bars_until_exit <= 0:
                self.Liquidate(self.spy)
                self.Log(
                    f"EXIT SIGNAL: {bar.EndTime:%Y-%m-%d %H:%M} Close={bar.Close:.2f}"
                )
                self._in_position = False
        else:
            fresh_crossover = current_above and not self._prev_ema5_above_ema10
            gap_ok = ema_gap >= 0.20
            rsi_ok = 50 <= rsi_val <= 70

            if fresh_crossover and gap_ok and rsi_ok:
                self._pending_entry = (ema5_val, ema10_val, rsi_val)
                self.SetHoldings(self.spy, 1.0)
                self._in_position = True
                self._bars_until_exit = 5
                self.Log(
                    f"ENTRY SIGNAL: {bar.EndTime:%Y-%m-%d %H:%M} "
                    f"Close={bar.Close:.2f} EMA5={ema5_val:.4f} "
                    f"EMA10={ema10_val:.4f} Gap={ema_gap:.4f} RSI={rsi_val:.2f}"
                )

        self._prev_ema5_above_ema10 = current_above

    def OnEndOfAlgorithm(self) -> None:
        if self._in_position:
            self.Liquidate(self.spy)
            self._in_position = False
