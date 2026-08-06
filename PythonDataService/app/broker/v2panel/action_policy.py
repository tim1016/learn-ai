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
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    ConditionScope,
    OperatorBlocker,
    OperatorConfirmationCopy,
)
from app.schemas.run_admission import RunAdmissionDecision


@dataclass(frozen=True)
class ActionGuardContext:
    """Snapshot of the durable panel state used to compute action enablement."""

    running: bool
    phase: str
    desired_state: str
    hold_active: bool
    freeze_active: bool
    reconciliation_verdict: str | None
    outstanding_intents: int
    channel_fresh: bool
    has_exposure: bool
    resume_admission: RunAdmissionDecision | None
    flatten_supported: bool
    account_id: str
    strategy_instance_id: str
    exposure: dict[str, float]
    working_order_count: int
    account_working_order_count: int
    inventory_recovery_needed: bool


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
        audience="both",
        disposition="wait",
        headline=headline,
        detail=detail,
        applies_to="run",
        evidence=evidence,
    )


def _disabled(*blockers: OperatorBlocker) -> tuple[bool, list[OperatorBlocker]]:
    return False, list(blockers)


def _stable_admission_evidence_refs(evidence_refs: tuple[str, ...]) -> tuple[str, ...]:
    """Drop observation-only suffixes from the optimistic-concurrency input.

    The complete evidence reference remains on the admission receipt for audit.
    A fresh observation instant is not, by itself, a changed safety decision, so
    its timestamp must not make an already presented Resume action stale.

    Two refs carry only an observation instant that advances every evaluation:

    - ``market-data-feed:<feed_id>:<observed_at_ms>`` — a health probe; keep the
      feed identity, drop the probe time.
    - ``alpaca-reconciliation:<observed_at_ms>`` — a Clerk reconciliation pass;
      keep the constant marker, drop the pass time. Each panel GET and the
      action POST run their own fresh reconciliation with a fresh clock, so
      leaving this instant in the token made an unchanged off-duty Resume 409
      on essentially every click (val-nvda-0804-05, 2026-08-04).

    A genuine custody change is still captured by
    ``alpaca-clerk-journal:<account>:<journal_sequence>`` (the Clerk appends a
    line only on change) and by the decision's ``allowed`` / ``reason_code``
    fields, so normalising these two instants out cannot hide a real change.
    """
    stable: list[str] = []
    for ref in evidence_refs:
        if ref.startswith("market-data-feed:"):
            stable.append(":".join(ref.split(":")[:2]))
        elif ref.startswith("alpaca-reconciliation:"):
            stable.append("alpaca-reconciliation")
        else:
            stable.append(ref)
    return tuple(stable)


