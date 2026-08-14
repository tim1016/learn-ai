"""Durable, exact-identity cancellation for an owned manual order.

The cancellation command is deliberately distinct from the submitted order:
the source order keeps its immutable execution provenance while this module
records the one stable operator cancel request and reconciles it by the
Clerk's client order reference.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.broker.alpaca.clerk.exposure import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.facts import (
    ManualOrderCancelAcceptedFacts,
    ManualOrderCancelResultFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    EffectOperationResource,
    ManualOrderCancellationResource,
    ManualOrderLegResource,
    OrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence, fold_uncertain
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.ports import BrokerTradePort

ACTION_CANCEL_MANUAL_ORDER = "CANCEL_MANUAL_ORDER"


class ManualOrderCancelConflictError(ValueError):
    """A stable cancellation target was reused with another request identity."""


class ManualOrderCancelOwnershipError(ValueError):
    """The target is not an order owned by the requesting manual subject."""


class ManualOrderCancelTerminalError(ValueError):
    """The owned manual order is already terminal and cannot be canceled."""


@dataclass(frozen=True)
class ManualOrderCancellationSubmission:
    """The stable cancellation resource returned across click/reload/restart."""

    cancellation: ManualOrderCancellationResource
    command: CommandResource
    effect: EffectOperationResource
    created: bool


def _cancel_ids(order_ref: str) -> tuple[str, str]:
    digest = hashlib.sha256(order_ref.encode("utf-8")).hexdigest()
    return f"cmd:manual-cancel:{digest}", f"effect:manual-cancel:{digest}"


def _identity(
    *,
    account_id: str,
    subject_id: str,
    order_ref: str,
    cancel_request_id: str,
) -> tuple[str, str, str, str]:
    command_id, effect_operation_id = _cancel_ids(order_ref)
    idempotency_key = f"{account_id}:{subject_id}:{order_ref}:{cancel_request_id}:{ACTION_CANCEL_MANUAL_ORDER}"
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "subject_id": subject_id,
                "order_ref": order_ref,
                "cancel_request_id": cancel_request_id,
                "action": ACTION_CANCEL_MANUAL_ORDER,
            }
        ).encode("utf-8")
    ).hexdigest()
    return idempotency_key, payload_hash, command_id, effect_operation_id


def _target_or_raise(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    subject_id: str,
    require_working: bool = False,
) -> tuple[ManualOrderLegResource, OrderResource, EffectOperationResource]:
    leg = repo.manual_order_leg_for_order_ref(order_ref=order_ref)
    order = repo.order(order_ref)
    if (
        leg is None
        or order is None
        or leg.subject_id != subject_id
        or leg.effect_operation_id != order.effect_operation_id
    ):
        raise ManualOrderCancelOwnershipError(
            "Manual cancellation requires an SQLite-owned order in the requesting manual custody subject."
        )
    source_effect = repo.effect_operation(order.effect_operation_id)
    if source_effect is None or source_effect.kind != "MANUAL_ORDER":
        raise ManualOrderCancelOwnershipError("Manual cancellation target has no owned manual effect.")
    if require_working and (
        source_effect.state in {"succeeded", "failed", "rejected"} or _terminal(order.broker_state)
    ):
        raise ManualOrderCancelTerminalError(
            "Manual cancellation requires a working manual order; this order is already terminal."
        )
    return leg, order, source_effect


def _submission(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    command_id: str,
    effect_operation_id: str,
    created: bool,
) -> ManualOrderCancellationSubmission:
    cancellation = repo.manual_order_cancellation(order_ref=order_ref)
    command = repo.get_command(command_id)
    effect = repo.effect_operation(effect_operation_id)
    if cancellation is None or command is None or effect is None:
        raise RuntimeError("manual cancellation disappeared after durable acceptance")
    return ManualOrderCancellationSubmission(
        cancellation=cancellation,
        command=command,
        effect=effect,
        created=created,
    )


def accept_manual_order_cancellation(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    operator_id: str,
    order_ref: str,
    cancel_request_id: str,
) -> ManualOrderCancellationSubmission:
    """Durably accept one cancel request before examining the broker."""
    subject_id = manual_operator_subject_id(operator_id)
    idempotency_key, payload_hash, command_id, effect_operation_id = _identity(
        account_id=account_id,
        subject_id=subject_id,
        order_ref=order_ref,
        cancel_request_id=cancel_request_id,
    )
    existing = repo.get_command(command_id)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise ManualOrderCancelConflictError(
                "manual cancellation identity conflicts with its durable request"
            )
        return _submission(
            repo,
            order_ref=order_ref,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            created=False,
        )

    def build_transition() -> TransitionInput:
        _target_or_raise(
            repo,
            order_ref=order_ref,
            subject_id=subject_id,
            require_working=True,
        )
        facts = ManualOrderCancelAcceptedFacts(
            order_ref=order_ref,
            subject_id=subject_id,
            operator_id=operator_id,
            cancel_request_id=cancel_request_id,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="manual_order",
            action=ACTION_CANCEL_MANUAL_ORDER,
            intended_end_state=None,
            effect_idempotency_key=f"manual:{idempotency_key}",
            effect_kind="CANCEL",
        )
        return TransitionInput(
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="MANUAL_ORDER_CANCEL_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="MANUAL_ORDER_CANCEL_ACCEPTED",
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
        return _submission(
            repo,
            order_ref=order_ref,
            command_id=command_id,
            effect_operation_id=effect_operation_id,
            created=False,
        )
    assert isinstance(outcome, CommandCreated)
    return _submission(
        repo,
        order_ref=order_ref,
        command_id=command_id,
        effect_operation_id=effect_operation_id,
        created=True,
    )


def _append_source_canceled(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    source_effect: EffectOperationResource,
    why: str,
) -> None:
    if source_effect.state in {"succeeded", "failed", "rejected"}:
        return
    facts = ManualOrderCancelResultFacts(outcome="CANCELED", why=why)
    repo.append_transition(
        TransitionInput(
            command_id=source_effect.command_id,
            effect_operation_id=source_effect.effect_operation_id,
            order_ref=order_ref,
            transition_kind="MANUAL_ORDER_CANCELED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="failed",
            clerk_observed_at_ms=repo.clock(),
            summary_code="MANUAL_ORDER_CANCELED",
            facts_json=facts.to_facts_json(),
        )
    )


def _append_cancellation_result(
    repo: ClerkSqliteRepository,
    *,
    cancellation: ManualOrderCancellationResource,
    succeeded: bool,
    why: str,
) -> None:
    effect = repo.effect_operation(cancellation.effect_operation_id)
    if effect is None or effect.state in {"succeeded", "failed", "rejected"}:
        return
    facts = ManualOrderCancelResultFacts(
        outcome="CANCELED" if succeeded else "TARGET_TERMINAL",
        why=why,
    )
    repo.append_transition(
        TransitionInput(
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            order_ref=cancellation.order_ref,
            transition_kind=(
                "MANUAL_ORDER_CANCEL_CONFIRMED"
                if succeeded
                else "MANUAL_ORDER_CANCEL_TERMINAL"
            ),
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded" if succeeded else "failed",
            clerk_observed_at_ms=repo.clock(),
            summary_code=(
                "MANUAL_ORDER_CANCEL_CONFIRMED"
                if succeeded
                else "MANUAL_ORDER_CANCEL_TARGET_TERMINAL"
            ),
            facts_json=facts.to_facts_json(),
        )
    )


def _terminal(order_state: str | None) -> bool:
    return order_state is not None and order_state.lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


async def _resolve_claimed_manual_order_cancellation(
    repo: ClerkSqliteRepository,
    *,
    cancellation: ManualOrderCancellationResource,
    broker: ClaimedBrokerIO,
) -> None:
    """Use exact source-order evidence after the one durable cancel intent."""
    _leg, target, source_effect = _target_or_raise(
        repo,
        order_ref=cancellation.order_ref,
        subject_id=cancellation.subject_id,
    )
    if source_effect.state in {"succeeded", "failed", "rejected"}:
        _append_cancellation_result(
            repo,
            cancellation=cancellation,
            succeeded=False,
            why="The owned manual order was already terminal before this cancellation could act.",
        )
        return
    if source_effect.state == "accepted" and target.broker_order_id is None:
        _append_source_canceled(
            repo,
            order_ref=target.order_ref,
            source_effect=source_effect,
            why="The manual leg was canceled locally before broker activation.",
        )
        _append_cancellation_result(
            repo,
            cancellation=cancellation,
            succeeded=True,
            why="The manual leg had not reached the broker and was canceled locally.",
        )
        return

    observed = await broker.observe_exact(target.client_order_id)
    if isinstance(observed, BrokerError) or observed is None:
        fold_uncertain(
            repo,
            effect_operation_id=cancellation.effect_operation_id,
            order_ref=target.order_ref,
            why=(str(observed) if isinstance(observed, BrokerError) else "No exact order evidence yet."),
            transition_kind="ORDER_CANCEL_UNCERTAIN",
        )
        return
    fold_order_evidence(repo, effect_operation_id=source_effect.effect_operation_id, order=observed)
    target = repo.order(target.order_ref)
    source_effect = repo.effect_operation(source_effect.effect_operation_id)
    assert target is not None and source_effect is not None
    if _terminal(target.broker_state):
        if target.broker_state is not None and target.broker_state.lower() == "canceled":
            _append_source_canceled(
                repo,
                order_ref=target.order_ref,
                source_effect=source_effect,
                why="Exact broker evidence proved the manual order canceled.",
            )
        _append_cancellation_result(
            repo,
            cancellation=cancellation,
            succeeded=False,
            why="The owned manual order was already terminal at the broker.",
        )
        return
    if target.broker_order_id is None:
        fold_uncertain(
            repo,
            effect_operation_id=cancellation.effect_operation_id,
            order_ref=target.order_ref,
            why="Exact broker evidence did not include a broker order identity to cancel.",
            transition_kind="ORDER_CANCEL_UNCERTAIN",
        )
        return
    cancel_error: BrokerError | None = None
    try:
        await broker.cancel(target.broker_order_id, order_ref=target.order_ref)
    except BrokerUnavailable as exc:
        fold_uncertain(
            repo,
            effect_operation_id=cancellation.effect_operation_id,
            order_ref=target.order_ref,
            why=str(exc),
            transition_kind="ORDER_CANCEL_UNCERTAIN",
        )
        return
    except BrokerError as exc:
        # The exact lookup below, rather than a cancel transport error, is the
        # authority for whether the request reached Alpaca.
        cancel_error = exc
    observed_after = await broker.observe_exact(target.client_order_id)
    if isinstance(observed_after, BrokerError) or observed_after is None:
        fold_uncertain(
            repo,
            effect_operation_id=cancellation.effect_operation_id,
            order_ref=target.order_ref,
            why=(
                str(observed_after)
                if isinstance(observed_after, BrokerError)
                else (
                    f"{cancel_error}; cancellation response had no exact broker follow-up evidence."
                    if cancel_error is not None
                    else "Cancellation response had no exact broker follow-up evidence."
                )
            ),
            transition_kind="ORDER_CANCEL_UNCERTAIN",
        )
        return
    fold_order_evidence(repo, effect_operation_id=source_effect.effect_operation_id, order=observed_after)
    target_after = repo.order(target.order_ref)
    source_after = repo.effect_operation(source_effect.effect_operation_id)
    assert target_after is not None and source_after is not None
    if target_after.broker_state is not None and target_after.broker_state.lower() == "canceled":
        _append_source_canceled(
            repo,
            order_ref=target_after.order_ref,
            source_effect=source_after,
            why="Exact broker evidence proved this durable cancel request canceled the manual order.",
        )
        _append_cancellation_result(
            repo,
            cancellation=cancellation,
            succeeded=True,
            why="Exact broker evidence proved the manual order canceled.",
        )
    elif _terminal(target_after.broker_state):
        _append_cancellation_result(
            repo,
            cancellation=cancellation,
            succeeded=False,
            why="The manual order became terminal before cancellation could be proven.",
        )


async def resolve_manual_order_cancellation(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
    trade: BrokerTradePort,
) -> ManualOrderCancellationSubmission:
    """Resume the one durable cancellation, including after a process restart."""
    cancellation = repo.manual_order_cancellation_for_effect(effect_operation_id=effect_operation_id)
    if cancellation is None:
        raise ManualOrderCancelOwnershipError("Cancellation recovery has no owned manual target.")
    effect = repo.effect_operation(effect_operation_id)
    if effect is None:
        raise RuntimeError("manual cancellation effect disappeared")
    if effect.state not in {"succeeded", "failed", "rejected"}:
        claim = repo.claim_before_broker_contact(effect_operation_id)
        broker = ClaimedBrokerIO(
            repo=repo,
            effect_operation_id=effect_operation_id,
            claim_token=claim.token,
            trade=trade,
        )
        try:
            await _resolve_claimed_manual_order_cancellation(
                repo,
                cancellation=cancellation,
                broker=broker,
            )
        finally:
            repo.release_operation_claim(effect_operation_id=effect_operation_id, token=claim.token)
    return _submission(
        repo,
        order_ref=cancellation.order_ref,
        command_id=cancellation.command_id,
        effect_operation_id=cancellation.effect_operation_id,
        created=False,
    )


async def submit_manual_order_cancellation(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    operator_id: str,
    order_ref: str,
    cancel_request_id: str,
    trade: BrokerTradePort,
) -> ManualOrderCancellationSubmission:
    """Accept once and attempt the first exact cancellation reconciliation."""
    accepted = accept_manual_order_cancellation(
        repo,
        account_id=account_id,
        operator_id=operator_id,
        order_ref=order_ref,
        cancel_request_id=cancel_request_id,
    )
    if not accepted.created:
        return accepted
    return await resolve_manual_order_cancellation(
        repo,
        effect_operation_id=accepted.effect.effect_operation_id,
        trade=trade,
    )


__all__ = [
    "ACTION_CANCEL_MANUAL_ORDER",
    "ManualOrderCancelConflictError",
    "ManualOrderCancelOwnershipError",
    "ManualOrderCancelTerminalError",
    "ManualOrderCancellationSubmission",
    "accept_manual_order_cancellation",
    "resolve_manual_order_cancellation",
    "submit_manual_order_cancellation",
]
