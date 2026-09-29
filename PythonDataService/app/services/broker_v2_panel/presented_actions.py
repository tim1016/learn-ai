"""Presented-actions builder (spec §11).

Builds the closed ``PanelAction`` set for one bot given its lifecycle state and
the clerk's account state. The backend decides which actions are enabled and
authors their copy; Angular renders exactly what it is handed and executes only
the known action ids. Every action binds to the panel-state ``revision`` so a
stale POST is a 409.

Lifecycle semantics (§12) drive the enablement:

- ``stop``   — running bot; stops signals + cancels working entries, exposure
               untouched.
- ``archive`` — stopped, settled, flat bot; terminal (Clear on Home, ADR 0052).
- ``cancel_order`` — a working order exists.
- ``reconcile_now`` — always available (triggers a sweep).

Enablement logic lives in ``app.broker.v2panel.action_policy.ACTION_REGISTRY``
(the single canonical location per decision #18). This module is the stable
public entry point so callers (``panel_projection_service``) keep the same
import path.
"""

from __future__ import annotations

from app.broker.v2panel.action_policy import ActionGuardContext, build_actions_from_registry
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import ClerkCard, PanelAction


def build_actions(
    status: BotStatusView,
    clerk: ClerkCard,
    *,
    revision: int,
    channel_fresh: bool,
    exposure: dict[str, float],
    account_id: str,
    working_order_count: int,
    account_working_order_count: int,
    account_expected_exposure: dict[str, float],
) -> list[PanelAction]:
    """Build the closed presented-action set for one bot (§11, §12).

    ``exposure`` is the bot's attributed net exposure per symbol (from the S0
    rollup) — it gates ``archive`` while the bot still holds a position.
    """
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())
    del channel_fresh, account_working_order_count, account_expected_exposure
    ctx = ActionGuardContext(
        running=status.running,
        phase=status.phase,
        hold_active=clerk.hold_active,
        freeze_active=clerk.freeze_active,
        reconciliation_verdict=clerk.reconciliation_verdict,
        outstanding_intents=clerk.outstanding_intents,
        has_exposure=has_exposure,
        account_id=account_id,
        strategy_instance_id=status.strategy_instance_id,
        exposure=exposure,
        working_order_count=working_order_count,
    )
    return build_actions_from_registry(ctx, revision=revision, broker="alpaca")

