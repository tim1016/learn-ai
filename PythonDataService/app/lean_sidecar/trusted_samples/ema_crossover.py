"""EMA(5)/EMA(10) crossover trusted template — LEAN parity oracle for spec strategy.

Mirrors PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json
exactly. Parameters are class constants (not GetParameter values) so this template
is a deterministic oracle: any change to the parameters is a deliberate code change,
not a runtime config drift.

Fill model: LEAN's default ImmediateFillModel fills market orders at bar.EndTime /
bar.Close — which matches Engine Lab's signal_bar_close mode. See
docs/references/fill-model-parity-spike-2026-05-19.md.
"""

from __future__ import annotations

EMA_CROSSOVER_SOURCE = '''\
from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    """EMA(5)/EMA(10) crossover with RSI(14) gate on 15-min consolidated bars.

    Validation oracle for the Engine Lab spec at
    PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json.
    Parameters are pinned; only symbol, dates, and starting cash are configurable.
    """

    FAST_PERIOD = 5
    SLOW_PERIOD = 10
    RSI_PERIOD = 14
    BAR_MINUTES = 15
    EXIT_BARS = 5
    GAP_MIN = 0.20
    RSI_LO = 50
    RSI_HI = 70

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        equity = self.AddEquity(symbol_str, Resolution.Minute, fillForward=False)
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.symbol = equity.Symbol

        self.consolidator = TradeBarConsolidator(timedelta(minutes=self.BAR_MINUTES))
        self.consolidator.DataConsolidated += self.OnConsolidatedBar
        self.SubscriptionManager.AddConsolidator(self.symbol, self.consolidator)

        self.ema_fast = ExponentialMovingAverage(self.FAST_PERIOD)
        self.ema_slow = ExponentialMovingAverage(self.SLOW_PERIOD)
        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        self.prev_fast = None
        self.prev_slow = None
        self.bars_held = 0
        self.in_trade = False

        warmup_minutes = max(self.SLOW_PERIOD, self.RSI_PERIOD) * self.BAR_MINUTES * 2
        self.SetWarmUp(timedelta(minutes=warmup_minutes))
        self.SetBenchmark(lambda dt: 100)

    def OnConsolidatedBar(self, sender, bar):
        close = float(bar.Close)
        self.ema_fast.Update(bar.EndTime, close)
        self.ema_slow.Update(bar.EndTime, close)
        self.rsi.Update(bar.EndTime, close)

        if not (self.ema_fast.IsReady and self.ema_slow.IsReady and self.rsi.IsReady):
            self.prev_fast = float(self.ema_fast.Current.Value) if self.ema_fast.IsReady else None
            self.prev_slow = float(self.ema_slow.Current.Value) if self.ema_slow.IsReady else None
            return

        fast = float(self.ema_fast.Current.Value)
        slow = float(self.ema_slow.Current.Value)
        rsi = float(self.rsi.Current.Value)

        if self.IsWarmingUp:
            self.prev_fast, self.prev_slow = fast, slow
            return

        if self.in_trade:
            self.bars_held += 1
            if self.bars_held >= self.EXIT_BARS:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_held = 0
        else:
            fresh_cross = (
                self.prev_fast is not None
                and self.prev_slow is not None
                and fast > slow
                and self.prev_fast <= self.prev_slow
            )
            gap_ok = (fast - slow) >= self.GAP_MIN
            rsi_ok = self.RSI_LO <= rsi <= self.RSI_HI
            if fresh_cross and gap_ok and rsi_ok:
                self.SetHoldings(self.symbol, 1.0)
                self.in_trade = True
                self.bars_held = 0

        self.prev_fast, self.prev_slow = fast, slow

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
