/**
 * Default content for the LEAN script editor — a minimal skeleton that
 * mirrors the structure of
 * ``PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py``'s
 * ``EMA_CROSSOVER_SOURCE`` constant.
 *
 * Inline rather than fetched server-side: the editor's purpose is to
 * let the operator iterate on their own algorithm. The default exists
 * only to keep the editor from showing as a blank canvas on first
 * load. Drift with the trusted-sample source is acceptable — the
 * trusted sample is a parity oracle; this string is a starter template.
 */
export const EMA_CROSSOVER_SOURCE_TEMPLATE = `from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    """EMA(5)/EMA(10) crossover on 15-minute consolidated bars."""

    FAST_PERIOD = 5
    SLOW_PERIOD = 10

    def Initialize(self):
        self.SetStartDate(2025, 1, 13)
        self.SetEndDate(2025, 1, 17)
        self.SetCash(100000)
        equity = self.AddEquity("SPY", Resolution.Minute)
        self.symbol = equity.Symbol
        self.ema_fast = self.EMA(self.symbol, self.FAST_PERIOD, Resolution.Minute)
        self.ema_slow = self.EMA(self.symbol, self.SLOW_PERIOD, Resolution.Minute)

    def OnData(self, slice):
        if not (self.ema_fast.IsReady and self.ema_slow.IsReady):
            return
        if not self.Portfolio.Invested and self.ema_fast.Current.Value > self.ema_slow.Current.Value:
            self.SetHoldings(self.symbol, 1.0)
        elif self.Portfolio.Invested and self.ema_fast.Current.Value < self.ema_slow.Current.Value:
            self.Liquidate(self.symbol)
`;
