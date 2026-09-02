"""LEAN validation twin for the canonical RSI Mean Reversion strategy.

Formula: Long-only Wilders RSI(14) threshold mean reversion on 15-minute
signal bars. Entry while flat: RSI strictly below 30. Exit while in trade:
RSI strictly above 70. No time-based exit; any open position is liquidated
at end of algorithm.
Reference: mirrors
``PythonDataService/app/engine/strategy/spec/fixtures/rsi_mean_reversion.spec.json``
exactly.
Canonical implementation:
``app.engine.strategy.algorithms.rsi_mean_reversion.RsiMeanReversionAlgorithm``.
Validated against: ``docs/references/reconciliations/rsi-mean-reversion-lean-2026-09-01.md``.

Strategy parameters (RSI period, oversold, overbought) are class constants --
not ``GetParameter`` values -- so this template is a deterministic oracle: any
change to the rules is a deliberate code change, not runtime config drift.
This mirrors ``ema_crossover``'s discipline and is why the registry forwards
no ``lean_parameter_names`` for this strategy: a run that overrides ``window``,
``oversold``, or ``overbought`` is honestly reported as
``parameters_unrepresentable_by_twin`` rather than silently compared against a
twin still running 14/30/70.

Runtime parameters (symbol, bar_minutes, session, adjustment) ARE read via
``GetParameter`` because they describe the data contract, not the strategy
logic.

Unlike ``ema_crossover``, this template does not pin ``bar_minutes`` to 15.
That guard exists there because ``EXIT_BARS=5`` is a bar-count time stop tied
to the consolidator; RSI mean reversion has no bar-count-dependent rule, so
any consolidation period is a faithful twin of the same rules.

Fill model: LEAN's default ``ImmediateFillModel`` fills market orders at
bar.EndTime / bar.Close -- matches Engine Lab's ``signal_bar_close`` mode.

Bar consumption proof: observations.csv (every minute bar received).
Decision state proof: state.csv (one row per consolidated bar after warmup).
Its columns are RSI-shaped (``ts_ms_utc,close,rsi,signal``), not the
EMA-shaped schema in ``parity_matrix.state_parity.EXPECTED_COLUMNS``. That
comparator is Gate 2 of the cross-engine golden matrix, which has no strategy
axis yet and therefore does not cover this template; emitting dummy
``ema_fast``/``ema_slow``/``cross_state`` columns to satisfy it would fake the
exact agreement the gate exists to detect. ``deployment_validation`` sets the
same precedent with its own ``green_streak`` schema.
"""

from __future__ import annotations

RSI_MEAN_REVERSION_SOURCE = '''\
from AlgorithmImports import *
from datetime import datetime
from zoneinfo import ZoneInfo


_ET = ZoneInfo("America/New_York")


def _to_ms_utc(dt):
    """Normalize a QC-supplied Python datetime to int64 ms UTC.

    QC's Python bridge passes bar.EndTime as a naive datetime in the
    algorithm timezone (ET for US equities). Attaching the ET zone
    before .timestamp() is the only safe way to convert to a UTC epoch.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp() * 1000)


class MyAlgorithm(QCAlgorithm):
    """Wilders RSI(14) threshold mean reversion on 15-min consolidated bars.

    Validation twin for the Engine Lab spec at
    PythonDataService/app/engine/strategy/spec/fixtures/rsi_mean_reversion.spec.json.
    """

    RSI_PERIOD = 14
    OVERSOLD = 30
    OVERBOUGHT = 70

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        bar_minutes_str = self.GetParameter("bar_minutes") or "15"
        session = self.GetParameter("session") or "regular"
        adjustment = self.GetParameter("adjustment") or "raw"

        bar_minutes = int(bar_minutes_str)
        if bar_minutes <= 0:
            raise ValueError("bar_minutes=" + str(bar_minutes) + " must be positive")

        if adjustment != "raw":
            raise ValueError("adjustment=" + str(adjustment) + " not supported; only 'raw' in Phase 1")

        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        # Lock the brokerage model: this template is registered with the
        # interactive_brokers brokerage policy, so LEAN must charge IBKR
        # equity-tier commission (per-share + floor + cap), not the default
        # ConstantFeeModel(0). The engine side pins the same model via
        # app.engine.execution.commission.IbkrEquityCommissionModel.
        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)

        equity = self.AddEquity(
            symbol_str,
            Resolution.Minute,
            fillForward=False,
            extendedMarketHours=(session == "extended"),
        )
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.symbol = equity.Symbol

        self.consolidator = TradeBarConsolidator(timedelta(minutes=bar_minutes))
        self.consolidator.DataConsolidated += self.OnConsolidatedBar
        self.SubscriptionManager.AddConsolidator(self.symbol, self.consolidator)

        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        self.in_trade = False

        # Indicator-readiness gating only -- no wall-clock warmup call. Both
        # engines use the same IsReady gate (samples >= period + 1) so
        # state.csv row counts align.
        self.SetBenchmark(lambda dt: 100)

        obs_path = self.ObjectStore.GetFilePath("observations.csv")
        with open(obs_path, "w") as f:
            f.write("ms_utc,open,high,low,close,volume\\n")
        self._obs_path = obs_path

        state_path = self.ObjectStore.GetFilePath("state.csv")
        with open(state_path, "w") as f:
            f.write("ts_ms_utc,close,rsi,signal\\n")
        self._state_path = state_path

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

    def OnConsolidatedBar(self, sender, bar):
        close = float(bar.Close)
        self.rsi.Update(bar.EndTime, close)

        if not self.rsi.IsReady:
            return

        rsi = float(self.rsi.Current.Value)

        # Exit is evaluated only while in trade and entry only while flat,
        # so at most one action occurs per bar -- mirroring
        # RsiMeanReversionAlgorithm.evaluate_signal_bar's branch structure.
        signal = "HOLD"
        if self.in_trade:
            if rsi > self.OVERBOUGHT:
                self.Liquidate(self.symbol)
                self.in_trade = False
                signal = "EXIT"
        else:
            if rsi < self.OVERSOLD:
                self.SetHoldings(self.symbol, 1.0)
                self.in_trade = True
                signal = "ENTER"

        with open(self._state_path, "a") as f:
            f.write(
                str(_to_ms_utc(bar.EndTime)) + ","
                + str(close) + ","
                + str(rsi) + ","
                + signal + "\\n"
            )

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
