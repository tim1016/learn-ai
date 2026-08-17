"""Two-basis-point variant of the canonical EMA Crossover Signal.

Formula: entry gap gate = ``10,000 * (EMA(5) - EMA(10)) / EMA(10) >= 2``;
all indicator, crossover, RSI, holding-period, sizing, and execution semantics
are inherited unchanged from ``EmaCrossoverSignalAlgorithm``.
Reference: internal normalized-gap protocol documented in
``docs/references/spy-ema-normalized-gap-walk-forward.md``.
Canonical implementation: this file delegates the normalized-gap arithmetic
to ``app.engine.strategy.spec.primitives.difference_bps``.
Validated against: ``tests/engine/strategy/algorithms/test_signal_only_ema_crossover.py``;
``app/engine/strategy/spec/tests/test_spec_spy_ema_2_bps_parity.py``; and the
trade-level LEAN golden fixture ``ENG-007`` exercised by
``tests/integration/reconciliation/test_ema_crossover_2_bps_lean_golden.py``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.spec.primitives import difference_bps


class EmaCrossover2BpsAlgorithm(EmaCrossoverSignalAlgorithm):
    """Run the EMA crossover with configurable relative-gap and RSI gates."""

    STRATEGY_KEY = "ema_crossover_2_bps"

    def __init__(
        self,
        symbol: str = "SPY",
        output_dir: Path | None = None,
        *,
        gap_bps: Decimal | float = Decimal("2"),
        rsi_min: Decimal | float = Decimal("50"),
        rsi_max: Decimal | float = Decimal("70"),
    ) -> None:
        super().__init__(symbol=symbol, output_dir=output_dir)
        self._gap_bps = Decimal(str(gap_bps))
        self._rsi_min = Decimal(str(rsi_min))
        self._rsi_max = Decimal(str(rsi_max))
        if not self._gap_bps.is_finite() or not Decimal(0) <= self._gap_bps <= Decimal(100):
            raise ValueError("gap_bps must be finite and between 0 and 100")
        if not self._rsi_min.is_finite() or not self._rsi_max.is_finite():
            raise ValueError("RSI gates must be finite")
        if not Decimal(0) <= self._rsi_min < self._rsi_max <= Decimal(100):
            raise ValueError("rsi_min must be less than rsi_max, within 0 to 100")

    def _gap_is_sufficient(self, ema_fast: Decimal, ema_slow: Decimal) -> bool:
        return difference_bps(ema_fast, ema_slow) >= self._gap_bps

    def _rsi_gate_bounds(self) -> tuple[Decimal, Decimal]:
        return self._rsi_min, self._rsi_max


__all__ = ["EmaCrossover2BpsAlgorithm"]
