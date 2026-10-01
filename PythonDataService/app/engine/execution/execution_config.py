"""ExecutionConfig — single source of truth for market-mechanics knobs.

A reusable realism layer that decouples strategy logic from broker
simulation. Strategies stay focused on signal generation; this config
owns slippage, commission and fill-mode selection, threaded through the
``/api/engine/backtest`` request into ``FillModel`` and the engine.

It holds no session wall-clock rule. The one rule that reads the
session's end -- a decision on the bar that ends at the calendar's close
is set aside -- lives in ``app.lean_sidecar.closing_bar`` (#2607).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode


@dataclass(frozen=True)
class ExecutionConfig:
    """Execution realism settings for a backtest run.

    Defaults match ``FillModel``'s defaults exactly.
    """

    fill_mode: FillMode = FillMode.SIGNAL_BAR_CLOSE
    commission_per_order: Decimal = field(default_factory=lambda: Decimal("1.00"))
    slippage_per_share: Decimal = field(default_factory=lambda: Decimal(0))

    def build_fill_model(self) -> FillModel:
        return FillModel(
            mode=self.fill_mode,
            commission_per_order=self.commission_per_order,
            slippage_per_share=self.slippage_per_share,
        )
