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
"""

from __future__ import annotations

from app.broker.v2panel.vocabulary import ActionId, copy_for
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import ClerkCard, PanelAction
from app.schemas.operator_blocker import OperatorConfirmationCopy


def _action(
    action_id: ActionId,
    *,
    enabled: bool,
    revision: int,
    confirmation: OperatorConfirmationCopy | None = None,
) -> PanelAction:
    copy = copy_for(action_id)
    return PanelAction(
        action_id=action_id,
        label=copy.label,
        explanation=copy.explanation,
        enabled=enabled,
        blockers=[],
        confirmation=confirmation,
        revision=revision,
    )


def build_actions(
    status: BotStatusView,
    clerk: ClerkCard,
    *,
    revision: int,
    flatten_supported: bool,
    channel_fresh: bool,
    exposure: dict[str, float],
) -> list[PanelAction]:
    """Build the closed presented-action set for one bot (§11, §12).

    ``channel_fresh`` reflects whether the hold's root condition (channel
    health) has been freshly observed — the clear_hold gate (§7.3).
    ``exposure`` is the bot's attributed net exposure per symbol (from the S0
    rollup) — it gates ``flatten_stop`` when the bot is stopped but still holds
    a position.
    """
    retired = status.phase == "RETIRED"
    running = status.running
    has_exposure = any(abs(qty) > 0 for qty in exposure.values())

    actions: list[PanelAction] = []

    # start — off duty and not retired.
    actions.append(
        _action("start", enabled=not running and not retired, revision=revision)
    )
    # stop — running.
    actions.append(_action("stop", enabled=running, revision=revision))
    # flatten_stop — only when the broker supports flatten and the bot is
    # running or holds exposure.
    actions.append(
        _action(
            "flatten_stop",
            enabled=flatten_supported and (running or has_exposure),
            revision=revision,
        )
    )
    # retire — any non-retired bot (terminal; Button-Rule confirm).
    actions.append(_action("retire", enabled=not retired, revision=revision))
    # cancel_order — presented always; the specific order id is chosen in the UI
    # from the working-orders list. Enabled whenever the bot is not retired.
    actions.append(_action("cancel_order", enabled=not retired, revision=revision))
    # clear_hold — an active hold whose root condition is healthy + freshly
    # observed (§7.3). No force-override path.
    actions.append(
        _action(
            "clear_hold",
            enabled=clerk.hold_active and channel_fresh,
            revision=revision,
        )
    )
    # reconcile_now — always available (triggers a sweep).
    actions.append(_action("reconcile_now", enabled=True, revision=revision))

    return actions
