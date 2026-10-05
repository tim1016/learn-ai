"""LEAN validation twin for the EMA Previous Crossover strategy.

Formula: Long-only EMA(fast)/EMA(slow) crossover on 15-minute signal bars,
gated by Wilders RSI(14). A cross up opens a cross that stays open while the
fast EMA stays above the slow one. The first bar of an open cross where the
gap floors and the RSI band pass is its entry bar, and that bar spends the
cross whether or not the entry is taken. Exit after ``hold_bars`` bars.
Reference: the rule is the owner's (#2902, 2026-10-05); no outside source
states it. LEAN supplies the consolidation, the indicators and the fills.
Canonical Python implementation:
``app.engine.strategy.algorithms.ema_previous_crossover.EmaPreviousCrossoverAlgorithm``.
Validated against:
``tests/integration/reconciliation/test_ema_previous_crossover_lean_golden.py``.

All seven strategy settings are read from validated runtime parameters, so a
companion run executes the same resolved configuration as Python at any
point, not only the defaults.

The data contract is the EMA Crossover twin's: minute data, raw prices, no
fill-forward, a 15-minute consolidator, the Interactive Brokers fee model,
``observations.csv`` (every minute bar received) and ``state.csv`` (one row
per decision bar once all three indicators are ready).
"""

from __future__ import annotations

EMA_PREVIOUS_CROSSOVER_SOURCE = '''\
from AlgorithmImports import *
from datetime import datetime
import math
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
    """EMA previous crossover: enter on the first good bar after a cross."""

    RSI_PERIOD = 14
    FAST_PERIOD_DEFAULT = 5
    SLOW_PERIOD_DEFAULT = 10
    HOLD_BARS_DEFAULT = 5
    GAP_DEFAULT = 0.20
    GAP_BPS_DEFAULT = 0.0
    RSI_LO_DEFAULT = 50.0
    RSI_HI_DEFAULT = 70.0

    def _whole_number(self, name, default, low, high):
        raw = float(self.GetParameter(name) or str(default))
        if not math.isfinite(raw) or raw != int(raw) or not low <= raw <= high:
            raise ValueError(name + " must be a whole number between " + str(low) + " and " + str(high))
        return int(raw)

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        bar_minutes_str = self.GetParameter("bar_minutes") or "15"
        session = self.GetParameter("session") or "regular"
        adjustment = self.GetParameter("adjustment") or "raw"
        gap_min = float(self.GetParameter("gap") or str(self.GAP_DEFAULT))
        gap_bps_min = float(self.GetParameter("gap_bps") or str(self.GAP_BPS_DEFAULT))
        rsi_lo = float(self.GetParameter("rsi_min") or str(self.RSI_LO_DEFAULT))
        rsi_hi = float(self.GetParameter("rsi_max") or str(self.RSI_HI_DEFAULT))
        fast_period = self._whole_number("fast_period", self.FAST_PERIOD_DEFAULT, 2, 30)
        slow_period = self._whole_number("slow_period", self.SLOW_PERIOD_DEFAULT, 3, 40)
        self.hold_bars = self._whole_number("hold_bars", self.HOLD_BARS_DEFAULT, 1, 26)

        bar_minutes = int(bar_minutes_str)
        if bar_minutes != 15:
            raise ValueError(
                "bar_minutes=" + str(bar_minutes) + " not supported; "
                "the hold counts 15-minute decision bars"
            )

        if adjustment != "raw":
            raise ValueError("adjustment=" + str(adjustment) + " not supported; only 'raw'")

        if not math.isfinite(gap_min) or gap_min < 0.0:
            raise ValueError("gap must be finite and at least 0")
        if not math.isfinite(gap_bps_min) or not 0.0 <= gap_bps_min <= 100.0:
            raise ValueError("gap_bps must be finite and between 0 and 100")
        if not math.isfinite(rsi_lo) or not math.isfinite(rsi_hi) or not 0.0 <= rsi_lo < rsi_hi <= 100.0:
            raise ValueError("rsi_min must be less than rsi_max, within 0 to 100")
        if fast_period >= slow_period:
            raise ValueError("fast_period must be less than slow_period")
        self.gap_min = gap_min
        self.gap_bps_min = gap_bps_min
        self.rsi_lo = rsi_lo
        self.rsi_hi = rsi_hi

        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        # IBKR equity-tier commission, the model the Python engine pins.
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

        self.ema_fast = ExponentialMovingAverage(fast_period)
        self.ema_slow = ExponentialMovingAverage(slow_period)
        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        # Cross state: advanced on every bar, whatever the position.
        self.fast_was_above_slow = False
        self.cross_open = False
        self.bars_held = 0
        self.in_trade = False

        # Indicator-readiness gating only; no wall-clock warmup call.
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
        # A cross is only a cross against a relation the previous bar established.
        relation_was_known = self.ema_fast.IsReady and self.ema_slow.IsReady
        self.ema_fast.Update(bar.EndTime, close)
        self.ema_slow.Update(bar.EndTime, close)
        self.rsi.Update(bar.EndTime, close)

        if not (self.ema_fast.IsReady and self.ema_slow.IsReady):
            self.fast_was_above_slow = False
            self.cross_open = False
            return

        fast = float(self.ema_fast.Current.Value)
        slow = float(self.ema_slow.Current.Value)

        fast_above_slow = fast > slow
        if not fast_above_slow:
            self.cross_open = False
        elif relation_was_known and not self.fast_was_above_slow:
            self.cross_open = True
        self.fast_was_above_slow = fast_above_slow

        if not self.rsi.IsReady:
            return

        rsi = float(self.rsi.Current.Value)

        absolute_gap_ok = (fast - slow) >= self.gap_min
        gap_bps = 10000.0 * (fast - slow) / slow
        normalized_gap_ok = gap_bps >= self.gap_bps_min
        rsi_ok = self.rsi_lo <= rsi <= self.rsi_hi
        entry_bar = self.cross_open and absolute_gap_ok and normalized_gap_ok and rsi_ok
        if entry_bar:
            # One entry bar per cross, taken or not.
            self.cross_open = False

        signal = "HOLD"
        if self.in_trade:
            self.bars_held += 1
            if self.bars_held >= self.hold_bars:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_held = 0
                signal = "EXIT"
        elif entry_bar:
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

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
