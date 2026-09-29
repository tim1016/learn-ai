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
from typing import Literal

from app.broker.v2panel.vocabulary import ACTION_IDS, ActionId, copy_for
from app.schemas.broker_v2_panel import PanelAction
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    ConditionScope,
    OperatorBlocker,
    OperatorConfirmationCopy,
)


@dataclass(frozen=True)
class ActionGuardContext:
    """Snapshot of the durable panel state used to compute action enablement."""

    running: bool
    phase: str
    hold_active: bool
    freeze_active: bool
    reconciliation_verdict: str | None
    outstanding_intents: int
    has_exposure: bool
    account_id: str
    strategy_instance_id: str
    exposure: dict[str, float]
    working_order_count: int


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


def _blocker(
    condition_id: str,
    *,
    scope: ConditionScope,
    headline: str,
    detail: str,
    evidence: dict[str, str | int | float | bool | None] | None = None,
) -> OperatorBlocker:
    return OperatorBlocker.for_host(
        condition_id=condition_id,
        scope=scope,
        host="bot_cockpit",
        anchor=SURFACE_ANCHOR,
        disposition="wait",
        headline=headline,
        detail=detail,
        applies_to="run",
        evidence=evidence,
    )


def _disabled(*blockers: OperatorBlocker) -> tuple[bool, list[OperatorBlocker]]:
    return False, list(blockers)


