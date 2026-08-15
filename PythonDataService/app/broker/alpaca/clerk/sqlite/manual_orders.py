"""SQLite-native ordered manual-ticket acceptance and broker submission.

This is deliberately separate from ``enter.py``: a manual ticket has no
strategy instance or run, and attaching it to one would corrupt bot custody.
The shared repository and evidence folds still provide the single durable
command/effect/order state machine.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.facts import (
    CustodySubjectRegisteredFacts,
    ManualOrderAcceptedFacts,
    ManualTicketLegReservedFacts,
    ManualTicketReservedFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.models import (
    CommandCreated,
    CommandExistingConflict,
    CommandExistingSame,
    CommandResource,
    ControlMetaSnapshot,
    ManualOrderLegResource,
    ManualOrderTicketResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    ReductionIntent,
    require_manual_admission,
    require_manual_reduction,
)
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.broker.contract.ports import BrokerTradePort
from app.engine.live.identity import validate_strategy_instance_id
from app.engine.live.order_identity import build_manual_order_namespace, build_order_ref, mint_intent_id

ACTION_SUBMIT_MANUAL_ORDER = "SUBMIT_MANUAL_ORDER"


class ManualTicketConflictError(ValueError):
    """A stable browser ticket UUID was reused with another immutable leg."""


class ManualTicketContinuationError(ValueError):
    """A later ticket leg was not explicitly eligible for activation."""


@dataclass(frozen=True)
class ManualPreviewRevision:
    """The one atomic Clerk revision a manual preview authorizes."""

    authority_generation: int
    db_identity_token: str
    control_revision: int

    @classmethod
    def from_meta(cls, meta: ControlMetaSnapshot) -> ManualPreviewRevision:
        return cls(
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            control_revision=meta.control_revision,
        )

    def matches(self, meta: ControlMetaSnapshot) -> bool:
        return self == self.from_meta(meta)


@dataclass(frozen=True)
class ManualTicketLeg:
    """One browser-stable ordered ticket leg before it reaches broker custody."""

    leg_id: str
    instruction: BrokerOrderLeg


@dataclass(frozen=True)
class ManualOrderSubmission:
    """Current resources for a single ticket leg, after one submit attempt."""

    ticket: ManualOrderTicketResource
    leg: ManualOrderLegResource
    command: CommandResource
    created: bool


def manual_instruction_hash(leg: BrokerOrderLeg) -> str:
    """Hash the complete normalized broker instruction, never display input."""
    return hashlib.sha256(canonicalize(leg.model_dump(mode="json")).encode("utf-8")).hexdigest()


def _ticket_instruction_hash(
    *,
    account_id: str,
    subject_id: str,
    ticket_id: str,
    legs: tuple[ManualTicketLeg, ...],
) -> str:
    return hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "subject_id": subject_id,
                "ticket_id": ticket_id,
                "legs": [
                    {"leg_id": leg.leg_id, "instruction_hash": manual_instruction_hash(leg.instruction)} for leg in legs
                ],
            }
        ).encode("utf-8")
    ).hexdigest()


def _identity(
    *,
    account_id: str,
    subject_id: str,
    ticket_id: str,
    leg_id: str,
    leg: BrokerOrderLeg,
) -> tuple[str, str, str, str, str]:
    instruction_hash = manual_instruction_hash(leg)
    idempotency_key = f"{account_id}:{subject_id}:{ticket_id}:{leg_id}:{ACTION_SUBMIT_MANUAL_ORDER}"
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "account_id": account_id,
                "subject_id": subject_id,
                "ticket_id": ticket_id,
                "leg_id": leg_id,
                "instruction": leg.model_dump(mode="json"),
            }
        ).encode("utf-8")
    ).hexdigest()
    command_id = manual_order_command_id(ticket_id, leg_id)
    effect_idempotency_key = f"manual:{idempotency_key}"
    return idempotency_key, payload_hash, command_id, effect_idempotency_key, instruction_hash


def manual_order_command_id(ticket_id: str, leg_id: str) -> str:
    """Return the client-stable command identity for one ticket leg."""
    return f"cmd:manual:{ticket_id}:{leg_id}"


def _require_supported_leg(leg: BrokerOrderLeg) -> None:
    if (
        leg.side not in {OrderSide.BUY, OrderSide.SELL}
        or leg.order_type not in {OrderType.MARKET, OrderType.LIMIT}
        or leg.time_in_force not in {TimeInForce.DAY, TimeInForce.GTC}
    ):
        raise ValueError("manual tickets support only BUY or SELL market/limit DAY/GTC equity legs")


def _require_ticket_legs(legs: tuple[ManualTicketLeg, ...]) -> None:
    if not legs or len(legs) > 8:
        raise ManualTicketConflictError("manual tickets require between one and eight ordered legs")
    leg_ids = [leg.leg_id for leg in legs]
    if any(not leg_id for leg_id in leg_ids) or len(set(leg_ids)) != len(leg_ids):
        raise ManualTicketConflictError("manual tickets require unique stable leg identities")
    for leg in legs:
        _require_supported_leg(leg.instruction)


def _require_manual_leg_admission(
    repo: ClerkSqliteRepository,
    *,
    subject_id: str,
    leg: BrokerOrderLeg,
    continuation_ticket_id: str | None = None,
) -> None:
    # ``accept_manual_order`` invokes this planner while
    # ``commit_first_transition`` holds the repository's reentrant write
    # coordinator.  Keep the reduction read here: moving it before that
    # commit primitive would split the available-quantity check from the
    # durable MANUAL_ORDER_ACCEPTED reservation and reopen an oversell race.
    if leg.side is OrderSide.BUY:
        require_manual_admission(
            repo,
            subject_id=subject_id,
            continuation_ticket_id=continuation_ticket_id,
        )
        return
    require_manual_reduction(
        repo,
        subject_id=subject_id,
        intent=ReductionIntent(symbol=leg.symbol, side=leg.side.value, quantity=leg.quantity),
    )


def _submission(
    repo: ClerkSqliteRepository,
    *,
    ticket_id: str,
    leg_id: str,
    command: CommandResource,
    created: bool,
) -> ManualOrderSubmission:
    ticket = repo.manual_order_ticket(ticket_id)
    if ticket is None:
        raise RuntimeError(f"manual ticket {ticket_id!r} disappeared after durable acceptance")
    leg = next((item for item in ticket.legs if item.leg_id == leg_id), None)
    if leg is None or leg.command_id != command.command_id:
        raise RuntimeError(f"manual ticket leg {leg_id!r} is not bound to command {command.command_id!r}")
    return ManualOrderSubmission(ticket=ticket, leg=leg, command=command, created=created)


def next_manual_ticket_leg(
    repo: ClerkSqliteRepository,
    *,
    ticket_id: str,
) -> ManualTicketLeg:
    """Return the one next reserved leg after broker acknowledgement.

    This is deliberately a read-only eligibility check. The caller must still
    present a fresh preview token and invoke the explicit continuation path
    before this returned instruction can become a broker effect.
    """
    ticket = repo.manual_order_ticket(ticket_id)
    if ticket is None:
        raise ManualTicketContinuationError("manual ticket does not exist")
    for index, resource in enumerate(ticket.legs):
        if resource.state != "RESERVED":
            continue
        if index == 0:
            if resource.instruction is None:
                raise ManualTicketContinuationError(
                    "this legacy ticket lacks durable instruction evidence for a safe activation"
                )
            return ManualTicketLeg(resource.leg_id, BrokerOrderLeg.model_validate(resource.instruction))
        prior_states = {leg.state for leg in ticket.legs[:index]}
        for prior_leg in ticket.legs[:index]:
            if prior_leg.order_ref is None:
                continue
            cancellation = repo.manual_order_cancellation(order_ref=prior_leg.order_ref)
            if cancellation is not None and cancellation.state in {"ACCEPTED", "UNKNOWN"}:
                raise ManualTicketContinuationError(
                    "the ticket remains paused until the prior manual cancellation is terminal"
                )
        if "UNKNOWN" in prior_states:
            raise ManualTicketContinuationError(
                "the ticket remains paused until the prior manual order outcome is reconciled"
            )
        if prior_states & {"RESERVED", "ACCEPTED"}:
            raise ManualTicketContinuationError("the prior manual ticket leg has not reached broker acknowledgement")
        if resource.instruction is None:
            raise ManualTicketContinuationError(
                "this legacy ticket lacks durable instruction evidence for a safe continuation"
            )
        return ManualTicketLeg(resource.leg_id, BrokerOrderLeg.model_validate(resource.instruction))
    raise ManualTicketContinuationError("manual ticket has no remaining leg to continue")


def _ensure_subject_and_ticket(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    operator_id: str,
    subject_id: str,
    ticket_id: str,
    legs: tuple[ManualTicketLeg, ...],
) -> None:
    if repo.custody_subject(subject_id) is None:
        subject_facts = CustodySubjectRegisteredFacts(
            subject_id=subject_id,
            kind="MANUAL_OPERATOR",
            strategy_instance_id=None,
            operator_id=operator_id,
        )
        repo.append_transition(
            TransitionInput(
                transition_kind="CUSTODY_SUBJECT_REGISTERED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                clerk_observed_at_ms=repo.clock(),
                summary_code="CUSTODY_SUBJECT_REGISTERED",
                facts_json=subject_facts.to_facts_json(),
            )
        )
    ticket_hash = _ticket_instruction_hash(
        account_id=account_id,
        subject_id=subject_id,
        ticket_id=ticket_id,
        legs=legs,
    )
    ticket = repo.manual_order_ticket(ticket_id)
    if ticket is not None:
        persisted_legs = tuple((item.leg_id, item.sequence_index, item.instruction_hash) for item in ticket.legs)
        expected_legs = tuple(
            (leg.leg_id, index, manual_instruction_hash(leg.instruction)) for index, leg in enumerate(legs)
        )
        if (
            ticket.subject_id != subject_id
            or ticket.operator_id != operator_id
            or ticket.instruction_hash != ticket_hash
            or persisted_legs != expected_legs
        ):
            raise ManualTicketConflictError("manual ticket identity conflicts with its durable reservation")
        return
    ticket_facts = ManualTicketReservedFacts(
        ticket_id=ticket_id,
        subject_id=subject_id,
        operator_id=operator_id,
        instruction_hash=ticket_hash,
        legs=tuple(
            ManualTicketLegReservedFacts(
                leg_id=leg.leg_id,
                instruction_hash=manual_instruction_hash(leg.instruction),
                instruction=leg.instruction.model_dump(mode="json"),
            )
            for leg in legs
        ),
    )
    repo.append_transition(
        TransitionInput(
            transition_kind="MANUAL_TICKET_RESERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="MANUAL_TICKET_RESERVED",
            facts_json=ticket_facts.to_facts_json(),
        )
    )


def accept_manual_order(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    operator_id: str,
    ticket_id: str,
    leg_id: str,
    leg: BrokerOrderLeg,
    ticket_legs: tuple[ManualTicketLeg, ...] | None = None,
    continuation: bool = False,
    expected_preview_revision: ManualPreviewRevision | None = None,
) -> ManualOrderSubmission:
    """Finalize one ticket leg locally before the caller may contact Alpaca."""
    ticket_legs = ticket_legs or (ManualTicketLeg(leg_id=leg_id, instruction=leg),)
    _require_ticket_legs(ticket_legs)
    requested_leg = next((item for item in ticket_legs if item.leg_id == leg_id), None)
    if requested_leg is None or requested_leg.instruction != leg:
        raise ManualTicketConflictError("manual leg does not match its immutable ticket reservation")
    validate_strategy_instance_id(operator_id)
    subject_id = manual_operator_subject_id(operator_id)
    idempotency_key, payload_hash, command_id, effect_key, instruction_hash = _identity(
        account_id=account_id,
        subject_id=subject_id,
        ticket_id=ticket_id,
        leg_id=leg_id,
        leg=leg,
    )
    existing = repo.get_command(command_id)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise DurableConflictError(existing)
        _ensure_subject_and_ticket(
            repo,
            account_id=account_id,
            operator_id=operator_id,
            subject_id=subject_id,
            ticket_id=ticket_id,
            legs=ticket_legs,
        )
        return _submission(
            repo,
            ticket_id=ticket_id,
            leg_id=leg_id,
            command=existing,
            created=False,
        )

    def build_transition() -> TransitionInput:
        if expected_preview_revision is not None:
            meta = repo.control_meta_snapshot()
            if not expected_preview_revision.matches(meta):
                raise ManualTicketContinuationError(
                    "The manual-order preview is stale. Refresh the ticket before confirming."
                )
        _ensure_subject_and_ticket(
            repo,
            account_id=account_id,
            operator_id=operator_id,
            subject_id=subject_id,
            ticket_id=ticket_id,
            legs=ticket_legs,
        )
        ticket = repo.manual_order_ticket(ticket_id)
        if ticket is None:
            raise RuntimeError("manual ticket disappeared while accepting its first leg")
        current_index = next(
            (index for index, item in enumerate(ticket.legs) if item.leg_id == leg_id),
            None,
        )
        if current_index is None or ticket.legs[current_index].state != "RESERVED":
            raise ManualTicketContinuationError("this manual ticket leg is not awaiting activation")
        if current_index == 0 and continuation:
            raise ManualTicketContinuationError("the first manual ticket leg must use initial confirmation")
        if current_index > 0:
            if not continuation:
                raise ManualTicketContinuationError(
                    "later manual ticket legs require an explicit Continue remaining legs confirmation"
                )
            next_leg = next_manual_ticket_leg(repo, ticket_id=ticket_id)
            if next_leg.leg_id != leg_id:
                raise ManualTicketContinuationError("this manual ticket leg is not the next eligible continuation")
        _require_manual_leg_admission(
            repo,
            subject_id=subject_id,
            leg=leg,
            continuation_ticket_id=ticket_id if continuation else None,
        )
        order_ref = build_order_ref(build_manual_order_namespace(operator_id), mint_intent_id())
        facts = ManualOrderAcceptedFacts(
            ticket_id=ticket_id,
            leg_id=leg_id,
            subject_id=subject_id,
            operator_id=operator_id,
            instruction_hash=instruction_hash,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            kind="manual_order",
            action=ACTION_SUBMIT_MANUAL_ORDER,
            intended_end_state=None,
            effect_idempotency_key=effect_key,
            effect_kind="MANUAL_ORDER",
            leg=leg.model_dump(mode="json"),
        )
        return TransitionInput(
            command_id=command_id,
            effect_operation_id=f"effect:manual:{ticket_id}:{leg_id}",
            order_ref=order_ref,
            transition_kind="MANUAL_ORDER_ACCEPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="MANUAL_ORDER_ACCEPTED",
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
            ticket_id=ticket_id,
            leg_id=leg_id,
            command=outcome.command,
            created=False,
        )
    assert isinstance(outcome, CommandCreated)
    return _submission(
        repo,
        ticket_id=ticket_id,
        leg_id=leg_id,
        command=outcome.command,
        created=True,
    )


async def submit_manual_order(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    operator_id: str,
    ticket_id: str,
    leg_id: str,
    leg: BrokerOrderLeg,
    trade: BrokerTradePort,
    ticket_legs: tuple[ManualTicketLeg, ...] | None = None,
    continuation: bool = False,
    expected_preview_revision: ManualPreviewRevision | None = None,
) -> ManualOrderSubmission:
    """Accept once, claim once, and submit the exact durable order identity."""
    accepted = accept_manual_order(
        repo,
        account_id=account_id,
        operator_id=operator_id,
        ticket_id=ticket_id,
        leg_id=leg_id,
        leg=leg,
        ticket_legs=ticket_legs,
        continuation=continuation,
        expected_preview_revision=expected_preview_revision,
    )
    if not accepted.created:
        return accepted
    assert accepted.leg.effect_operation_id is not None and accepted.leg.order_ref is not None
    from app.broker.alpaca.clerk.sqlite.order_evidence import (
        fold_failed,
        fold_order_submission_acknowledgement,
        fold_uncertain,
    )

    claim = repo.claim_before_broker_contact(accepted.leg.effect_operation_id)
    broker = ClaimedBrokerIO(
        repo=repo,
        effect_operation_id=accepted.leg.effect_operation_id,
        claim_token=claim.token,
        trade=trade,
    )
    unexpected_error: Exception | None = None
    try:
        try:
            order = await broker.submit(leg, client_order_id=accepted.leg.order_ref)
        except BrokerUnavailable as exc:
            fold_uncertain(
                repo,
                effect_operation_id=accepted.leg.effect_operation_id,
                order_ref=accepted.leg.order_ref,
                why=str(exc),
            )
        except BrokerError as exc:
            fold_failed(
                repo,
                effect_operation_id=accepted.leg.effect_operation_id,
                order_ref=accepted.leg.order_ref,
                summary_code="ORDER_SUBMIT_FAILED",
                reason="The manual order did not reach Alpaca.",
                why=str(exc),
            )
        except Exception as exc:
            fold_uncertain(
                repo,
                effect_operation_id=accepted.leg.effect_operation_id,
                order_ref=accepted.leg.order_ref,
                why=f"manual broker submission raised {type(exc).__name__}: {exc}",
            )
            unexpected_error = exc
        else:
            if order.client_order_id == accepted.leg.order_ref:
                fold_order_submission_acknowledgement(
                    repo,
                    effect_operation_id=accepted.leg.effect_operation_id,
                    order=order,
                )
            else:
                fold_uncertain(
                    repo,
                    effect_operation_id=accepted.leg.effect_operation_id,
                    order_ref=accepted.leg.order_ref,
                    why=(
                        f"broker returned client_order_id={order.client_order_id!r}, "
                        f"expected {accepted.leg.order_ref!r}"
                    ),
                )
    finally:
        repo.release_operation_claim(
            effect_operation_id=accepted.leg.effect_operation_id,
            token=claim.token,
        )
    if unexpected_error is not None:
        raise unexpected_error
    command = repo.get_command(accepted.command.command_id)
    assert command is not None
    return _submission(
        repo,
        ticket_id=ticket_id,
        leg_id=leg_id,
        command=command,
        created=True,
    )


__all__ = [
    "ACTION_SUBMIT_MANUAL_ORDER",
    "ManualOrderSubmission",
    "ManualPreviewRevision",
    "ManualTicketConflictError",
    "ManualTicketContinuationError",
    "ManualTicketLeg",
    "accept_manual_order",
    "manual_instruction_hash",
    "manual_order_command_id",
    "next_manual_ticket_leg",
    "submit_manual_order",
]
