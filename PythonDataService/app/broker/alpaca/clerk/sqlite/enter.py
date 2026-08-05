"""Strategy-decision ENTER over the SQLite spine (#1377).

Proves the R1 capture-before-contact fence and the R4 lost-response
resolution discipline end-to-end through one broker-facing command: opening
a position. Two phases, deliberately separable:

- :func:`accept_enter` — purely local. Reserves the content-addressed
  ``(strategy_instance_id, decision_id)`` identity, mints the order identity,
  and commits the effect operation + order row through the full R9 mirror
  fence. No broker call happens inside it, and none may happen before it
  returns (R1).
- :func:`submit_enter` — calls :func:`accept_enter`, then (only for a fresh
  reservation) calls the broker. A lost response (``BrokerUnavailable``) is
  never fabricated into a terminal outcome; it folds ``unknown`` and hands
  off to :func:`resolve_enter_submission` immediately, the same function a
  later, out-of-process recovery sweep would call for an intent that never
  even reached the broker (accepted, then the process died before the try
  block ran at all — indistinguishable from the caller's side, and resolved
  identically: by asking the broker whether ``order_ref`` landed).

Every timestamp this module writes comes from ``repo.clock`` — the same
clock the repository was constructed with, never an independently-passed
one. The R4 grace window compares "when did this order first become
uncertain" (a value read back from ``custody_transitions.recorded_at_ms``,
which is always stamped by the repository's own clock) against "now"; those
two must share a time source or the comparison is meaningless. In
production both are ``now_ms_utc`` so this never bites, but a test wiring a
controllable fake clock into the repository and a *different* one into this
module would silently break the grace math — reading the repo's own clock
here removes that whole class of mistake instead of relying on every caller
to pass the same clock twice.

Deliberately deferred to later slices: the effect operation never reaches a
terminal ``succeeded`` state in this module (that requires knowing an ENTER
is "done" — fully filled vs. still working — which is EXIT/reconciliation
territory, #1378/#1379); ``is_correction`` fills are stored but not given
special reversal handling; admission gating against open holds/uncertainties
is R6 (#1380), not this slice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.broker.alpaca.clerk.recovery import UNCERTAIN_SUBMIT_GRACE_MS
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    reject_colon,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    CommandResource,
    ReservationConflict,
    ReservedExisting,
    ReservedNew,
    TransitionInput,
)
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
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


def accept_enter(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    leg: BrokerOrderLeg,
) -> EnterSubmission:
    """Reserve + accept, entirely local (no broker call). R1's fence.

    A fresh reservation commits the effect operation and order row (mirror
    finalize included) before returning — that commit is what "one accepted
    operation" means for recovery: nothing about a broker call is durable
    yet, so there is nothing for recovery to duplicate, only to resolve.
    """
    reject_colon("strategy_instance_id", strategy_instance_id)
    reject_colon("decision_id", decision_id)

    idempotency_key = f"{strategy_instance_id}:{decision_id}"
    command_id = f"cmd:{idempotency_key}"
    leg_payload = leg.model_dump(mode="json")
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "decision_id": decision_id,
                "action": ACTION_ENTER,
                "leg": leg_payload,
            }
        ).encode("utf-8")
    ).hexdigest()

    with repo.serialized():
        outcome = repo.reserve_command(
            command_id=command_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="strategy_decision",
            strategy_instance_id=strategy_instance_id,
            run_id=None,
            action=ACTION_ENTER,
            intended_end_state=None,
        )
        if isinstance(outcome, ReservationConflict):
            raise DurableConflictError(outcome.command)
        if isinstance(outcome, ReservedExisting):
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
        assert isinstance(outcome, ReservedNew)

        namespace = build_bot_order_namespace(strategy_instance_id)
        order_ref = build_order_ref(namespace, mint_intent_id())
        effect_operation_id = f"effect:{idempotency_key}"

        repo.append_transition(
            TransitionInput(
                strategy_instance_id=strategy_instance_id,
                command_id=command_id,
                effect_operation_id=effect_operation_id,
                order_ref=order_ref,
                transition_kind="ENTER_ACCEPTED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                clerk_observed_at_ms=repo.clock(),
                summary_code="ENTER_ACCEPTED",
                facts_json=canonicalize(
                    {
                        "effect_idempotency_key": f"enter:{idempotency_key}",
                        "decision_id": decision_id,
                        "leg": leg_payload,
                    }
                ),
            )
        )
        command = repo.get_command(command_id)
        assert command is not None
        return EnterSubmission(
            command=command,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            created=True,
        )


async def submit_enter(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    decision_id: str,
    leg: BrokerOrderLeg,
    trade: BrokerTradePort,
) -> EnterSubmission:
    """Accept, then (only for a fresh reservation) call the broker.

    A duplicate decision (transport retry or a genuine re-request while the
    original is still working) returns the existing resource from
    :func:`accept_enter` alone — no second broker call, satisfying "one
    broker intent" regardless of how many callers raced to submit it.
    """
    accepted = accept_enter(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        leg=leg,
    )
    if not accepted.created:
        return accepted

    assert accepted.effect_operation_id is not None and accepted.order_ref is not None
    try:
        order = await trade.submit(leg, client_order_id=accepted.order_ref)
    except BrokerUnavailable as exc:
        _fold_uncertain(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            why=str(exc),
        )
        return await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)
    except BrokerError as exc:
        _fold_failed(
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
        _fold_failed(
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


def fold_order_evidence(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order: BrokerOrder,
) -> None:
    """The one gate for "what does an observed ``BrokerOrder`` mean" — the
    happy-path submit ack and :func:`resolve_enter_submission`'s found-order
    case both route through this, not two copies. Folds the acknowledgement
    (idempotent on ``orders.broker_state`` via §3c's no-regression rule) and,
    if the snapshot reports fill progress, the fill (idempotent and
    namespace-attributed via ``_fold_order_fill_observed``).
    """
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    order_ref = order.client_order_id
    assert order_ref is not None

    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            broker_order_id=order.order_id,
            broker_state=order.status,
            transition_kind="ORDER_SUBMIT_ACKED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            source_event_at_ms=order.updated_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="ORDER_SUBMIT_ACKED",
            facts_json=canonicalize({}),
        )
    )

    if order.filled_quantity > 0 and order.filled_avg_price is not None:
        repo.append_transition(
            TransitionInput(
                strategy_instance_id=effect.strategy_instance_id,
                command_id=effect.command_id,
                effect_operation_id=effect_operation_id,
                order_ref=order_ref,
                transition_kind="ORDER_FILL_OBSERVED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                source_event_at_ms=order.updated_at_ms,
                clerk_observed_at_ms=repo.clock(),
                summary_code="ORDER_FILL_OBSERVED",
                facts_json=canonicalize(
                    {
                        "symbol": order.symbol,
                        "side": order.side.upper(),
                        "cumulative_filled_quantity": order.filled_quantity,
                        "avg_price": order.filled_avg_price,
                        "is_correction": False,
                    }
                ),
            )
        )


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


def _fold_uncertain(
    repo: ClerkSqliteRepository, *, effect_operation_id: str, order_ref: str, why: str
) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="ORDER_SUBMIT_UNCERTAIN",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="unknown",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ORDER_SUBMIT_UNCERTAIN",
            facts_json=canonicalize({"why": why}),
        )
    )


def _fold_failed(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    order_ref: str,
    summary_code: str,
    reason: str,
    why: str,
) -> None:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="ORDER_SUBMIT_FAILED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="failed",
            clerk_observed_at_ms=repo.clock(),
            summary_code=summary_code,
            facts_json=canonicalize({"reason": reason, "why": why}),
        )
    )


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
