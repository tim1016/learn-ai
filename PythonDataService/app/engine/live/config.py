"""Live-runtime configuration.

Phase 1 intentionally keeps this module small. Broker-mode safety stays in
``app.broker.ibkr.config`` and order safety stays in ``app.broker.ibkr.orders``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path


@dataclass(frozen=True)
class LiveConfig:
    """Engine-level knobs for paper runtime tests and later CLI wiring."""

    symbol: str = "SPY"
    # Wall-clock cutoff (interpreted in the same timezone as the bar's
    # ``time`` field) at which the live engine cancels open orders and
    # market-flats every position. Set to ``None`` to disable; the
    # default 15:55 ET targets the standard NYSE close at 16:00. Mirrors
    # ``ExecutionConfig.force_flat_at`` from the backtest engine so the
    # two driver paths can be aligned by passing ``None`` on both sides.
    force_flat_at: time | None = time(15, 55)
    consolidator_period_min: int = 15
    run_dir: Path = Path("live_runs")
    max_submit_latency_ms: int = 500