def _guard_deploy(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    # deploy is a list-page action; the per-bot panel always presents it disabled.
    return _disabled()


ArchiveBlockedCause = Literal[
    "BOT_STILL_RUNNING",
    "BOT_DUTY_NOT_SETTLED",
    "ARCHIVE_CUSTODY_UNPROVABLE",
    "ARCHIVE_WOULD_STRAND_CUSTODY",
]


@dataclass(frozen=True)
class ArchiveVerdict:
    """One definition of "may this registration be archived".

    Shared by the panel guard, which answers it against a projected custody
    snapshot to decide what to present, and by the committing operation in
    :mod:`app.services.bot_runner`, which answers it again against a freshly
    reconciled one before it writes. Archiving is irreversible and the
    presented decision is always older than the click, so the rule must hold
    at both moments and must be one rule, or the two drift.
    """

    eligible: bool
    cause: ArchiveBlockedCause | None = None
    already_retired: bool = False


def evaluate_archive(
    *,
    running: bool,
    phase: str,
    has_exposure: bool,
    working_order_count: int,
    outstanding_effect_count: int,
    custody_provable: bool,
) -> ArchiveVerdict:
    """Decide archive eligibility, nearest obstacle first (ADR 0052).

    Archive is the one exit for a registration the operator is *finished
    with* -- Clear on Home's Finished fold (#2567, #2578). Its enabling proof
    is custody: the registration is stopped, flat, and has no working orders.
    That is why ``custody_provable`` is checked *before* the exposure guard. A
    frozen account cannot prove exposure at all, so under a freeze
    ``has_exposure=False`` reports the Clerk's ignorance rather than the bot's
    flatness -- and archiving on it would be treating an unproven fact as an
    enabling one. The custody guard *is* the proof, so it must be believable
    before it is believed.

    ``running`` and ``phase`` are two different facts and both are required.
    ``running`` is process liveness; ``phase`` is the durable duty record. They
    disagree in exactly the window that matters -- a task that has died before
    its stop transition committed reads ``running=False`` while the authority
    still holds an ACTIVE run -- and archiving there would stamp
    ``retired_at_ms`` on a registration whose run never ended. The fold that
    writes it states there is no active run; this is what makes that true.

    ``outstanding_effect_count`` is bot-scoped and asymmetric by design: the
    commit-time caller reads it from a freshly reconciled custody snapshot,
    while the presentation cannot see it and passes zero. That asymmetry is
    the same one the whole action already has -- the presented decision is
    always older than the click -- and it fails in the safe direction: an
    accepted-but-not-yet-working effect can arm the button and will still be
    refused at commit, rather than committing and letting the effect create
    broker custody for a terminal registration.
    """
    if phase == "RETIRED":
        return ArchiveVerdict(eligible=False, already_retired=True)
    if running:
        return ArchiveVerdict(eligible=False, cause="BOT_STILL_RUNNING")
    if phase != "OFF_DUTY":
        return ArchiveVerdict(eligible=False, cause="BOT_DUTY_NOT_SETTLED")
    if not custody_provable:
        return ArchiveVerdict(eligible=False, cause="ARCHIVE_CUSTODY_UNPROVABLE")
    if has_exposure or working_order_count or outstanding_effect_count:
        return ArchiveVerdict(eligible=False, cause="ARCHIVE_WOULD_STRAND_CUSTODY")
    return ArchiveVerdict(eligible=True)


_ARCHIVE_BLOCKER_COPY: dict[ArchiveBlockedCause, tuple[str, str]] = {
    "BOT_STILL_RUNNING": (
        "Stop the bot before clearing it.",
        "A running bot still evaluates bars and can place orders.",
    ),
    "BOT_DUTY_NOT_SETTLED": (
        "This bot's last run has not finished settling.",
        "Its process is gone but its run is still open. Wait for recovery to "
        "record how that run ended, then clear it.",
    ),
    "ARCHIVE_CUSTODY_UNPROVABLE": (
        "This account cannot prove the bot is flat.",
        "A bot is cleared only on proof that it holds nothing. Choose Reconcile "
        "now once Alpaca can be read, then clear it.",
    ),
    "ARCHIVE_WOULD_STRAND_CUSTODY": (
        "This bot still holds shares or has a working order.",
        "Flatten it and let its working orders finish, then clear it.",
    ),
}


def _guard_archive(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    """Present the shared archive rule as operator guidance (ADR 0052)."""
    verdict = evaluate_archive(
        running=ctx.running,
        phase=ctx.phase,
        has_exposure=ctx.has_exposure,
        working_order_count=ctx.working_order_count,
        # The panel has no bot-scoped effect count; the commit does, and it is
        # what enforces this. See `evaluate_archive` on why that asymmetry is
        # safe here and is the action's existing contract, not a gap in it.
        outstanding_effect_count=0,
        custody_provable=not ctx.freeze_active,
    )
    if verdict.eligible:
        return True, []
    if verdict.already_retired:
        return _disabled(
            _blocker(
                "BOT_ALREADY_RETIRED",
                scope="bot",
                headline="This bot is already cleared.",
                detail="A cleared bot is off Home already; its history is kept.",
                evidence={"strategy_instance_id": ctx.strategy_instance_id},
            )
        )
    assert verdict.cause is not None
    headline, detail = _ARCHIVE_BLOCKER_COPY[verdict.cause]
    return _disabled(
        _blocker(
            verdict.cause,
            scope="account" if verdict.cause == "ARCHIVE_CUSTODY_UNPROVABLE" else "bot",
            headline=headline,
            detail=detail,
            evidence={"strategy_instance_id": ctx.strategy_instance_id},
        )
    )


def _guard_cancel_order(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _disabled()


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
    "archive": ActionPolicy(
        action_id="archive",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_archive,
        revision_inputs=lambda ctx: (
            ctx.phase,
            ctx.running,
            ctx.has_exposure,
            tuple(sorted(ctx.exposure.items())),
            ctx.working_order_count,
            ctx.freeze_active,
        ),
    ),
    "cancel_order": ActionPolicy(
        action_id="cancel_order",
        supported_brokers=frozenset(),
        list_page_only=False,
        guard=_guard_cancel_order,
        revision_inputs=lambda ctx: (ctx.phase,),
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
        if (policy := ACTION_REGISTRY.get(action_id)) is not None
        and broker in policy.supported_brokers
    ]


def _confirmation_for_action(
    action_id: str,
    *,
    enabled: bool,
    ctx: ActionGuardContext,
) -> OperatorConfirmationCopy | None:
    """Build the typed blast-radius copy for consequential actions."""

    if not enabled:
        return None
    if action_id == "archive":
        # State the custody the operator is archiving *on*, not just the bot's
        # name. Archive's enabling proof is that this bot holds nothing, so the
        # confirmation quotes that proof back: an operator who sees "Attributed
        # exposure: none. Working orders: 0." is confirming the fact the guard
        # actually used, and a stale screen showing otherwise is exactly what
        # the commit-time re-check refuses.
        return OperatorConfirmationCopy(
            title="Archive this bot?",
            body=(
                f"This takes {ctx.strategy_instance_id} on account "
                f"{ctx.account_id} off the roster. It is stopped, with no "
                f"attributed exposure and {ctx.working_order_count} working "
                "orders."
            ),
            consequence=(
                "The registration can start no further runs and its id is "
                "never reused. Its history and receipts are kept. This cannot "
                "be undone."
            ),
            confirm_label="Archive bot",
            required_token="ARCHIVE",
        )
    return None


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
        policy = ACTION_REGISTRY.get(action_id)
        if policy is None:
            continue
        if broker not in policy.supported_brokers:
            continue
        if policy.list_page_only:
            continue
        enabled, blockers = policy.guard(ctx)
        copy = copy_for(action_id)
        # Each action owns its own compare-and-set domain: only the state its
        # guard reads can change its token, so unrelated panel changes cannot
        # manufacture a 409.
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
                confirmation=_confirmation_for_action(
                    action_id,
                    enabled=enabled,
                    ctx=ctx,
                ),
                revision=revision,
                concurrency_token=concurrency_token,
            )
        )
    return actions
