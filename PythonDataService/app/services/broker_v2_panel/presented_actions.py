"""Presented-actions builder (spec §11).

Builds the closed ``PanelAction`` set for one bot given its lifecycle state and
the clerk's account state. The backend decides which actions are enabled and
authors their copy; Angular renders exactly what it is handed and executes only
the known action ids. Every action binds to the panel-state ``revision`` so a
stale POST is a 409.

Lifecycle semantics (§12) drive the enablement:

- ``resume`` — creates a new run after the runner's typed Resume admission.
- ``pause`` — holds bar evaluation on the current live run.
- ``continue`` — releases a paused live run without changing its run id.
- ``stop``   — running bot; stops signals + cancels working entries, exposure
               untouched.
- ``flatten_stop`` — running/exposed bot, only when the broker supports flatten.
- ``retire`` — non-retired bot; terminal, carries ``replaces_sid`` lineage on
               the replacement deploy (not this action).
- ``cancel_order`` — a working order exists.
- ``clear_hold`` — an active hold whose root condition is healthy + fresh.
- ``record_inventory_baseline`` — missing-intent or stale-attribution recovery
  with no unresolved or working orders.
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
from app.schemas.run_admission import RunAdmissionDecision


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
    account_working_order_count: int,
    account_expected_exposure: dict[str, float],
    resume_admission: RunAdmissionDecision | None,
) -> list[PanelAction]:
    """Build the closed presented-action set for one bot (§11, §12).

    ``channel_fresh`` reflects whether the hold's root condition (channel
    health) has been freshly observed — the clear_hold gate (§7.3).
    ``exposure`` is the bot's attributed net exposure per symbol (from the S0
    rollup) — it gates ``flatten_stop`` when the bot is stopped but still holds
    a position.
    """
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())
    account_expected_flat = not any(
        abs(quantity) > 0 for quantity in account_expected_exposure.values()
    )
    inventory_recovery_needed = clerk.reconciliation_verdict == "missing_intent" or (
        clerk.reconciliation_verdict == "clean"
        and not status.running
        and has_exposure
        and account_expected_flat
    )
    ctx = ActionGuardContext(
        running=status.running,
        phase=status.phase,
        desired_state=status.desired_state,
        hold_active=clerk.hold_active,
        freeze_active=clerk.freeze_active,
        reconciliation_verdict=clerk.reconciliation_verdict,
        outstanding_intents=clerk.outstanding_intents,
        channel_fresh=channel_fresh,
        has_exposure=has_exposure,
        resume_admission=resume_admission,
        flatten_supported=flatten_supported,
        account_id=account_id,
        strategy_instance_id=status.strategy_instance_id,
        exposure=exposure,
        working_order_count=working_order_count,
        account_working_order_count=account_working_order_count,
        inventory_recovery_needed=inventory_recovery_needed,
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
    """Present only Stop in the roster; exact Resume belongs in the bot panel.

    Resume requires request-specific runner and Clerk admission. The catalog
    intentionally does not approximate that decision across every row.
    """
    if status.phase == "RETIRED":
        return None
    if not status.running:
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
            reconciliation_verdict=None,
            outstanding_intents=0,
            channel_fresh=False,
            has_exposure=False,
            resume_admission=None,
            flatten_supported=flatten_supported,
            account_id=account_id,
            strategy_instance_id=status.strategy_instance_id,
            exposure=exposure,
            working_order_count=0,
            account_working_order_count=0,
            inventory_recovery_needed=False,
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
            account_working_order_count=0,
            account_expected_exposure={},
            resume_admission=None,
        )
    return next(
        (action for action in actions if action.action_id == "stop"),
        None,
    )
