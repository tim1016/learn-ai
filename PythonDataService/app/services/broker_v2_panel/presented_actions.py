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
    ctx = ActionGuardContext(
        running=status.running,
        phase=status.phase,
        hold_active=clerk.hold_active,
        channel_fresh=channel_fresh,
        has_exposure=any(abs(qty) > 0 for qty in exposure.values()),
        flatten_supported=flatten_supported,
    )
    return build_actions_from_registry(ctx, revision=revision, broker="alpaca")