def _guard_deploy(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    # deploy is a list-page action; the per-bot panel always presents it disabled.
    return _disabled()


def _guard_resume(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    """Render the runner's typed Resume decision without recreating it."""
    decision = ctx.resume_admission
    if decision is None:
        return _disabled(
            _blocker(
                "RESUME_ADMISSION_UNAVAILABLE",
                scope="bot",
                headline="Resume safety is unknown.",
                detail="Refresh after the bot registry and Clerk can produce one admission decision.",
                evidence={"strategy_instance_id": ctx.strategy_instance_id},
            )
        )
    if decision.allowed:
        return True, []
    return _disabled(
        _blocker(
            decision.reason_code,
            scope="bot",
            headline="Resume is blocked.",
            detail=decision.explanation,
            evidence={
                "strategy_instance_id": decision.strategy_instance_id,
                "evaluated_at_ms": decision.evaluated_at_ms,
            },
        )
    )


def _guard_stop(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    if ctx.running:
        return True, []
    return _disabled(
        _blocker(
            "BOT_NOT_RUNNING",
            scope="bot",
            headline="The bot is already off duty.",
                detail="Use Resume when you are ready to create a new run.",
        )
    )


def _guard_pause(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    if ctx.running and ctx.desired_state == "RUNNING":
        return True, []
    return _disabled(
        _blocker(
            "PAUSE_REQUIRES_LIVE_RUNNING_RUN",
            scope="bot",
            headline="Pause requires a live evaluating run.",
            detail="Resume a stopped bot, or use Continue if this run is already paused.",
            evidence={"strategy_instance_id": ctx.strategy_instance_id},
        )
    )


def _guard_continue(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    if ctx.running and ctx.desired_state == "PAUSED":
        return True, []
    return _disabled(
        _blocker(
            "CONTINUE_REQUIRES_LIVE_PAUSED_RUN",
            scope="bot",
            headline="Continue requires a live paused run.",
            detail="Continue keeps the current run ID; Resume is only for a terminal prior run.",
            evidence={"strategy_instance_id": ctx.strategy_instance_id},
        )
    )


def _guard_flatten_stop(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    blockers: list[OperatorBlocker] = []
    if not ctx.flatten_supported:
        blockers.append(
            _blocker(
                "FLATTEN_UNSUPPORTED",
                scope="broker",
                headline="This broker does not support panel flattening.",
                detail="Use the broker's custody surface to reduce exposure safely.",
            )
        )
    if ctx.freeze_active:
        blockers.append(
            _blocker(
                "ACCOUNT_CUSTODY_UNPROVABLE",
                scope="account",
                headline="The Clerk cannot prove the exposure to flatten.",
                detail="Restore broker observation and run Reconcile now before flattening.",
                evidence={"account_id": ctx.account_id},
            )
        )
    if not ctx.running and not ctx.has_exposure:
        blockers.append(
            _blocker(
                "BOT_ALREADY_FLAT_AND_STOPPED",
                scope="bot",
                headline="The bot is already stopped with no attributed exposure.",
                detail="No flatten command is necessary.",
            )
        )
    return (not blockers), blockers


def _guard_retire(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _disabled()


def _guard_cancel_order(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _disabled()


def _guard_clear_hold(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    if not ctx.hold_active:
        return _disabled(
            _blocker(
                "NO_ACCOUNT_HOLD",
                scope="account",
                headline="No account hold is active.",
                detail="There is nothing to clear.",
            )
        )
    if not ctx.channel_fresh:
        return _disabled(
            _blocker(
                "HOLD_ROOT_NOT_RECOVERED",
                scope="account",
                headline="The hold's root condition is not healthy and fresh.",
                detail="Restore both channels and reconcile before clearing the hold.",
                evidence={"account_id": ctx.account_id},
            )
        )
    return True, []


def _guard_reconcile_now(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return True, []


def _guard_record_inventory_baseline(
    ctx: ActionGuardContext,
) -> tuple[bool, list[OperatorBlocker]]:
    blockers: list[OperatorBlocker] = []
    if ctx.running:
        blockers.append(
            _blocker(
                "BOT_RUNNING",
                scope="bot",
                headline="Stop the bot before retiring inventory attribution.",
                detail="Stop strategy evaluation, then refresh the recovery controls.",
                evidence={"strategy_instance_id": ctx.strategy_instance_id},
            )
        )
    if not ctx.inventory_recovery_needed:
        blockers.append(
            _blocker(
                "INVENTORY_BASELINE_NOT_REQUIRED",
                scope="account",
                headline="No inventory cutover recovery is active.",
                detail=(
                    "This recovery appears only for a missing-intent freeze or a "
                    "stopped bot whose stale attribution remains while the account is flat."
                ),
                evidence={"account_id": ctx.account_id},
            )
        )
    if ctx.outstanding_intents:
        blockers.append(
            _blocker(
                "UNRESOLVED_INTENTS",
                scope="account",
                headline="Unresolved order intents block inventory recovery.",
                detail="Resolve every uncertain submit before recording an inventory baseline.",
                evidence={"outstanding_intents": ctx.outstanding_intents},
            )
        )
    if ctx.account_working_order_count:
        blockers.append(
            _blocker(
                "WORKING_ORDERS_PRESENT",
                scope="account",
                headline="Working orders block inventory recovery.",
                detail="Cancel or settle every working order, then reconcile again.",
                evidence={"working_order_count": ctx.account_working_order_count},
            )
        )
    return (not blockers), blockers


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
    "resume": ActionPolicy(
        action_id="resume",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_resume,
        revision_inputs=lambda ctx: (
            ctx.resume_admission.allowed if ctx.resume_admission is not None else None,
            ctx.resume_admission.reason_code if ctx.resume_admission is not None else None,
            ctx.resume_admission.configuration_hash if ctx.resume_admission is not None else None,
            (
                _stable_admission_evidence_refs(ctx.resume_admission.evidence_refs)
                if ctx.resume_admission is not None
                else ()
            ),
        ),
    ),
    "pause": ActionPolicy(
        action_id="pause",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_pause,
        revision_inputs=lambda ctx: (ctx.running, ctx.desired_state),
    ),
    "continue": ActionPolicy(
        action_id="continue",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_continue,
        revision_inputs=lambda ctx: (ctx.running, ctx.desired_state),
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
        revision_inputs=lambda ctx: (
            ctx.running,
            ctx.has_exposure,
            tuple(sorted(ctx.exposure.items())),
            ctx.working_order_count,
            ctx.flatten_supported,
            ctx.freeze_active,
        ),
    ),
    "retire": ActionPolicy(
        action_id="retire",
        supported_brokers=frozenset(),
        list_page_only=False,
        guard=_guard_retire,
        revision_inputs=lambda ctx: (ctx.phase,),
    ),
    "cancel_order": ActionPolicy(
        action_id="cancel_order",
        supported_brokers=frozenset(),
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
    "record_inventory_baseline": ActionPolicy(
        action_id="record_inventory_baseline",
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_record_inventory_baseline,
        revision_inputs=lambda ctx: (
            ctx.running,
            ctx.reconciliation_verdict,
            ctx.outstanding_intents,
            ctx.account_working_order_count,
            ctx.inventory_recovery_needed,
        ),
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
    if action_id == "flatten_stop":
        return OperatorConfirmationCopy(
            title="Flatten attributed exposure and stop?",
            body=(
                f"This command targets {ctx.strategy_instance_id} on account "
                f"{ctx.account_id}. Attributed exposure: "
                + (
                    ", ".join(
                        f"{symbol} {quantity:g}"
                        for symbol, quantity in sorted(ctx.exposure.items())
                    )
                    or "none"
                )
                + f". Working orders: {ctx.working_order_count}."
            ),
            consequence=(
                "The runtime stops first. The Clerk then cancels working entry "
                "orders and submits reducing orders; fills may complete later."
            ),
            confirm_label="Flatten & stop",
            required_token="FLATTEN",
        )
    if action_id == "record_inventory_baseline":
        return OperatorConfirmationCopy(
            title="Adopt current broker inventory as the accounting baseline?",
            body=(
                f"This account-level recovery targets {ctx.account_id}. "
                "It reads current Alpaca positions and records an audited "
                "cutover in the Clerk journal."
            ),
            consequence=(
                "Earlier trades remain in history but stop contributing to "
                "current account or bot exposure. All pre-cutover bot "
                "attribution is retired; current broker positions remain "
                "unassigned."
            ),
            confirm_label="Recover inventory baseline",
            required_token="BASELINE",
        )
    if action_id == "clear_hold":
        return OperatorConfirmationCopy(
            title="Clear the account hold?",
            body=(
                f"This account-level hold on {ctx.account_id} currently blocks "
                "new-entry order submission for every bot on the account."
            ),
            consequence=(
                f"New-entry order submission resumes immediately for every bot "
                f"on {ctx.account_id}. The condition that caused the hold is "
                "not re-checked by this action — clear it only once the root "
                "cause is confirmed resolved."
            ),
            confirm_label="Clear hold",
            required_token="CLEARHOLD",
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
