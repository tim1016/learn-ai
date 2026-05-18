"""Buy-and-hold Engine-Lab strategy — Phase 5g.2 cross-run pair.

Spiritually identical to the LEAN-Lab trusted sample at
``app/lean_sidecar/trusted_samples/buy_and_hold.py``:

1. Subscribe to one symbol.
2. On the first received minute bar, set 100% of equity into the symbol.
3. Hold to end of backtest.

This is the canonical golden case for Phase 5g cross-engine
reconciliation: running it through the Engine Lab on the same workspace
data zips that the LEAN-Lab trusted sample consumed should produce
trade logs that the cross-reconciler diffs without surfacing any
gating divergence beyond fee / fill-model differences that are already
classified by the ``DivergenceCategory`` taxonomy.

Constructor convention: cross-runnable strategies accept a ``symbol``
kwarg so the cross-run primitive can pin the LEAN-Lab run's symbol
through the resolver. Other parameters (dates, initial cash) are
pinned by the cross-runner via a subclass wrap of ``initialize`` —
this strategy provides harmless defaults so it remains runnable in
isolation.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.base import Strategy


class BuyAndHoldStrategy(Strategy):
    """Engine-Lab pair for the LEAN-Lab buy-and-hold trusted sample.

    Parameters
    ----------
    symbol:
        Ticker to trade. Uppercased on assignment. Defaults to ``SPY`` to
        match the LEAN-Lab trusted-sample default; the cross-run
        primitive passes the LEAN-Lab run's actual symbol so the engine
        reads the right data zips.
    """

    def __init__(self, symbol: str = "SPY") -> None:
        super().__init__()
        self._symbol_name = symbol.upper()
        self._invested = False

    def initialize(self) -> None:
        if self.start_date is None:
            self.set_start_date(2025, 1, 6)
        if self.end_date is None:
            self.set_end_date(2025, 1, 10)
        if not self.initial_cash or self.initial_cash == Decimal(100000):
            self.set_cash(100000)

        assert self.ctx is not None
        self.ctx.add_equity(self._symbol_name)
        self.ctx.register_consolidator(
            self._symbol_name,
            timedelta(minutes=1),
            self._on_minute_bar,
        )

    def _on_minute_bar(self, bar: TradeBar) -> None:
        if self._invested:
            return
        assert self.ctx is not None
        self.ctx.set_holdings(self._symbol_name, Decimal("1.0"))
        self._invested = True
