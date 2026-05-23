"""EMA(5)/EMA(10) crossover trusted template — LEAN parity oracle for spec strategy.

Mirrors PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json
exactly. Strategy parameters (period, gap, RSI band, time stop) are class
constants — not GetParameter values — so this template is a deterministic
oracle: any change to the parameters is a deliberate code change, not a
runtime config drift.

Runtime parameters (symbol, bar_minutes, session, adjustment) ARE read via
GetParameter because they describe the data contract, not the strategy logic.
The orchestrator passes them through LeanConfig.parameters; the parity test
asserts the values reach the algorithm correctly.

Fill model: LEAN's default ImmediateFillModel fills market orders at
bar.EndTime / bar.Close — matches Engine Lab's signal_bar_close mode.
See docs/references/fill-model-parity-spike-2026-05-19.md.

Bar consumption proof: observations.csv (every minute bar received).
Decision state proof: state.csv (one row per consolidated bar after warmup).

PR B (2026-05-19): the 15-minute consolidator and EXIT_BARS=5 time-stop
are pinned by the template's source itself, not by a global
``bar_minutes: Literal[15]`` on the request. Callers that build a
``DataPolicy`` for this template MUST set
``strategy_bars=BarsSpec(timespan="minute", multiplier=15)`` — the
router synthesizes that default when a legacy payload omits the
``data_policy`` block.
"""

from __future__ import annotations

EMA_CROSSOVER_SOURCE = '''\
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
    """EMA(5)/EMA(10) crossover with RSI(14) gate on 15-min consolidated bars.

    Validation oracle for the Engine Lab spec at
    PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json.
    """

    FAST_PERIOD = 5
    SLOW_PERIOD = 10
    RSI_PERIOD = 14
    EXIT_BARS = 5
    GAP_MIN = 0.20
    RSI_LO = 50
    RSI_HI = 70

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        bar_minutes_str = self.GetParameter("bar_minutes") or "15"
        session = self.GetParameter("session") or "regular"
        adjustment = self.GetParameter("adjustment") or "raw"

        bar_minutes = int(bar_minutes_str)
        if bar_minutes != 15:
            raise ValueError(
                "bar_minutes=" + str(bar_minutes) + " not supported; "
                "EXIT_BARS=5 is tied to a 15-min consolidator in this branch"
            )

        if adjustment != "raw":
            raise ValueError("adjustment=" + str(adjustment) + " not supported; only 'raw' in Phase 1")

        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        # Lock the brokerage model: matrix Gate 3 runs with assert_fees=True
        # so LEAN must charge IBKR equity-tier commission (per-share + floor +
        # cap), not the default ConstantFeeModel(0). The engine side pins the
        # same model via app.engine.execution.commission.IbkrEquityCommissionModel.
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

        self.ema_fast = ExponentialMovingAverage(self.FAST_PERIOD)
        self.ema_slow = ExponentialMovingAverage(self.SLOW_PERIOD)
        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        self.prev_fast = None
        self.prev_slow = None
        self.bars_held = 0
        self.in_trade = False

        # Indicator-readiness gating only — no wall-clock warmup call. Both
        # engines use the same IsReady gate so state.csv row counts align.
        self.SetBenchmark(lambda dt: 100)

        obs_path = self.ObjectStore.GetFilePath("observations.csv")
        with open(obs_path, "w") as f:
            f.write("ms_utc,open,high,low,close,volume\\n")
        self._obs_path = obs_path

        state_path = self.ObjectStore.GetFilePath("state.csv")
        with open(state_path, "w") as f:
            f.write("ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\\n")
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

        signal = "HOLD"
        if self.in_trade:
            self.bars_held += 1
            if self.bars_held >= self.EXIT_BARS:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_held = 0
                signal = "EXIT"
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
                signal = "ENTER"

        if fast > slow:
            cross_state = "above"
        elif fast < slow:
            cross_state = "below"
        else:
            cross_state = "equal"

        with open(self._state_path, "a") as f:
            f.write(
                str(_to_ms_utc(bar.EndTime)) + ","
                + str(close) + ","
                + str(fast) + ","
                + str(slow) + ","
                + str(rsi) + ","
                + cross_state + ","
                + signal + "\\n"
            )

        self.prev_fast, self.prev_slow = fast, slow

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
