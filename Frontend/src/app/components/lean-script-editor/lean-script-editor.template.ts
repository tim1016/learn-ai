// Default content for the LEAN script editor — a parity-aligned starter
// that mirrors the strategy semantics of the canonical parity oracle at
// PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py
// (which itself mirrors the python SpyEmaCrossover strategy).
//
// Running this template through the LEAN sidecar against the python
// SpyEmaCrossover strategy on the same window should produce the same
// trades. The parity-test instrumentation carried by the oracle —
// observations.csv / state.csv ObjectStore writers, the _to_ms_utc
// helper, the bar_minutes / session / adjustment GetParameter pulls —
// is intentionally dropped here. Those exist for parity-test
// scaffolding and would be noise in a starter an operator is meant to
// read top-to-bottom.
//
// Symbol, start/end dates, and starting cash are pulled from
// self.GetParameter(...) — the same parameters the orchestrator
// forwards from the form. Hardcoding any of these in a user algorithm
// causes silent runtime errors (LEAN finds no data for the hardcoded
// symbol when the form selected a different one), so the seed teaches
// the correct pattern by example.
//
// Stay in sync with EMA_CROSSOVER_SOURCE in the trusted_samples file
// above. The editor test pins the parity-critical markers (RSI(14)
// Wilders, 15-min consolidator, EXIT_BARS = 5, GAP_MIN = 0.20,
// fresh-cross logic) so future edits cannot silently regress to a
// simpler EMA-only crossover that no longer matches SpyEmaCrossover.
export const EMA_CROSSOVER_SOURCE_TEMPLATE = `from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    """EMA(5)/EMA(10) crossover with RSI(14) gate on 15-min consolidated bars.

    Parity-aligned starter: produces the same trades as the python
    SpyEmaCrossover strategy on the same window. Symbol, window, and
    starting cash come from form parameters.
    """

    FAST_PERIOD = 5
    SLOW_PERIOD = 10
    RSI_PERIOD = 14
    EXIT_BARS = 5
    GAP_MIN = 0.20
    RSI_LO = 50
    RSI_HI = 70

    def Initialize(self):
        # Pull symbol/window/cash from form parameters; never hardcode.
        # The orchestrator forwards these via LeanConfig.parameters; the
        # fallback values are safety nets that should rarely fire.
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

        self.consolidator = TradeBarConsolidator(timedelta(minutes=15))
        self.consolidator.DataConsolidated += self.OnConsolidatedBar
        self.SubscriptionManager.AddConsolidator(self.symbol, self.consolidator)

        self.ema_fast = ExponentialMovingAverage(self.FAST_PERIOD)
        self.ema_slow = ExponentialMovingAverage(self.SLOW_PERIOD)
        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        self.prev_fast = None
        self.prev_slow = None
        self.bars_held = 0
        self.in_trade = False

        # Engine Lab stages data for the requested symbol only. LEAN's
        # default benchmark is SPY and may request unstaged hour/daily
        # files during post-run analysis, so pin a constant benchmark.
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
`;
