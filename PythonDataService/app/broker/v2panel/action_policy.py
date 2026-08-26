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
    has_exposure: bool
    resume_admission: RunAdmissionDecision | None
    flatten_supported: bool
    account_id: str
    strategy_instance_id: str
    exposure: dict[str, float]
    working_order_count: int
    # True when this bot's strategy key is no longer in the runtime registry,
    # so the registration can never run again (dead vocabulary / legacy
    # registration). Defaults False: a caller that has not resolved the
    # registry leaves retire disabled rather than offering it speculatively.
    strategy_runtime_missing: bool = False


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

    Four refs carry only an observation instant that advances every evaluation:

    - ``market-data-feed:<feed_id>:<observed_at_ms>`` — a health probe; keep the
      feed identity, drop the probe time.
    - ``alpaca-reconciliation:<observed_at_ms>`` — a Clerk reconciliation pass;
      keep the constant marker, drop the pass time. Each panel GET and the
      action POST run their own fresh reconciliation with a fresh clock, so
      leaving this instant in the token made an unchanged off-duty Resume 409
      on essentially every click (val-nvda-0804-05, 2026-08-04).
    - ``market-liveness-clock:<source>:<observed_at_ms>`` and
      ``market-liveness-symbol:<source>:<observed_at_ms>`` — liveness probes
      stamped by ``run_admission.py`` with a fresh instant per evaluation;
      keep the source identity, drop the probe time. Left in, they reproduced
      the same always-stale Resume fleet-wide (0/20 executions, 2026-08-24).

    A genuine custody change is still captured by
    ``alpaca-clerk-journal:<account>:<journal_sequence>`` (the Clerk appends a
    line only on change) and by the decision's ``allowed`` / ``reason_code``
    fields — a liveness *state* change flips those — so normalising these
    instants out cannot hide a real change.
    """
    stable: list[str] = []
    for ref in evidence_refs:
        if ref.startswith(
            ("market-data-feed:", "market-liveness-clock:", "market-liveness-symbol:")
        ):
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


RetirementBlockedCause = Literal[
    "BOT_STILL_RUNNING",
    "STRATEGY_STILL_RUNNABLE",
    "RETIRE_WOULD_STRAND_CUSTODY",
]


@dataclass(frozen=True)
class RetirementVerdict:
    """One definition of "may this registration be retired".

    Shared by the panel guard, which answers it against a projected custody
    snapshot to decide what to present, and by the committing operation in
    :mod:`app.services.bot_runner`, which answers it again against a freshly
    reconciled one before it writes. Retirement is irreversible and the
    presented decision is always older than the click, so the same rule must
    hold at both moments -- and it must be one rule, or the two drift.
    """

    eligible: bool
    cause: RetirementBlockedCause | None = None
    already_retired: bool = False


def evaluate_retirement(
    *,
    running: bool,
    phase: str,
    strategy_runtime_missing: bool,
    has_exposure: bool,
    working_order_count: int,
) -> RetirementVerdict:
    """Decide retirement eligibility, nearest obstacle first.

    Ordered so an operator learns the closest thing they can act on, and so
    the custody guards are the last word: retire must never strand exposure.

    ``strategy_runtime_missing`` is the only proof available here that a
    registration can never run again, and it is a *narrower* condition than
    the one retire exists to clear. A bot bound to an unresolvable instrument
    is equally dead, and is the case that motivated this guard -- but its
    strategy key is alive, so this predicate does not fire for it (T1,
    2026-08-26; #1795).

    The blocker copy is worded accordingly. It says the strategy program
    exists, which is what this actually checks; an earlier wording claimed
    "This bot can still run", which the same panel contradicted by refusing
    Resume permanently on the very bot retire exists for.

    Widening this needs a durable, read-safe proof of symbol validity, which
    does not exist yet: no admission reason code is structurally permanent
    (``MARKET_DATA_STALE`` is also what a *warming* symbol reports, so keying
    on it would make every starting bot retire-eligible), and a broker
    security lookup is barred from this path by the #1776 pure-read
    invariant. See #1795.
    """
    if phase == "RETIRED":
        return RetirementVerdict(eligible=False, already_retired=True)
    if running:
        return RetirementVerdict(eligible=False, cause="BOT_STILL_RUNNING")
    if not strategy_runtime_missing:
        return RetirementVerdict(eligible=False, cause="STRATEGY_STILL_RUNNABLE")
    if has_exposure or working_order_count:
        return RetirementVerdict(eligible=False, cause="RETIRE_WOULD_STRAND_CUSTODY")
    return RetirementVerdict(eligible=True)


_RETIRE_BLOCKER_COPY: dict[RetirementBlockedCause, tuple[str, str]] = {
    "BOT_STILL_RUNNING": (
        "Stop the bot before retiring it.",
        "A running bot still evaluates bars and can place orders.",
    ),
    "STRATEGY_STILL_RUNNABLE": (
        "This bot's strategy program still exists.",
        "Retire only clears a registration whose strategy program the runtime "
        "no longer has. It does not cover a bot that cannot run for another "
        "reason -- an unresolvable symbol, for instance.",
    ),
    "RETIRE_WOULD_STRAND_CUSTODY": (
        "This bot still holds custody.",
        "Flatten attributed exposure and let working orders reach a terminal "
        "state before retiring the registration.",
    ),
}


def _guard_retire(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    """Present the shared retirement rule as operator guidance (S5).

    Retire cleans up a registration the runtime can no longer honour -- the
    legacy bot bound to a strategy key that no longer exists. It is not "end
    this bot's life": a healthy stopped bot stays out of scope, because that
    is a destructive lifecycle action with its own safety story.

    The rule itself lives in :func:`evaluate_retirement` because the
    committing operation must re-prove it against fresh custody; this guard
    only turns its verdict into copy.
    """
    verdict = evaluate_retirement(
        running=ctx.running,
        phase=ctx.phase,
        strategy_runtime_missing=ctx.strategy_runtime_missing,
        has_exposure=ctx.has_exposure,
        working_order_count=ctx.working_order_count,
    )
    if verdict.eligible:
        return True, []
    if verdict.cause is None:
        return _disabled()
    return _disabled(
        _blocker(
            verdict.cause,
            scope="bot",
            headline=_RETIRE_BLOCKER_COPY[verdict.cause][0],
            detail=_RETIRE_BLOCKER_COPY[verdict.cause][1],
            evidence={
                "strategy_instance_id": ctx.strategy_instance_id,
                "working_order_count": ctx.working_order_count,
            },
        )
    )


def _guard_cancel_order(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return _disabled()


def _guard_reconcile_now(ctx: ActionGuardContext) -> tuple[bool, list[OperatorBlocker]]:
    return True, []


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
        supported_brokers=frozenset({"alpaca"}),
        list_page_only=False,
        guard=_guard_retire,
        revision_inputs=lambda ctx: (
            ctx.phase,
            ctx.running,
            ctx.strategy_runtime_missing,
            ctx.has_exposure,
            ctx.working_order_count,
        ),
    ),
    "cancel_order": ActionPolicy(
        action_id="cancel_order",
        supported_brokers=frozenset(),
        list_page_only=False,
        guard=_guard_cancel_order,
        revision_inputs=lambda ctx: (ctx.phase,),
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
    if action_id == "retire":
        return OperatorConfirmationCopy(
            title="Retire this registration?",
            body=(
                f"This clears {ctx.strategy_instance_id} on account "
                f"{ctx.account_id} from the roster. Its strategy is no longer "
                "registered, so the runtime can never honour it again."
            ),
            consequence=(
                "The registration stops issuing feed subscriptions and can "
                "start no further runs. This cannot be undone."
            ),
            confirm_label="Retire registration",
            required_token="RETIRE",
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
