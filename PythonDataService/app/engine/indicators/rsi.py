"""RelativeStrengthIndex with Wilders smoothing.

Formula: RSI = 100 - 100/(1 + RS), where RS = avg_gain / avg_loss; Wilders smoothing avg_new = (avg_old·(period-1) + sample) / period.
Reference: references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/RelativeStrengthIndex.cs and WilderMovingAverage.cs, selected by MovingAverageTypeExtensions.cs
Canonical implementation: this file.
Validated against: PythonDataService/tests/fixtures/test_indicator_fixtures.py::TestIND003RSI

Mirrors LEAN's Indicators/RelativeStrengthIndex.cs with
``MovingAverageType.Wilders``.

Critical reproducibility details:
  * Per-step gain: ``max(0, input - prev_input)`` when ``input >= prev_input``,
    else 0. (Equality is a gain of 0, not a loss.)
  * Per-step loss: ``max(0, prev_input - input)`` when ``input < prev_input``,
    else 0.
  * The gain/loss Wilder averages publish a running SMA for the first
    ``period - 1`` deltas. On the ``period``-th delta, they apply
    ``avg_new = (avg_old * (period - 1) + sample_new) / period``.
  * Warmup: ``is_ready`` when ``samples >= period + 1`` (one extra sample
    for the first delta). LEAN publishes RSI throughout warmup, starting
    with 100 on the first price sample, when both averages are still zero.
  * Edge case: if ``round(avg_loss, 10) == 0``, RSI = 100.
  * Formula: ``RS = avg_gain / avg_loss``; ``RSI = 100 - 100 / (1 + RS)``.
Convergence counts bound recursive seed memory (ADR 0083). They do not
guarantee the same relative bound for ratios or cancelling output terms.
Measured twin evidence: tests/engine/indicators/test_convergence_twins.py.

"""

from __future__ import annotations

from decimal import Decimal

from app.engine.indicators.base import Indicator
from app.engine.indicators.convergence import CONVERGENCE_TOLERANCE, decay_steps


class RelativeStrengthIndex(Indicator):
    series_kind = "rsi_wilders"

    def __init__(self, name: str, period: int) -> None:
        # Readiness requires one extra price sample for the first delta.
        super().__init__(name, period)
        self._period_dec = Decimal(period)
        self._period_minus_1 = Decimal(period - 1)
        self._prev_input: Decimal | None = None
        self._avg_gain = Decimal(0)
        self._avg_loss = Decimal(0)
        # The child Wilder averages use a running SMA before readiness.
        self._gain_sum: Decimal = Decimal(0)
        self._loss_sum: Decimal = Decimal(0)
        self._delta_samples: int = 0

    @property
    def is_ready(self) -> bool:
        # RSI(period) needs period+1 samples (for period deltas).
        return self.samples >= self.period + 1

    @property
    def ready_bars(self) -> int:
        return self.period + 1

    def bars_to_converge(self, tolerance: float = CONVERGENCE_TOLERANCE) -> int:
        return self.ready_bars + decay_steps(1 / self.period, tolerance)

    def _compute_next_value(self, timestamp_ms: int, value: Decimal) -> Decimal | None:
        prev = self._prev_input
        self._prev_input = value
        if prev is None:
            # LEAN's child averages are zero; its zero-loss branch publishes 100.
            return Decimal(100)

        if value >= prev:
            gain = value - prev
            loss = Decimal(0)
        else:
            gain = Decimal(0)
            loss = prev - value

        self._delta_samples += 1

        if self._delta_samples < self.period:
            self._gain_sum += gain
            self._loss_sum += loss
            count = Decimal(self._delta_samples)
            self._avg_gain = self._gain_sum / count
            self._avg_loss = self._loss_sum / count
        else:
            # The period-th delta is already on the Wilder recurrence path.
            self._avg_gain = (self._avg_gain * self._period_minus_1 + gain) / self._period_dec
            self._avg_loss = (self._avg_loss * self._period_minus_1 + loss) / self._period_dec

        # Compute RSI from the current averages.
        # LEAN's edge case: if rounded-to-10 avg_loss is zero, RSI is 100.
        if round(self._avg_loss, 10) == Decimal(0):
            return Decimal(100)
        rs = self._avg_gain / self._avg_loss
        return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))

    def _reset_state(self) -> None:
        self._prev_input = None
        self._avg_gain = Decimal(0)
        self._avg_loss = Decimal(0)
        self._gain_sum = Decimal(0)
        self._loss_sum = Decimal(0)
        self._delta_samples = 0
