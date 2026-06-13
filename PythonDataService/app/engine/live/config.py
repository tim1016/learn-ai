"""Live-runtime configuration.

Phase 1 intentionally keeps this module small. Broker-mode safety stays in
``app.broker.ibkr.config`` and order safety stays in ``app.broker.ibkr.orders``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path

from app.engine.execution.order_sizer import SizingPolicy
from app.engine.live.order_identity import DEFAULT_ORDER_REF_MAX_LENGTH


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

    # ADR 0009 — live position-sizing policy. ``None`` ⇒ legacy/unknown
    # (pre-policy ``SimpleFloorSizing`` all-in); a sizing-aware deploy ALWAYS
    # writes an explicit policy (the canonical safe default is FixedShares(1)),
    # so a fresh empty-``live_config`` run never hash-collides with the safe
    # canary. Hashed into ``run_id`` through ``live_config`` like every other
    # field on this dataclass.
    sizing: SizingPolicy | None = None

    # ── Durable submit protocol (ADR-0008 / PRD #446) ──────────────────────────
    # Master switch. Stays False until BOTH Acceptance-Gate receipts exist;
    # ``broker_ownership_query.require_durable_submit_activation`` refuses
    # activation otherwise, so flipping this alone cannot turn the protocol on.
    durable_submit_enabled: bool = False
    # Conservative cap used to bound ``build_order_ref`` and the
    # ``strategy_instance_id`` length rule for the deterministic core. TODO(#446
    # Gate #1): the REAL cap is ``durable_submit_verified_order_ref_cap``, set
    # only from a live paper receipt; this fallback just gives the pure logic a
    # bound to enforce. Truncation is silent and catastrophic.
    durable_submit_order_ref_max_length: int = DEFAULT_ORDER_REF_MAX_LENGTH
    # The orderRef cap PROVEN by a live paper order (Gate #1). ``None`` =
    # unverified => activation refused (ADR-0008 §1: "C is intentionally unset
    # until the paper-receipt gate verifies the actual echoed cap").
    durable_submit_verified_order_ref_cap: int | None = None

