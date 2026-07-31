"""ActionPolicy registry — per-action guard + broker-scope rules (spec §11).

Every ``ActionPolicy`` declares which brokers support an action and a guard
function that decides whether the action is currently enabled for a given
``ActionGuardContext``. Copy (label/explanation) stays in ``vocabulary.py``
(``copy_for``). Execution stays in ``panel_data_source._action_performers``.
This module is the single canonical location for enablement logic — it
replaces the scattered ``if``-chains in ``presented_actions.py`` (spec §11,
decision register #7, #18).

``build_actions_from_registry`` is the replacement body for
``presented_actions.build_actions``. ``supported_action_ids_for`` feeds
``panel_profile_service.alpaca_panel_profile`` so the profile is derived from
the same registry, never manually maintained in parallel.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from app.broker.v2panel.vocabulary import ACTION_IDS, ActionId, copy_for
from app.schemas.broker_v2_panel import PanelAction
from app.schemas.operator_blocker import OperatorBlocker


@dataclass(frozen=True)
class ActionGuardContext:
    """Snapshot of the durable panel state used to compute action enablement."""

    running: bool
    phase: str
    hold_active: bool
    channel_fresh: bool
    has_exposure: bool
    flatten_supported: bool


@dataclass(frozen=True)
class ActionPolicy:
    """Closed descriptor for one panel action.

    ``supported_brokers``      — which brokers expose this action.
    ``list_page_only``         — True for actions that belong to the broker/bots
                                 list page (e.g. ``deploy``), not the per-bot
                                 panel. ``build_actions_from_registry`` skips
                                 list-page-only actions; the profile still
                                 advertises them (the list page reads the same
                                 profile).
    ``guard``                  — (enabled, blockers) for the current context.
    ``revision_inputs``        — tuple of state fields that, when changed, advance
                                 the panel revision for this action (reserved for
                                 future fine-grained revision computation).
    """

    action_id: str
    supported_brokers: frozenset[str]
    list_page_only: bool
    guard: Callable[[ActionGuardContext], tuple[bool, list[OperatorBlocker]]]
    revision_inputs: Callable[[ActionGuardContext], tuple]


def _no_blockers(enabled: bool) -> tuple[bool, list[OperatorBlocker]]:
    return enabled, []


def _guard_deploy(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    # deploy is a list-page action; the per-bot panel always presents it disabled.
    return _no_blockers(False)


def _guard_start(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(
        not ctx.running
        and ctx.phase != "RETIRED"
        and not ctx.has_exposure
        and not ctx.hold_active
    )


def _guard_stop(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(ctx.running)


def _guard_flatten_stop(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(ctx.flatten_supported and (ctx.running or ctx.has_exposure))


def _guard_retire(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(ctx.phase != "RETIRED")


def _guard_cancel_order(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(ctx.phase != "RETIRED")


def _guard_clear_hold(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(ctx.hold_active and ctx.channel_fresh)


def _guard_reconcile_now(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _no_blockers(True)


ACTION_REGISTRY: dict[str, ActionPolicy] = {
    # deploy is a list-page action (broker/bots list), not a per-bot panel action.
    # The profile advertises it; the per-bot build skips it (list_page_only=True).
    "deploy": ActionPolicy(
        action_id="deploy",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=True,
        guard=_guard_deploy,
        revision_inputs=lambda ctx: (),
    ),
    "start": ActionPolicy(
        action_id="start",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_start,
        revision_inputs=lambda ctx: (
            ctx.running,
            ctx.phase,
            ctx.has_exposure,
            ctx.hold_active,
        ),
    ),
    "stop": ActionPolicy(
        action_id="stop",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_stop,
        revision_inputs=lambda ctx: (ctx.running,),
    ),
    "flatten_stop": ActionPolicy(
        action_id="flatten_stop",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_flatten_stop,
        revision_inputs=lambda ctx: (ctx.running, ctx.has_exposure, ctx.flatten_supported),
    ),
    "retire": ActionPolicy(
        action_id="retire",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_retire,
        revision_inputs=lambda ctx: (ctx.phase,),
    ),
    "cancel_order": ActionPolicy(
        action_id="cancel_order",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_cancel_order,
        revision_inputs=lambda ctx: (ctx.phase,),
    ),
    "clear_hold": ActionPolicy(
        action_id="clear_hold",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_clear_hold,
        revision_inputs=lambda ctx: (ctx.hold_active, ctx.channel_fresh),
    ),
    "reconcile_now": ActionPolicy(
        action_id="reconcile_now",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_reconcile_now,
        revision_inputs=lambda ctx: (),
    ),
}


def supported_action_ids_for(broker: str) -> list[ActionId]:
    """Return the ordered action ids supported by ``broker`` (§11, §4).

    Preserves ``ACTION_IDS`` order so the profile is deterministically ordered
    and contract-test-stable.
    """
    return [
        action_id
        for action_id in ACTION_IDS
        if broker in ACTION_REGISTRY[action_id].supported_brokers
    ]


def build_actions_from_registry(
    ctx: ActionGuardContext,
    *,
    revision: int,
    broker: str,
) -> list[PanelAction]:
    """Build the closed presented-action set from the registry (§11).

    Filters to ``broker``-supported, per-bot actions (``list_page_only=False``),
    derives enablement from ``ctx`` via each policy's guard, and returns
    ``PanelAction`` objects in ``ACTION_IDS`` order with server-authored copy
    from ``copy_for()``.

    List-page-only actions (``deploy``) are advertised in the ``PanelProfile``
    via ``supported_action_ids_for`` but are NOT included in the per-bot action
    set — the list page renders them separately.
    """
    actions: list[PanelAction] = []
    for action_id in ACTION_IDS:
        policy = ACTION_REGISTRY[action_id]
        if broker not in policy.supported_brokers:
            continue
        if policy.list_page_only:
            continue
        enabled, blockers = policy.guard(ctx)
        copy = copy_for(action_id)
        # Each action owns its own compare-and-set domain.  In particular STOP
        # depends only on whether this instance is still running; Clerk journal
        # activity and other panel changes cannot manufacture a Stop-409.
        token_payload = {
            "action_id": action_id,
            "inputs": policy.revision_inputs(ctx),
        }
        concurrency_token = hashlib.sha256(
            json.dumps(token_payload, separators=(",", ":"), default=str).encode()
        ).hexdigest()[:32]
        actions.append(
            PanelAction(
                action_id=action_id,  # type: ignore[arg-type]
                label=copy.label,
                explanation=copy.explanation,
                enabled=enabled,
                blockers=blockers,
                confirmation=None,
                revision=revision,
                concurrency_token=concurrency_token,
            )
        )
    return actions
