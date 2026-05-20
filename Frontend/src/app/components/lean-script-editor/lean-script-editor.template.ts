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
 *
 * **Parameterised**: symbol, start/end dates, and starting cash are
 * pulled from ``self.GetParameter(...)`` — the same parameters the
 * orchestrator forwards from the form. Hardcoding any of these in a
 * user algorithm causes silent runtime errors (LEAN finds no data
 * for the hardcoded symbol when the form selected a different one),
 * so the seed teaches the correct pattern by example. Fixtures are
 * the only context where hardcoding is appropriate, because they pin
 * specific (symbol, window) tuples to byte-equivalent bars.
 *
 * **Benchmark pinned to a constant**: LEAN's default benchmark is SPY
 * daily, which the orchestrator does not stage (it stages bars for the
 * form's symbol only). Without ``SetBenchmark``, the post-run
 * ``ResultsAnalyzer`` throws ``Sequence contains no elements`` when it
 * tries to align an empty SPY benchmark series, flagging the run as
 * dirty. Trusted samples (``buy_and_hold.py``, ``ema_crossover.py``)
 * pin a constant for the same reason.
 */
export const EMA_CROSSOVER_SOURCE_TEMPLATE = `from AlgorithmImports import *


class MyAlgorithm(QCAlgorithm):
    """EMA(5)/EMA(10) crossover. Symbol, window, and cash come from form parameters."""

    FAST_PERIOD = 5
    SLOW_PERIOD = 10

    def Initialize(self):
        # Pull symbol/window/cash from form parameters; never hardcode.
        # The orchestrator forwards these via LeanConfig.parameters; the
        # fallback values are safety nets that should rarely fire.
        start = self.GetParameter("start_date") or "2025-01-13"
        end = self.GetParameter("end_date") or "2025-01-17"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"

        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        equity = self.AddEquity(symbol_str, Resolution.Minute)
        self.symbol = equity.Symbol
        self.ema_fast = self.EMA(self.symbol, self.FAST_PERIOD, Resolution.Minute)
        self.ema_slow = self.EMA(self.symbol, self.SLOW_PERIOD, Resolution.Minute)

        # LEAN's default benchmark is SPY daily, which would require staging
        # daily SPY bars in addition to the strategy symbol's minute bars.
        # The orchestrator only stages data for the selected symbol, so the
        # post-run ResultsAnalyzer fails ("Sequence contains no elements")
        # when it tries to build a benchmark curve from an unstaged SPY feed.
        # Pinning a constant benchmark sidesteps that.
        self.SetBenchmark(lambda dt: 100)

    def OnData(self, slice):
        if not (self.ema_fast.IsReady and self.ema_slow.IsReady):
            return
        if not self.Portfolio.Invested and self.ema_fast.Current.Value > self.ema_slow.Current.Value:
            self.SetHoldings(self.symbol, 1.0)
        elif self.Portfolio.Invested and self.ema_fast.Current.Value < self.ema_slow.Current.Value:
            self.Liquidate(self.symbol)
`;
