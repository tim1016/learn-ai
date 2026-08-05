"""Strategy-decision ENTER over the SQLite spine (#1377, rebuilt against the
corrective foundation slice).

Proves the R1 capture-before-contact fence and the R4 lost-response
resolution discipline end-to-end through one broker-facing command: opening
a position. Two phases, deliberately separable:

- :func:`accept_enter` — purely local. Requires the caller's
  ``lifecycle_run_id`` to match the bot's currently ``ACTIVE`` run (the same
  fence commands.py's Start/Stop enforce) before committing the
  content-addressed ``(strategy_instance_id, decision_id)`` identity through
  :meth:`ClerkSqliteRepository.commit_first_transition`, whose
  ``ENTER_ACCEPTED`` fold atomically creates the effect operation and its
  order row (both ``accepted`` — see the pinned contract's §4/§5) through the
  full R9 mirror fence. No broker call happens inside it, and none may
  happen before it returns (R1).
- :func:`submit_enter` — calls :func:`accept_enter`, then claims the effect
  operation (Scope D's CAS fencing token — pinned contract §2: "a
  transactionally claimed operation work item ... acquired before any broker
  contact") before it ever calls the broker, and only for a fresh
  reservation. A lost response (``BrokerUnavailable``) is never fabricated
  into a terminal outcome; it folds ``unknown`` and hands off to
  :func:`resolve_enter_submission` immediately, the same function a later,
  out-of-process recovery sweep would call for an intent that never even
  reached the broker (accepted, then the process died before the try block
  ran at all — indistinguishable from the caller's side, and resolved
  identically: by asking the broker whether ``order_ref`` landed).
  :func:`resolve_enter_submission` claims the operation too, so a
  *different*-owner recovery sweep can never reach the broker while another
  process already holds the claim; two *same*-process overlapping resolves
  both pass that claim (a same-owner claim is a renewal), so the one about
  to fold a terminal outcome re-verifies its own token is still current
  immediately beforehand and no-ops if a concurrent sibling already won the
  race. A submit response whose ``client_order_id`` doesn't match
  ``order_ref`` (R7) is treated exactly like a lost response too — folded
  ``unknown`` and handed to :func:`resolve_enter_submission` rather than
  folded as this order's evidence. A snapshot reporting fill progress with
  no price to attribute it at (anomalous, not normal) withholds both the
  fill and the ack rather than losing the fill forever.

Every timestamp this module writes comes from ``repo.clock`` — the same
clock the repository was constructed with, never an independently-passed
one. The R4 grace window compares "when did this order first become
uncertain" (a value read back from ``custody_transitions.recorded_at_ms``,
which is always stamped by the repository's own clock) against "now"; those
two must share a time source or the comparison is meaningless.

Deliberately deferred to later slices: the effect operation never reaches a
terminal ``succeeded`` state in this module (that requires knowing an ENTER
is "done" — fully filled vs. still working — which is EXIT/reconciliation
territory, #1378/#1379).

``fold_order_evidence``/``fold_uncertain``/``fold_failed`` moved to
``order_evidence.py`` for #1379: EXIT's cancel-the-entry and
submit-the-reducing-order steps need the identical evidence-folding gate,
not a second copy. Re-exported here (``__all__``) for existing callers.

R6 admission gating against open holds/uncertainties (#1380) is implemented
in :func:`accept_enter` via :func:`~.uncertainty.require_admission` — see
that function's own docstring for the policy (an ``ACCOUNT_CLERK``-scoped
block applies to every bot; a ``BOT``-scoped block applies only to the
matching bot).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.broker.alpaca.clerk.recovery import UNCERTAIN_SUBMIT_GRACE_MS
from app.broker.alpaca.clerk.sqlite.facts import EnterAcceptedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    reject_colon,
    require_active_run,
    require_strategy_instance,
)
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_failed,
    fold_order_evidence,
    fold_uncertain,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import require_admission
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from app.broker.contract.ports import BrokerTradePort
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)

ACTION_ENTER = "ENTER"

__all__ = [
    "EnterSubmission",
    "accept_enter",
    "fold_order_evidence",
    "resolve_enter_submission",
    "submit_enter",
]


@dataclass(frozen=True)
class EnterSubmission:
    command: CommandResource
    effect_operation_id: str | None
    order_ref: str | None
    created: bool  # False for a transport retry / genuine re-request of an existing decision


def _enter_identity(
    *, account_id: str, strategy_instance_id: str, decision_id: str, leg: BrokerOrderLeg
) -> tuple[str, str, str, str]:
    """The (idempotency_key, payload_hash, command_id, effect_idempotency_key)
    quadruple one ENTER decision commits under."""
    idempotency_key = f"{strategy_instance_id}:{decision_id}"
    command_id = f"cmd:{idempotency_key}"
    effect_idempotency_key = f"enter:{idempotency_key}"
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "decision_id": decision_id,
                "action": ACTION_ENTER,
                "leg": leg.model_dump(mode="json"),
            }
        ).encode("utf-8")
    ).hexdigest()
    return idempotency_key, payload_hash, command_id, effect_idempotency_key


def accept_enter(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    leg: BrokerOrderLeg,
) -> EnterSubmission:
    """Reserve + accept, entirely local (no broker call). R1's fence.

    ``lifecycle_run_id`` must match the bot's currently ``ACTIVE`` run — the
    same active-run fence Start/Stop enforce (commands.py), so a stopped or
    stale caller can never make a bot order-capable again just by calling
    this function. R6 admission (#1380) is checked here too: a currently
    ``ACTIVE`` uncertainty or hold that blocks new exposure — for this bot
    specifically, or account-wide — raises
    :class:`~.uncertainty.AdmissionBlockedError` before any transition is
    built. A fresh reservation commits the effect operation and order row
    (mirror finalize included) before returning — that commit is what "one
    accepted operation" means for recovery: nothing about a broker call is
    durable yet, so there is nothing for recovery to duplicate, only to
    resolve.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("decision_id", decision_id)
    reject_colon("lifecycle_run_id", lifecycle_run_id)

    idempotency_key, payload_hash, command_id, effect_idempotency_key = _enter_identity(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        leg=leg,
    )

    def build_transition() -> TransitionInput:
        # See commands.py's submit_start_run for why these checks run here,
        # under commit_first_transition's lock, rather than before it.
        require_strategy_instance(repo, strategy_instance_id)
        active = require_active_run(repo, strategy_instance_id, lifecycle_run_id)
        require_admission(repo, strategy_instance_id=strategy_instance_id)
        effect_operation_id = f"effect:{idempotency_key}"
        namespace = build_bot_order_namespace(strategy_instance_id)
        order_ref = build_order_ref(namespace, mint_intent_id())
        facts = EnterAcceptedFacts(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="strategy_decision",
            action=ACTION_ENTER,
            intended_end_state=None,
            effect_idempotency_key=effect_idempotency_key,
            effect_kind="ENTER",
            decision_id=decision_id,
            leg=leg.model_dump(mode="json"),
        )
        return TransitionInput(
            strategy_instance_id=strategy_instance_id,
            run_id=active.run_id,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="ENTER_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ENTER_ACCEPTED",
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        build_transition=build_transition,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise DurableConflictError(outcome.command)
    if isinstance(outcome, CommandExistingSame):
        existing_order = (
            repo.order_for_effect_operation(outcome.command.effect_operation_id)
            if outcome.command.effect_operation_id
            else None
        )
        return EnterSubmission(
            command=outcome.command,
            effect_operation_id=outcome.command.effect_operation_id,
            order_ref=existing_order.order_ref if existing_order else None,
            created=False,
        )
    assert isinstance(outcome, CommandCreated)
    order = repo.order_for_effect_operation(outcome.command.effect_operation_id)
    assert order is not None
    return EnterSubmission(
        command=outcome.command,
        effect_operation_id=outcome.command.effect_operation_id,
        order_ref=order.order_ref,
        created=True,
    )


async def submit_enter(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    lifecycle_run_id: str,
    leg: BrokerOrderLeg,
    trade: BrokerTradePort,
) -> EnterSubmission:
    """Accept, then (only for a fresh reservation) call the broker.

    A duplicate decision (transport retry or a genuine re-request while the
    original is still working) returns the existing resource from
    :func:`accept_enter` alone — no second broker call, satisfying "one
    broker intent" regardless of how many callers raced to submit it.

    Only ``BrokerUnavailable``/``BrokerError`` are caught around
    ``trade.submit`` — an unmapped exception propagates uncaught rather than
    being silently folded. That is a forensics gap (the durable log cannot
    distinguish "never contacted the broker" from "contacted, response lost
    to an unmapped error"), not a correctness one: the operation is already
    durably ``accepted`` from :func:`accept_enter`, so a later
    :func:`resolve_enter_submission` call still resolves it correctly by
    asking the broker whether ``order_ref`` landed, using the accept
    transition's own timestamp as the grace-window anchor (see
    ``_uncertain_since_ms``).
    """
    accepted = accept_enter(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=lifecycle_run_id,
        leg=leg,
    )
    if not accepted.created:
        return accepted

    assert accepted.effect_operation_id is not None and accepted.order_ref is not None
    repo.claim_before_broker_contact(accepted.effect_operation_id)
    try:
        order = await trade.submit(leg, client_order_id=accepted.order_ref)
    except BrokerUnavailable as exc:
        return await _fold_uncertain_and_resolve(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            why=str(exc),
            trade=trade,
        )
    except BrokerError as exc:
        fold_failed(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            summary_code="ORDER_SUBMIT_FAILED",
            reason="The order did not reach the broker.",
            why=str(exc),
        )
        return _snapshot(
            repo, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref
        )

    if order.client_order_id != accepted.order_ref:
        # Same epistemic situation as a lost response (R4): the broker
        # answered, but not with evidence for *this* order, so folding it
        # here would either violate custody_transitions.order_ref's foreign
        # key (order.client_order_id names no orders row we created) or
        # associate someone else's evidence with our effect operation. Ask
        # the broker again, this time by our own order_ref (R7).
        return await _fold_uncertain_and_resolve(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            why=(
                f"broker returned client_order_id={order.client_order_id!r}, "
                f"expected {accepted.order_ref!r}"
            ),
            trade=trade,
        )

    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=order)
    return _snapshot(
        repo, effect_operation_id=accepted.effect_operation_id, order_ref=accepted.order_ref
    )


async def resolve_enter_submission(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    trade: BrokerTradePort,
) -> EnterSubmission:
    """Resolve one order by exact client order identity (R4/R7).

    Idempotent, last-write-wins: an already-terminal or already-acked order
    short-circuits with no broker call. Otherwise asks the vendor whether
    ``order_ref`` landed:

    - found -> fold the evidence (ack, and any fill it already reports),
    - absent, grace not yet elapsed since the effect was first accepted or
      first went uncertain -> stays ``unknown``, no write (a first absent
      lookup is never assumed terminal — R4),
    - absent, grace elapsed -> fold a definitive ``failed``,
    - a lookup ``BrokerError``, or a mismatched ``client_order_id`` in the
      response -> stays ``unknown``; never fabricate a terminal on either.
    """
    order_row = repo.order(order_ref)
    assert order_row is not None
    effect = repo.effect_operation(order_row.effect_operation_id)
    assert effect is not None

    if effect.state in ("succeeded", "failed") or order_row.broker_order_id is not None:
        return _snapshot(repo, effect_operation_id=effect.effect_operation_id, order_ref=order_ref)

    claim = repo.claim_before_broker_contact(effect.effect_operation_id)
    uncertain_since_ms = _uncertain_since_ms(repo, order_ref)

    try:
        order = await trade.get_order_by_client_order_id(order_ref)
    except BrokerError:
        return _snapshot(repo, effect_operation_id=effect.effect_operation_id, order_ref=order_ref)

    if order is not None and order.client_order_id != order_ref:
        return _snapshot(repo, effect_operation_id=effect.effect_operation_id, order_ref=order_ref)

    if order is None:
        grace_active = (repo.clock() - uncertain_since_ms) < UNCERTAIN_SUBMIT_GRACE_MS
        if grace_active:
            return _snapshot(
                repo, effect_operation_id=effect.effect_operation_id, order_ref=order_ref
            )
        if not repo.verify_operation_claim(
            effect_operation_id=effect.effect_operation_id, token=claim.token
        ):
            # A same-owner claim is a renewal (Scope D), so two overlapping
            # resolves in this process can both pass the claim above — only
            # the one whose token is still current at this instant may fold
            # the terminal outcome. The loser's own token was overwritten by
            # the winner's renewal; the winner already recorded (or is
            # recording) the same result, so there is nothing left to do.
            return _snapshot(
                repo, effect_operation_id=effect.effect_operation_id, order_ref=order_ref
            )
        fold_failed(
            repo,
            effect_operation_id=effect.effect_operation_id,
            order_ref=order_ref,
            summary_code="ORDER_SUBMIT_FAILED_ABSENT",
            reason="The order did not reach the broker.",
            why="Alpaca has no order for this client_order_id (definitively absent).",
        )
    else:
        fold_order_evidence(repo, effect_operation_id=effect.effect_operation_id, order=order)

    return _snapshot(repo, effect_operation_id=effect.effect_operation_id, order_ref=order_ref)


def _uncertain_since_ms(repo: ClerkSqliteRepository, order_ref: str) -> int:
    """When did this order first become "we don't yet know the outcome"?

    The last ``ORDER_SUBMIT_UNCERTAIN`` transition's timestamp if the broker
    call was attempted and its response lost; otherwise the accept
    transition's own timestamp (always present, always first) — covering the
    case where the process died before the broker call was ever attempted.
    """
    transitions = repo.transitions_for_order(order_ref)
    for transition in reversed(transitions):
        if transition["transition_kind"] == "ORDER_SUBMIT_UNCERTAIN":
            return transition["recorded_at_ms"]
    return transitions[0]["recorded_at_ms"]


async def _fold_uncertain_and_resolve(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    why: str,
    trade: BrokerTradePort,
) -> EnterSubmission:
    """Shared tail for every ``submit_enter`` outcome that means "we don't
    know what the broker did with this order" — a lost response
    (``BrokerUnavailable``) and a mismatched-identity response are the same
    epistemic situation despite arising from different code paths (an
    exception vs. a normal-flow identity check), so both fold ``unknown``
    and delegate to the same by-identity resolution rather than each
    inlining the two-step."""
    fold_uncertain(repo, effect_operation_id=effect_operation_id, order_ref=order_ref, why=why)
    return await resolve_enter_submission(repo, order_ref=order_ref, trade=trade)


def _snapshot(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, order_ref: str
) -> EnterSubmission:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    command = repo.get_command(effect.command_id)
    assert command is not None
    return EnterSubmission(
        command=command,
        effect_operation_id=effect_operation_id,
        order_ref=order_ref,
        created=True,
    )
