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


def strategy_runtime_missing(strategy_key: str) -> bool:
    """True when no runtime is registered for this bot's strategy key.

    A registration the runtime no longer knows can never run again -- the
    legacy bot bound to a mistyped symbol is the standing example -- and
    retire is its only cure (#1778, S5). Deferred import: the strategy
    registry pulls in the engine, which must not load to present a panel.
    """
    from app.engine.strategy.registry import _STRATEGY_REGISTRY

    return strategy_key not in _STRATEGY_REGISTRY


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

    ``exposure`` is the bot's attributed net exposure per symbol (from the S0
    rollup) — it gates ``flatten_stop`` when the bot is stopped but still holds
    a position.
    """
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())
    del channel_fresh, account_working_order_count, account_expected_exposure
    ctx = ActionGuardContext(
        running=status.running,
        phase=status.phase,
        desired_state=status.desired_state,
        hold_active=clerk.hold_active,
        freeze_active=clerk.freeze_active,
        reconciliation_verdict=clerk.reconciliation_verdict,
        outstanding_intents=clerk.outstanding_intents,
        has_exposure=has_exposure,
        resume_admission=resume_admission,
        flatten_supported=flatten_supported,
        account_id=account_id,
        strategy_instance_id=status.strategy_instance_id,
        exposure=exposure,
        working_order_count=working_order_count,
        strategy_runtime_missing=strategy_runtime_missing(status.strategy_key),
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
            has_exposure=False,
            resume_admission=None,
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
            account_working_order_count=0,
            account_expected_exposure={},
            resume_admission=None,
        )
    return next(
        (action for action in actions if action.action_id == "stop"),
        None,
    )
