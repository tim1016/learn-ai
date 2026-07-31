"""Presented-actions builder (spec §11).

Builds the closed ``PanelAction`` set for one bot given its lifecycle state and
the clerk's account state. The backend decides which actions are enabled and
authors their copy; Angular renders exactly what it is handed and executes only
the known action ids. Every action binds to the panel-state ``revision`` so a
stale POST is a 409.

Lifecycle semantics (§12) drive the enablement:

- ``start``  — off-duty bot.
- ``stop``   — running bot; stops signals + cancels working entries, exposure
               untouched.
- ``flatten_stop`` — running/exposed bot, only when the broker supports flatten.
- ``retire`` — non-retired bot; terminal, carries ``replaces_sid`` lineage on
               the replacement deploy (not this action).
- ``cancel_order`` — a working order exists.
- ``clear_hold`` — an active hold whose root condition is healthy + fresh.
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


def resume_eligible(
    status: BotStatusView,
    clerk: ClerkCard,
    exposure: dict[str, float],
) -> bool:
    """Backend-owned flat/carryover Resume projection."""
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())
    common = (
        not status.running
        and status.phase != "RETIRED"
        and status.desired_state == "STOPPED"
        and not clerk.hold_active
        and not clerk.freeze_active
    )
    if not common:
        return False
    if not has_exposure:
        return True
    return (
        status.carryover_policy == "ALLOW"
        and status.carryover_account_policy_enabled
        and status.carryover_checkpoint_config_matches
        and status.carryover_checkpoint_exposure == exposure
        and clerk.reconciliation_verdict == "clean"
        and clerk.outstanding_intents == 0
    )


def build_actions(
    status: BotStatusView,
    clerk: ClerkCard,
    *,
    revision: int,
    flatten_supported: bool,
    channel_fresh: bool,
    exposure: dict[str, float],
    account_id: str,
    working_order_count: int,
) -> list[PanelAction]:
    """Build the closed presented-action set for one bot (§11, §12).

    ``channel_fresh`` reflects whether the hold's root condition (channel
    health) has been freshly observed — the clear_hold gate (§7.3).
    ``exposure`` is the bot's attributed net exposure per symbol (from the S0
    rollup) — it gates ``flatten_stop`` when the bot is stopped but still holds
    a position.
    """
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())
    carryover_resume_ready = has_exposure and resume_eligible(status, clerk, exposure)
    ctx = ActionGuardContext(
        running=status.running,
        phase=status.phase,
        desired_state=status.desired_state,
        hold_active=clerk.hold_active,
        freeze_active=clerk.freeze_active,
        channel_fresh=channel_fresh,
        has_exposure=has_exposure,
        carryover_resume_ready=carryover_resume_ready,
        flatten_supported=flatten_supported,
        account_id=account_id,
        strategy_instance_id=status.strategy_instance_id,
        exposure=exposure,
        working_order_count=working_order_count,
    )
    return build_actions_from_registry(ctx, revision=revision, broker="alpaca")


def build_roster_action(
    status: BotStatusView,
    clerk: ClerkCard | None,
    *,
    revision: int,
    flatten_supported: bool,
    channel_fresh: bool,
    exposure: dict[str, float],
    account_id: str,
) -> PanelAction | None:
    """Present only the routine Start/Stop command needed by a roster row.

    Start fails closed when Clerk posture is unavailable. Stop remains
    available because its canonical action token depends only on runtime
    liveness, not account posture.
    """
    if status.phase == "RETIRED":
        return None
    if clerk is None:
        if not status.running:
            return None
        ctx = ActionGuardContext(
            running=True,
            phase=status.phase,
            desired_state=status.desired_state,
            hold_active=True,
            freeze_active=True,
            channel_fresh=False,
            has_exposure=False,
            carryover_resume_ready=False,
            flatten_supported=flatten_supported,
            account_id=account_id,
            strategy_instance_id=status.strategy_instance_id,
            exposure=exposure,
            working_order_count=0,
        )
        actions = build_actions_from_registry(
            ctx,
            revision=revision,
            broker="alpaca",
        )
    else:
        actions = build_actions(
            status,
            clerk,
            revision=revision,
            flatten_supported=flatten_supported,
            channel_fresh=channel_fresh,
            exposure=exposure,
            account_id=account_id,
            working_order_count=0,
        )
    action_id = "stop" if status.running else "start"
    return next(
        (action for action in actions if action.action_id == action_id),
        None,
    )
