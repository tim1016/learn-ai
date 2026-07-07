"""Live-runtime configuration.

Phase 1 intentionally keeps this module small. Broker-mode safety stays in
``app.broker.ibkr.config`` and order safety stays in ``app.broker.ibkr.orders``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Literal

from app.engine.execution.order_sizer import SizingPolicy
from app.engine.live.order_identity import DEFAULT_ORDER_REF_MAX_LENGTH

# Single source of truth for the operator-supplied ``live_config`` dict keys.
# The deploy boundary (``HostRunnerDeployRequest._validate_sizing``) rejects
# unknown siblings; ``_live_config_from_ledger`` rejects them when reading a
# legacy ledger. Adding a field here is a deliberate two-sided change.
LIVE_CONFIG_LEDGER_KEYS: frozenset[str] = frozenset(
    {
        "symbol",
        "force_flat_at",
        "consolidator_period_min",
        "run_dir",
        "max_submit_latency_ms",
        "sizing",
        # PRD #593 Slice 1A — operator-declared instrument plan.
        # Hashed into ``run_id`` like every other key here. The current
        # deployment-validation live path consumes exactly one long stock leg;
        # unsupported shapes remain declarative until their resolver ships.
        "action",
        # ADR 0014 §6 — per-instance lag thresholds for the broker-activity
        # reconciliation verdict ladder. Optional block; absence ⇒ engine
        # uses ``ReconciliationTimingPolicy`` defaults. Hashed into
        # ``run_id`` so a threshold change forces a redeploy (cross-run
        # comparability of reconciliation verdicts is preserved).
        "reconciliation_timing_policy",
    }
)


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

    # ADR 0014 §6 — per-instance reconciliation lag thresholds. Stored as
    # a dict (not a typed model) so this dataclass stays a plain LiveConfig
    # and the publisher constructs ``ReconciliationTimingPolicy`` on demand.
    # ``None`` ⇒ publisher uses the policy's built-in defaults
    # (``caveat_lag_ms=2000``, ``excessive_lag_ms=10000``).
    reconciliation_timing_policy: dict | None = None


def stock_symbol_from_action_plan(action: object) -> str | None:
    """Return the single stock underlying declared by a live action plan.

    Action plans are operator-authored deploy identity. For the current
    stock-only runtime path, exactly one long stock leg is the traded ticker.
    Option, short, and multi-leg plans are not consumable by the stock runtime
    yet, so they deliberately return ``None``.
    """
    if not isinstance(action, dict):
        return None
    on_enter = action.get("on_enter")
    if not isinstance(on_enter, list) or not on_enter:
        return None

    symbols: set[str] = set()
    for leg in on_enter:
        if not isinstance(leg, dict):
            return None
        if leg.get("position") != "long":
            return None
        instrument = leg.get("instrument")
        if not isinstance(instrument, dict):
            return None
        if instrument.get("kind") != "stock":
            return None
        underlying = instrument.get("underlying")
        if not isinstance(underlying, str) or not underlying.strip():
            return None
        symbols.add(underlying.strip().upper())

    if len(symbols) != 1 or len(on_enter) != 1:
        return None
    return next(iter(symbols))


ActionPlanDeployReasonCode = Literal[
    "ACTION_PLAN_EMPTY",
    "ACTION_PLAN_ENTRY_LEG_REQUIRED",
    "ACTION_PLAN_UNSUPPORTED",
    "ACTION_PLAN_CLOSE_LEG_REQUIRED",
]


@dataclass(frozen=True)
class ActionPlanDeployReadiness:
    reason_code: ActionPlanDeployReasonCode | None = None
    message: str = "Action plan is ready for deployment."

    @property
    def can_deploy(self) -> bool:
        return self.reason_code is None


_ACTION_PLAN_REQUIRED_STRATEGIES = frozenset({"deployment_validation"})


def action_plan_deploy_readiness(
    *,
    strategy_key: str,
    live_config: dict,
) -> ActionPlanDeployReadiness:
    """Return the deploy-time action-plan verdict for strategies that consume it.

    Today the live runtime consumes a stock-only action plan for
    ``deployment_validation``: exactly one long stock entry leg plus a close-leg
    exit for that entry. Other strategies remain non-blocking until they declare
    a deploy-time action-plan contract, which keeps valid future entry-only/roll
    plans from being rejected by this deployment-validation-specific gate.
    """

    if strategy_key.strip() not in _ACTION_PLAN_REQUIRED_STRATEGIES:
        return ActionPlanDeployReadiness()
    action = live_config.get("action") if isinstance(live_config, dict) else None
    if not isinstance(action, dict):
        return ActionPlanDeployReadiness(
            reason_code="ACTION_PLAN_EMPTY",
            message=(
                "Deployment Validation requires an action plan with one long stock "
                "entry leg and a matching close leg before deployment."
            ),
        )
    on_enter = action.get("on_enter")
    on_exit = action.get("on_exit")
    has_entries = isinstance(on_enter, list) and len(on_enter) > 0
    has_exits = isinstance(on_exit, list) and len(on_exit) > 0
    if not has_entries and not has_exits:
        return ActionPlanDeployReadiness(
            reason_code="ACTION_PLAN_EMPTY",
            message=("Deployment Validation requires an action plan; ON ENTER and ON EXIT are both empty."),
        )
    if not has_entries:
        return ActionPlanDeployReadiness(
            reason_code="ACTION_PLAN_ENTRY_LEG_REQUIRED",
            message="Deployment Validation requires at least one ON ENTER entry leg.",
        )
    try:
        from app.schemas.action_plan import ActionPlan

        plan = ActionPlan.model_validate(action)
    except ValueError:
        return ActionPlanDeployReadiness(
            reason_code="ACTION_PLAN_UNSUPPORTED",
            message=(
                "Deployment Validation cannot consume this action-plan shape. "
                "Use one long stock entry leg with a close-leg exit."
            ),
        )
    if stock_symbol_from_action_plan(action) is None:
        return ActionPlanDeployReadiness(
            reason_code="ACTION_PLAN_UNSUPPORTED",
            message=(
                "Deployment Validation currently supports exactly one long stock "
                "entry leg. Option, short, and multi-leg plans are not deployable "
                "on this runtime path yet."
            ),
        )
    entry_leg_id = plan.on_enter[0].leg_id
    if not any(exit_entry.entry_leg_id == entry_leg_id for exit_entry in plan.on_exit):
        return ActionPlanDeployReadiness(
            reason_code="ACTION_PLAN_CLOSE_LEG_REQUIRED",
            message=(f"Deployment Validation requires an ON EXIT close leg for the entry leg {entry_leg_id!r}."),
        )
    return ActionPlanDeployReadiness()
