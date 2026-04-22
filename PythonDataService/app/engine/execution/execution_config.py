"""ExecutionConfig — single source of truth for market-mechanics knobs.

A reusable realism layer that decouples strategy logic from broker
simulation. Strategies stay focused on signal generation; this config
owns slippage, commission, fill-mode selection, and (in upcoming PRs)
TP/SL intrabar resolution, session entry cutoffs, force-flat, and limit
order penetration rules.

PR 1 scope: slippage, commission, and fill mode — threaded through the
``/api/engine/backtest`` request into ``FillModel``. Later PRs will add:

* ``tp_sl_resolver`` — pessimistic-first intrabar ordering (the high-
  leverage anti-bias win; see project memory).
* ``session_entry_cutoff`` and ``force_flat_at_close`` — wrapper-level
  rules that cancel orphan entry orders and sync strategy state.
* ``limit_penetrate_ticks`` — measured against the bar's adverse
  extreme (low for buy limits, high for sell limits), not the close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal

from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode


@dataclass(frozen=True)
class ExecutionConfig:
    """Execution realism settings for a backtest run.

    Defaults match ``FillModel``'s defaults exactly AND leave the
    session-boundary rules off, so ``ExecutionConfig()`` preserves
    bit-exact LEAN parity for strategies that don't opt into the
    realism layer.

    ``session_entry_cutoff`` and ``force_flat_at`` are interpreted in
    the timezone of the bar data (``bar.time`` / ``bar.end_time``). For
    LEAN minute data that's exchange local time (America/New_York); for
    other sources the caller is responsible for aligning the two.
    """

    fill_mode: FillMode = FillMode.SIGNAL_BAR_CLOSE
    commission_per_order: Decimal = field(default_factory=lambda: Decimal("1.00"))
    slippage_per_share: Decimal = field(default_factory=lambda: Decimal(0))
    # After this time-of-day, the engine drops any order that would
    # increase |position|. Reducing or flipping orders still fill — the
    # cutoff protects against opening NEW exposure late in the session,
    # not against closing what's already there.
    session_entry_cutoff: time | None = None
    # When set, the engine treats the first minute bar whose
    # ``bar.time.time() >= force_flat_at`` as a session-close barrier:
    # it cancels every queued and deferred order, clears active TP/SL
    # brackets, closes every open position at that minute's close, and
    # calls ``strategy.on_force_flat()`` so strategies can reset their
    # own internal state. Once per calendar day.
    force_flat_at: time | None = None
    # Dollar penetration required past a resting limit's price before
    # the engine counts the bar as a fill. Measured against the bar's
    # adverse extreme: the low for a buy limit, the high for a sell
    # limit. Default 0 = touch fill (TradingView's permissive default).
    # For US equities a ``Decimal("0.02")`` = 2 cents = 2 ticks gives
    # a realistic queue-position model without simulating the order
    # book directly.
    limit_penetration: Decimal = field(default_factory=lambda: Decimal(0))

    def build_fill_model(self) -> FillModel:
        return FillModel(
            mode=self.fill_mode,
            commission_per_order=self.commission_per_order,
            slippage_per_share=self.slippage_per_share,
        )
