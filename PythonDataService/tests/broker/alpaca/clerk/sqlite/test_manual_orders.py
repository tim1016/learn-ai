"""Manual market-tracer custody tests: one operator subject, never a pseudo-bot."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import (
    CustodySubjectRegisteredFacts,
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
    ManualOrderCancelResultFacts,
    ManualTicketLegReservedFacts,
    ManualTicketReservedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancelOwnershipError,
    ManualOrderCancelTerminalError,
    accept_manual_order_cancellation,
    resolve_manual_order_cancellation,
    submit_manual_order_cancellation,
    submit_manual_ticket_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import (
    ManualPreviewRevision,
    ManualTicketContinuationError,
    ManualTicketLeg,
    accept_manual_order,
    next_manual_ticket_leg,
    submit_manual_order,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    decide_capability,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

ACCOUNT_ID = "PA-TEST"
OPERATOR_ID = "operator"
TICKET_ID = "7de3a77c-b698-4e0d-a5d1-2f624574ed35"
LEG_ID = "09d6d63e-6375-4e6d-8d20-3b1bf70c2465"


class FakeTrade:
    broker_id = "alpaca"

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        unavailable: bool = False,
        unexpected: bool = False,
        mismatched_client_order_id: bool = False,
        cancel_unavailable: bool = False,
        cancel_error: BrokerError | None = None,
    ) -> None:
        self.repo = repo
        self.unavailable = unavailable
        self.cancel_unavailable = cancel_unavailable
        self.cancel_error = cancel_error
        self.unexpected = unexpected
        self.mismatched_client_order_id = mismatched_client_order_id
        self.submit_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.orders: dict[str, BrokerOrder] = {}
        self.order_was_durable_at_submit = False

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        ticket = self.repo.manual_order_ticket(TICKET_ID)
        self.order_was_durable_at_submit = bool(
            ticket and ticket.legs[0].order_ref == client_order_id and ticket.legs[0].effect_operation_id is not None
        )
        if self.unavailable:
            raise BrokerUnavailable("response lost", broker="alpaca")
        if self.unexpected:
            raise RuntimeError("malformed broker response")
        order = BrokerOrder(
            broker="alpaca",
            order_id=f"broker-order-{len(self.submit_calls)}",
            client_order_id=("wrong-client-order-id" if self.mismatched_client_order_id else client_order_id),
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side.value,
            order_type=leg.order_type.value,
            time_in_force=leg.time_in_force.value,
            quantity=leg.quantity,
            filled_quantity=0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=1_700_000_000_100,
            created_at_ms=1_700_000_000_100,
            updated_at_ms=1_700_000_000_100,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=1_700_000_000_100,
        )
        self.orders[client_order_id] = order
        return order

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self.cancel_unavailable:
            raise BrokerUnavailable("cancel response lost", broker="alpaca")
        if self.cancel_error is not None:
            raise self.cancel_error
        for client_order_id, order in self.orders.items():
            if order.order_id == order_id:
                self.orders[client_order_id] = order.model_copy(
                    update={
                        "status": "canceled",
                        "canceled_at_ms": 1_700_000_000_300,
                        "updated_at_ms": 1_700_000_000_300,
                        "observed_at_ms": 1_700_000_000_300,
                    }
                )
                return
        raise AssertionError(f"unknown broker order {order_id!r}")

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)


@pytest.fixture
def repo(tmp_path: Path):
    repository = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_700_000_000_000),
    )
    repository.register_strategy_instance(
        strategy_instance_id="bot-1",
        symbol="SPY",
        config_hash="bot-config",
    )
    yield repository
    repository.close()


def market_buy(quantity: float = 1) -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=quantity)


def filled_order(order_ref: str) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id="broker-order-1",
        client_order_id=order_ref,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=1,
        filled_quantity=1,
        limit_price=None,
        stop_price=None,
        filled_avg_price=500,
        status="filled",
        submitted_at_ms=1_700_000_000_100,
        created_at_ms=1_700_000_000_100,
        updated_at_ms=1_700_000_000_300,
        filled_at_ms=1_700_000_000_300,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_300,
    )


def market_sell(quantity: float = 1) -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="sell", quantity=quantity)


@pytest.mark.asyncio
async def test_manual_order_is_durable_before_broker_contact_and_never_a_bot(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)

    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )

    assert trade.order_was_durable_at_submit
    assert len(trade.submit_calls) == 1
    assert submitted.command.strategy_instance_id is None
    assert submitted.leg.order_ref == trade.submit_calls[0]
    assert submitted.leg.order_ref.startswith("manual/operator/v1:")
    assert submitted.leg.state == "IN_PROGRESS"
    assert submitted.ticket.subject_id == manual_operator_subject_id(OPERATOR_ID)
    assert repo.strategy_instance(manual_operator_subject_id(OPERATOR_ID)) is None
    effect = repo.effect_operation(submitted.leg.effect_operation_id or "")
    assert effect is not None
    assert effect.strategy_instance_id is None
    assert effect.kind == "MANUAL_ORDER"


@pytest.mark.asyncio
async def test_manual_acceptance_refuses_a_preview_bound_to_an_old_control_revision(
    repo: ClerkSqliteRepository,
) -> None:
    observed = repo.control_meta_snapshot()
    trade = FakeTrade(repo=repo)
    repo.register_strategy_instance(
        strategy_instance_id="competing-manual-transition",
        symbol="QQQ",
        config_hash="competing",
    )

    with pytest.raises(ManualTicketContinuationError, match="preview is stale"):
        await submit_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id=TICKET_ID,
            leg_id=LEG_ID,
            leg=market_buy(),
            trade=trade,
            expected_preview_revision=ManualPreviewRevision.from_meta(observed),
        )

    assert trade.submit_calls == []
    assert repo.get_command(f"cmd:manual:{TICKET_ID}:{LEG_ID}") is None


@pytest.mark.asyncio
async def test_replay_reuses_one_manual_order_and_changed_content_conflicts(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    first = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    retry = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )

    assert retry.created is False
    assert retry.leg.order_ref == first.leg.order_ref
    assert len(trade.submit_calls) == 1
    with pytest.raises(DurableConflictError, match="already exists with a different payload"):
        accept_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id=TICKET_ID,
            leg_id=LEG_ID,
            leg=market_buy(quantity=2),
        )
    assert len(trade.submit_calls) == 1


@pytest.mark.asyncio
async def test_ordered_manual_ticket_submits_only_one_leg_until_explicit_continue(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    second_leg_id = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"
    legs = (
        ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),
        ManualTicketLeg(leg_id=second_leg_id, instruction=market_buy(quantity=2)),
    )

    first = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=legs,
        trade=trade,
    )

    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert [(leg.leg_id, leg.sequence_index, leg.state) for leg in ticket.legs] == [
        (LEG_ID, 0, "IN_PROGRESS"),
        (second_leg_id, 1, "RESERVED"),
    ]
    assert trade.submit_calls == [first.leg.order_ref]

    second = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=second_leg_id,
        leg=market_buy(quantity=2),
        ticket_legs=legs,
        continuation=True,
        trade=trade,
    )

    assert second.created is True
    assert len(trade.submit_calls) == 2
    assert trade.submit_calls[1] == second.leg.order_ref


@pytest.mark.asyncio
async def test_unknown_first_ticket_leg_refuses_later_broker_contact(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo, unavailable=True)
    second_leg_id = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"
    legs = (
        ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),
        ManualTicketLeg(leg_id=second_leg_id, instruction=market_buy(quantity=2)),
    )
    await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=legs,
        trade=trade,
    )

    with pytest.raises(ManualTicketContinuationError, match="remains paused"):
        await submit_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id=TICKET_ID,
            leg_id=second_leg_id,
            leg=market_buy(quantity=2),
            ticket_legs=legs,
            continuation=True,
            trade=trade,
        )
    assert len(trade.submit_calls) == 1
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and ticket.legs[1].state == "RESERVED"


@pytest.mark.asyncio
async def test_pending_prior_cancellation_refuses_ticket_continuation_before_broker_contact(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    second_leg_id = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"
    legs = (
        ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),
        ManualTicketLeg(leg_id=second_leg_id, instruction=market_buy(quantity=2)),
    )
    first = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=legs,
        trade=trade,
    )
    assert first.leg.order_ref is not None
    accepted_cancel = accept_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=first.leg.order_ref,
        cancel_request_id="fbe501a8-ba5e-42f9-b3d8-41c6edaf8c32",
    )
    assert accepted_cancel.cancellation.state == "ACCEPTED"

    with pytest.raises(ManualTicketContinuationError, match="prior manual cancellation"):
        next_manual_ticket_leg(repo, ticket_id=TICKET_ID)
    with pytest.raises(ManualTicketContinuationError, match="prior manual cancellation"):
        await submit_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id=TICKET_ID,
            leg_id=second_leg_id,
            leg=market_buy(quantity=2),
            ticket_legs=legs,
            continuation=True,
            trade=trade,
        )

    assert len(trade.submit_calls) == 1


@pytest.mark.asyncio
async def test_ticket_cancel_closes_reserved_ticket_without_broker_contact(
    repo: ClerkSqliteRepository,
) -> None:
    subject_id = manual_operator_subject_id(OPERATOR_ID)
    repo.append_transition(
        TransitionInput(
            transition_kind="CUSTODY_SUBJECT_REGISTERED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="CUSTODY_SUBJECT_REGISTERED",
            facts_json=CustodySubjectRegisteredFacts(
                subject_id=subject_id,
                kind="MANUAL_OPERATOR",
                strategy_instance_id=None,
                operator_id=OPERATOR_ID,
            ).to_facts_json(),
        )
    )
    repo.append_transition(
        TransitionInput(
            transition_kind="MANUAL_TICKET_RESERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="accepted",
            clerk_observed_at_ms=repo.clock(),
            summary_code="MANUAL_TICKET_RESERVED",
            facts_json=ManualTicketReservedFacts(
                ticket_id=TICKET_ID,
                subject_id=subject_id,
                operator_id=OPERATOR_ID,
                instruction_hash="ticket-hash",
                legs=(
                    ManualTicketLegReservedFacts(LEG_ID, "first-hash"),
                    ManualTicketLegReservedFacts("5791929d-4a3f-4ffc-a15f-62c34cb6c873", "second-hash"),
                ),
            ).to_facts_json(),
        )
    )
    trade = FakeTrade(repo=repo)

    cancelled = await submit_manual_ticket_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        cancel_request_id="8f859881-16a7-4df6-a3d4-7889f3a4ef26",
        trade=trade,
    )

    ticket = repo.manual_order_ticket(TICKET_ID)
    assert cancelled == ()
    assert ticket is not None and ticket.state == "CANCELED"
    assert [leg.state for leg in ticket.legs] == ["CANCELED", "CANCELED"]
    assert trade.cancel_calls == []


@pytest.mark.asyncio
async def test_manual_limit_gtc_leg_is_durable_and_submitted_once(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    limit = BrokerOrderLeg(
        symbol="SPY",
        side="buy",
        quantity=2,
        order_type="limit",
        limit_price=500.25,
        time_in_force="gtc",
    )

    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=limit,
        trade=trade,
    )

    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.legs[0].instruction == limit.model_dump(mode="json")
    assert submitted.leg.state == "IN_PROGRESS"
    assert len(trade.submit_calls) == 1


@pytest.mark.asyncio
async def test_lost_manual_submit_response_remains_queryable_unknown(
    repo: ClerkSqliteRepository,
) -> None:
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=FakeTrade(repo=repo, unavailable=True),
    )

    assert submitted.command.state == "unknown"
    resumed = repo.manual_order_ticket(TICKET_ID)
    assert resumed is not None
    assert resumed.legs[0].order_ref == submitted.leg.order_ref
    assert resumed.state == "PAUSED_UNKNOWN"
    assert resumed.legs[0].state == "UNKNOWN"
    assert repo.uncertain_orders()[0].order_ref == submitted.leg.order_ref
    uncertainty = repo.active_uncertainties_for_admission(
        subject_id=manual_operator_subject_id(OPERATOR_ID),
    )
    assert uncertainty[0]["scope"] == "CUSTODY_SUBJECT"
    assert uncertainty[0]["strategy_instance_id"] is None


@pytest.mark.asyncio
async def test_manual_submit_unexpected_broker_error_is_durable_uncertain_then_reraised(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo, unexpected=True)

    with pytest.raises(RuntimeError, match="malformed broker response"):
        await submit_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id=TICKET_ID,
            leg_id=LEG_ID,
            leg=market_buy(),
            trade=trade,
        )

    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "PAUSED_UNKNOWN"
    assert ticket.legs[0].state == "UNKNOWN"
    assert trade.submit_calls == [ticket.legs[0].order_ref]


@pytest.mark.asyncio
async def test_manual_submit_mismatched_client_order_id_is_durable_uncertain(
    repo: ClerkSqliteRepository,
) -> None:
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=FakeTrade(repo=repo, mismatched_client_order_id=True),
    )

    assert submitted.command.state == "unknown"
    assert submitted.ticket.state == "PAUSED_UNKNOWN"
    assert submitted.leg.state == "UNKNOWN"


@pytest.mark.asyncio
async def test_manual_order_exact_coverage_tolerance_completes_the_ticket(
    repo: ClerkSqliteRepository,
) -> None:
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=FakeTrade(repo=repo),
    )
    assert submitted.leg.effect_operation_id is not None
    assert submitted.leg.order_ref is not None
    exact = ExecutionSliceFilledFacts(
        execution_id="manual-execution-complete",
        symbol="SPY",
        side="BUY",
        slice_qty=1 + FILL_QTY_EPSILON / 2,
        slice_price=500,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=1_700_000_000_200,
    )
    repo.append_transition(
        TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=exact.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=exact.to_facts_json(),
        )
    )
    fold_order_evidence(
        repo,
        effect_operation_id=submitted.leg.effect_operation_id,
        order=filled_order(submitted.leg.order_ref),
    )

    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "COMPLETED"
    assert ticket.legs[0].state == "SUCCEEDED"
    assert repo.effect_operation(submitted.leg.effect_operation_id).state == "succeeded"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_direct_coverage_supersession_completes_a_filled_manual_ticket(
    repo: ClerkSqliteRepository,
) -> None:
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=FakeTrade(repo=repo),
    )
    assert submitted.leg.effect_operation_id is not None
    assert submitted.leg.order_ref is not None
    fold_order_evidence(
        repo,
        effect_operation_id=submitted.leg.effect_operation_id,
        order=filled_order(submitted.leg.order_ref),
    )
    exact = ExecutionSliceFilledFacts(
        execution_id="manual-coverage-resolution-execution",
        symbol="SPY",
        side="BUY",
        slice_qty=1,
        slice_price=500,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=1_700_000_000_400,
    )

    def exact_transition() -> TransitionInput:
        return TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=exact.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=exact.to_facts_json(),
        )

    def coverage_conflict() -> TransitionInput:
        conflict = UncertaintyRaisedFacts(
            severity="error",
            blocks_new_exposure=True,
            allows_reduction=False,
            reason_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
            headline="Exact execution overlaps aggregate recovery evidence",
            explanation="The evidence must be reconciled before new exposure can resume.",
            operator_impact="New exposure is blocked until coverage is reconciled.",
            next_step="Resolve the exact execution coverage.",
            evidence_refs=[exact.execution_id],
            cause_facts=ExecutionCoverageConflictCause(
                order_ref=submitted.leg.order_ref,
                execution_id=exact.execution_id,
            ).to_mapping(),
        )
        return TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="UNCERTAINTY_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
            facts_json=conflict.to_facts_json(),
        )

    assert (
        repo.append_execution_slice_if_absent(
            execution_id=exact.execution_id,
            order_ref=submitted.leg.order_ref,
            build_transition=exact_transition,
            build_coverage_conflict=coverage_conflict,
        )
        == "coverage_superseded"
    )
    uncertainty = repo.active_uncertainties_for_admission(
        subject_id=manual_operator_subject_id(OPERATOR_ID),
    )
    assert uncertainty == []
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "COMPLETED"
    assert ticket.legs[0].state == "SUCCEEDED"
    assert repo.effect_operation(submitted.leg.effect_operation_id).state == "succeeded"  # type: ignore[union-attr]
    restored = decide_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        subject_id=manual_operator_subject_id(OPERATOR_ID),
    )
    assert restored.allowed is True


@pytest.mark.asyncio
async def test_exact_execution_changes_only_the_manual_subject_position(
    repo: ClerkSqliteRepository,
) -> None:
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=FakeTrade(repo=repo),
    )
    assert submitted.leg.effect_operation_id is not None
    assert submitted.leg.order_ref is not None
    facts = ExecutionSliceFilledFacts(
        execution_id="manual-execution-1",
        symbol="SPY",
        side="BUY",
        slice_qty=1,
        slice_price=500,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=1_700_000_000_200,
    )
    repo.append_transition(
        TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        )
    )

    assert repo.attributed_positions_for_subject(manual_operator_subject_id(OPERATOR_ID)) == {"SPY": 1}
    assert repo.attributed_positions_for_strategy("bot-1") == {}
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        history = reader.account_executions(origin="manual", state="effective")
        attribution = reader.account_pnl_attribution(
            from_ms=0,
            to_ms=facts.source_event_at_ms,
            marks={"SPY": MarketMark(price=510, observed_at_ms=facts.source_event_at_ms)},
        )
    finally:
        reader.close()
    assert len(history.executions) == 1
    execution = history.executions[0]
    assert execution.origin == "manual"
    assert execution.strategy_instance_id is None
    assert execution.subject_id == manual_operator_subject_id(OPERATOR_ID)
    assert attribution.open_pnl_total == pytest.approx(10, abs=1e-6, rel=0)
    assert attribution.execution_coverage == "complete"


@pytest.mark.asyncio
async def test_manual_execution_correction_replaces_only_manual_custody(
    repo: ClerkSqliteRepository,
) -> None:
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=FakeTrade(repo=repo),
    )
    assert submitted.leg.effect_operation_id is not None
    assert submitted.leg.order_ref is not None
    exact = ExecutionSliceFilledFacts(
        execution_id="manual-execution-to-correct",
        symbol="SPY",
        side="BUY",
        slice_qty=1,
        slice_price=500,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=1_700_000_000_200,
    )
    repo.append_transition(
        TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=exact.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=exact.to_facts_json(),
        )
    )
    corrected = ExecutionCorrectedFacts(
        execution_id="manual-corrected-execution",
        superseded_execution_ref=exact.execution_id,
        symbol="SPY",
        side="BUY",
        corrected_qty=2,
        corrected_price=501,
        why="broker corrected the exact manual fill",
    )

    outcome = repo.append_execution_correction_or_raise(
        correction=TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="EXECUTION_CORRECTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=1_700_000_000_201,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_CORRECTED",
            facts_json=corrected.to_facts_json(),
        ),
        build_uncertainty=lambda reason: pytest.fail(f"unexpected correction rejection: {reason}"),
    )

    assert outcome == "appended"
    assert repo.attributed_positions_for_subject(manual_operator_subject_id(OPERATOR_ID)) == {"SPY": 2.0}
    assert repo.attributed_positions_for_strategy("bot-1") == {}


@pytest.mark.asyncio
async def test_manual_sell_reserves_only_its_subject_long_position(repo: ClerkSqliteRepository) -> None:
    trade = FakeTrade(repo=repo)
    bought = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(quantity=2),
        trade=trade,
    )
    assert bought.leg.effect_operation_id is not None and bought.leg.order_ref is not None
    exact = ExecutionSliceFilledFacts(
        execution_id="manual-owned-long",
        symbol="SPY",
        side="BUY",
        slice_qty=2,
        slice_price=500,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=1_700_000_000_200,
    )
    repo.append_transition(
        TransitionInput(
            command_id=bought.command.command_id,
            effect_operation_id=bought.leg.effect_operation_id,
            order_ref=bought.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=exact.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=exact.to_facts_json(),
        )
    )

    race_candidates = (
        ("b5d667d3-6820-4c60-927a-2130f3c02aaf", "91cd2b42-12b1-4c04-9caa-ffdfc346fbc2"),
        ("f1d052f8-e2a2-4d16-a9a2-96b6c6c9d8a1", "cf0342f0-541c-4e71-9ab0-1951127e45c6"),
    )
    outcomes = await asyncio.gather(
        *(
            asyncio.to_thread(
                accept_manual_order,
                repo,
                account_id=ACCOUNT_ID,
                operator_id=OPERATOR_ID,
                ticket_id=ticket_id,
                leg_id=leg_id,
                leg=market_sell(quantity=1.5),
            )
            for ticket_id, leg_id in race_candidates
        ),
        return_exceptions=True,
    )
    reduced = next(outcome for outcome in outcomes if not isinstance(outcome, Exception))
    assert len([outcome for outcome in outcomes if isinstance(outcome, AdmissionBlockedError)]) == 1

    assert reduced.created is True
    assert repo.manual_reduction_available_quantity(
        subject_id=manual_operator_subject_id(OPERATOR_ID), symbol="SPY"
    ) == pytest.approx(0.5, abs=1e-9, rel=0)
    with pytest.raises(AdmissionBlockedError, match="MANUAL_LONG_QUANTITY_UNAVAILABLE"):
        accept_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id="28e9f1ee-b606-4be7-84c8-9c9173d16e52",
            leg_id="a43be8b2-41ed-4ce7-8dc2-842229eb4ebc",
            leg=market_sell(quantity=1),
        )

    with pytest.raises(AdmissionBlockedError, match="MANUAL_LONG_QUANTITY_UNAVAILABLE"):
        accept_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id="other-operator",
            ticket_id="4b1be7d3-9345-4775-8bdc-a0792cddc03f",
            leg_id="0ba22f8d-8f4e-4f28-a15f-97d41985271f",
            leg=market_sell(quantity=1),
        )

    # The durable history may contain replayed/recovered acceptance evidence
    # for one effect.  It must remain one reservation, rather than charging
    # the same pending sell twice against its manual long position.
    accepted_transition = repo._conn.execute(
        "SELECT * FROM custody_transitions WHERE effect_operation_id = ? AND transition_kind = 'MANUAL_ORDER_ACCEPTED'",
        (reduced.leg.effect_operation_id,),
    ).fetchone()
    assert accepted_transition is not None
    duplicate = dict(accepted_transition)
    duplicate["prev_hash"] = "test-duplicated-acceptance-prev-hash"
    duplicate["row_hash"] = "test-duplicated-acceptance-row-hash"
    columns = tuple(column for column in duplicate if column != "sequence")
    repo._conn.execute(
        f"INSERT INTO custody_transitions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        tuple(duplicate[column] for column in columns),
    )
    repo._conn.commit()

    assert repo.manual_reduction_available_quantity(
        subject_id=manual_operator_subject_id(OPERATOR_ID), symbol="SPY"
    ) == pytest.approx(0.5, abs=1e-9, rel=0)


@pytest.mark.asyncio
async def test_manual_sell_reserves_only_its_unfilled_remainder(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    bought = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(quantity=2),
        trade=trade,
    )
    assert bought.leg.effect_operation_id is not None and bought.leg.order_ref is not None
    repo.append_transition(
        TransitionInput(
            command_id=bought.command.command_id,
            effect_operation_id=bought.leg.effect_operation_id,
            order_ref=bought.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=1_700_000_000_200,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=ExecutionSliceFilledFacts(
                execution_id="manual-owned-long-for-partial-sell",
                symbol="SPY",
                side="BUY",
                slice_qty=2,
                slice_price=500,
                fee=None,
                fee_fidelity="not_reported",
                evidence_source="websocket",
                source_event_at_ms=1_700_000_000_200,
            ).to_facts_json(),
        )
    )
    reduced = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id="7621bfd6-7211-4399-8cc1-01a5a34565eb",
        leg_id="f7f1993d-0562-4d8c-9fcf-091fedc515fa",
        leg=market_sell(quantity=1.5),
    )
    assert reduced.leg.effect_operation_id is not None and reduced.leg.order_ref is not None
    repo.append_transition(
        TransitionInput(
            command_id=reduced.command.command_id,
            effect_operation_id=reduced.leg.effect_operation_id,
            order_ref=reduced.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=1_700_000_000_300,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=ExecutionSliceFilledFacts(
                execution_id="manual-partial-sell",
                symbol="SPY",
                side="SELL",
                slice_qty=0.5,
                slice_price=501,
                fee=None,
                fee_fidelity="not_reported",
                evidence_source="websocket",
                source_event_at_ms=1_700_000_000_300,
            ).to_facts_json(),
        )
    )

    assert repo.manual_reduction_available_quantity(
        subject_id=manual_operator_subject_id(OPERATOR_ID), symbol="SPY"
    ) == pytest.approx(0.5, abs=1e-9, rel=0)


@pytest.mark.asyncio
async def test_manual_cancel_is_durable_idempotent_and_proves_exact_target(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None

    cancelled = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="d38ed6a0-fc4f-4dfb-9091-eafcc61549b4",
        trade=trade,
    )
    replay = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="d38ed6a0-fc4f-4dfb-9091-eafcc61549b4",
        trade=trade,
    )

    assert cancelled.cancellation.state == "SUCCEEDED"
    assert cancelled.command.state == "succeeded"
    assert replay.created is False
    assert len(trade.cancel_calls) == 1
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and ticket.legs[0].state == "CANCELED"
    assert ticket.state == "CANCELED"


@pytest.mark.asyncio
async def test_manual_cancel_marks_unactivated_ticket_legs_canceled_without_broker_contact(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    second_leg_id = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"
    legs = (
        ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),
        ManualTicketLeg(leg_id=second_leg_id, instruction=market_buy(quantity=2)),
    )
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=legs,
        trade=trade,
    )
    assert submitted.leg.order_ref is not None

    await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="d38e6b15-9f4f-47a4-88fd-2e5c91031eb0",
        trade=trade,
    )

    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert [leg.state for leg in ticket.legs] == ["CANCELED", "CANCELED"]
    assert ticket.legs[1].order_ref is None
    assert len(trade.submit_calls) == 1


@pytest.mark.asyncio
async def test_ticket_cancel_cancels_each_verified_working_leg_with_stable_child_requests(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    second_leg_id = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"
    legs = (
        ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),
        ManualTicketLeg(leg_id=second_leg_id, instruction=market_buy(quantity=2)),
    )
    first = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=legs,
        trade=trade,
    )
    second = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=second_leg_id,
        leg=market_buy(quantity=2),
        ticket_legs=legs,
        continuation=True,
        trade=trade,
    )
    assert first.leg.order_ref is not None and second.leg.order_ref is not None

    cancelled = await submit_manual_ticket_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        cancel_request_id="d38e6b15-9f4f-47a4-88fd-2e5c91031eb0",
        trade=trade,
    )
    replay = await submit_manual_ticket_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        cancel_request_id="d38e6b15-9f4f-47a4-88fd-2e5c91031eb0",
        trade=trade,
    )

    assert [item.cancellation.state for item in cancelled] == ["SUCCEEDED", "SUCCEEDED"]
    assert [item.cancellation.cancel_request_id for item in replay] == [
        item.cancellation.cancel_request_id for item in cancelled
    ]
    assert len(trade.cancel_calls) == 2
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "CANCELED"
    assert [leg.state for leg in ticket.legs] == ["CANCELED", "CANCELED"]


@pytest.mark.asyncio
async def test_ticket_cancel_stops_before_later_broker_writes_after_an_unknown_outcome(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo, cancel_unavailable=True)
    second_leg_id = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"
    legs = (
        ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),
        ManualTicketLeg(leg_id=second_leg_id, instruction=market_buy(quantity=2)),
    )
    await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=legs,
        trade=trade,
    )
    await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=second_leg_id,
        leg=market_buy(quantity=2),
        ticket_legs=legs,
        continuation=True,
        trade=trade,
    )

    cancelled = await submit_manual_ticket_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        cancel_request_id="d38e6b15-9f4f-47a4-88fd-2e5c91031eb0",
        trade=trade,
    )

    assert [item.cancellation.state for item in cancelled] == ["UNKNOWN"]
    assert len(trade.cancel_calls) == 1
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "PAUSED_UNKNOWN"
    assert [leg.state for leg in ticket.legs] == ["IN_PROGRESS", "IN_PROGRESS"]


@pytest.mark.asyncio
async def test_manual_cancel_never_activated_leg_requires_exact_broker_absence_evidence(
    repo: ClerkSqliteRepository,
) -> None:
    accepted = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
    )
    assert accepted.leg.order_ref is not None
    trade = FakeTrade(repo=repo)

    cancelled = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=accepted.leg.order_ref,
        cancel_request_id="aa3e591e-8d40-4207-8a1f-82bfb15619c1",
        trade=trade,
    )

    assert cancelled.cancellation.state == "UNKNOWN"
    assert cancelled.effect.state == "unknown"
    assert trade.cancel_calls == []
    with pytest.raises(ManualOrderCancelOwnershipError):
        await submit_manual_order_cancellation(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            order_ref="manual/operator/v1:foreign",
            cancel_request_id="7728050b-e571-465b-9bde-e36b6f9687c9",
            trade=trade,
        )


@pytest.mark.asyncio
async def test_manual_cancel_resolves_a_target_that_never_reached_the_broker(
    tmp_path: Path,
) -> None:
    """Sibling of the EXIT cancel-prove absence branch (#1775, finding S15c).

    A manual leg whose submit response was lost and which the broker never had
    cannot be cancelled and can never become working. Once absence is proven
    past the R4 grace window, the cancellation reaches a terminal outcome
    instead of folding cancel-uncertain on every recovery pass.

    ``lease_ttl_ms`` is bumped past the 30 s grace window so the clock jump
    exercises only the grace-window math, not an incidental lease expiry.
    """
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
        lease_ttl_ms=300_000,
    )
    trade = FakeTrade(repo=repo, unavailable=True)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None
    cancellation = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="6f0c3c0f-1f2a-4a1a-9a3a-0f27a1b1b9c2",
        trade=trade,
    )
    assert cancellation.effect.state == "unknown"  # absence not yet provable

    clock.advance(30_001)  # past the R4 submit-absence grace window
    await resolve_manual_order_cancellation(
        repo,
        effect_operation_id=cancellation.effect.effect_operation_id,
        trade=trade,
    )

    effect = repo.effect_operation(cancellation.effect.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    source = repo.effect_operation(submitted.leg.effect_operation_id)
    assert source is not None and source.state == "failed"
    assert repo.uncertain_orders() == []
    repo.close()


@pytest.mark.asyncio
async def test_manual_cancel_records_a_broker_rejection_when_exact_evidence_stays_working(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo, cancel_error=BrokerError("broker refused cancellation"))
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None

    cancellation = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="10a32cd9-5bb9-4c2f-a13b-4b18fb612a36",
        trade=trade,
    )

    assert cancellation.cancellation.state == "UNKNOWN"
    assert cancellation.effect.state == "unknown"
    latest = repo.transitions_for_order(submitted.leg.order_ref)[-1]
    assert latest["transition_kind"] == "ORDER_CANCEL_UNCERTAIN"
    assert "broker refused cancellation" in latest["facts_json"]


@pytest.mark.asyncio
async def test_manual_cancel_refuses_a_terminal_target_before_creating_a_cancel_effect(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None and submitted.leg.effect_operation_id is not None
    repo.append_transition(
        TransitionInput(
            command_id=submitted.command.command_id,
            effect_operation_id=submitted.leg.effect_operation_id,
            order_ref=submitted.leg.order_ref,
            transition_kind="MANUAL_ORDER_CANCELED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="failed",
            clerk_observed_at_ms=repo.clock(),
            summary_code="MANUAL_ORDER_CANCELED",
            facts_json=ManualOrderCancelResultFacts(
                outcome="CANCELED",
                why="Exact broker evidence already made this manual order terminal.",
            ).to_facts_json(),
        )
    )

    with pytest.raises(ManualOrderCancelTerminalError, match="already terminal"):
        await submit_manual_order_cancellation(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            order_ref=submitted.leg.order_ref,
            cancel_request_id="d40f1aeb-263c-4a57-8583-0e48f1d9298b",
            trade=trade,
        )

    assert repo.manual_order_cancellation(order_ref=submitted.leg.order_ref) is None
    assert trade.cancel_calls == []


@pytest.mark.asyncio
async def test_manual_cancel_recovers_an_already_canceled_target_as_success(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None
    order = trade.orders[submitted.leg.order_ref]
    trade.orders[submitted.leg.order_ref] = order.model_copy(
        update={"status": "canceled", "canceled_at_ms": 1_700_000_000_300}
    )

    cancellation = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="a43145a2-afd2-4d2b-a2fc-09a6b8b24a79",
        trade=trade,
    )

    assert cancellation.cancellation.state == "SUCCEEDED"
    assert trade.cancel_calls == []
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and ticket.state == "CANCELED"


@pytest.mark.asyncio
async def test_manual_cancel_closes_expired_target_and_does_not_issue_delete(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None and submitted.leg.effect_operation_id is not None
    trade.orders[submitted.leg.order_ref] = trade.orders[submitted.leg.order_ref].model_copy(
        update={"status": "expired", "expired_at_ms": 1_700_000_000_300}
    )

    cancellation = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="2f7575e4-4ffb-4fca-a7a0-576d72d1391b",
        trade=trade,
    )

    assert cancellation.cancellation.state == "FAILED"
    assert repo.effect_operation(submitted.leg.effect_operation_id).state == "failed"
    assert trade.cancel_calls == []
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None and ticket.state == "COMPLETED"
    assert ticket.legs[0].state == "FAILED"


@pytest.mark.asyncio
async def test_manual_cancel_polls_a_pending_cancel_without_another_delete(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None
    trade.orders[submitted.leg.order_ref] = trade.orders[submitted.leg.order_ref].model_copy(
        update={"status": "pending_cancel"}
    )

    accepted = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="50e68b27-4d3d-4bb0-a802-7c5c239dbdbf",
        trade=trade,
    )
    await resolve_manual_order_cancellation(
        repo,
        effect_operation_id=accepted.effect.effect_operation_id,
        trade=trade,
    )

    assert accepted.cancellation.state == "ACCEPTED"
    assert trade.cancel_calls == []


@pytest.mark.asyncio
async def test_unknown_manual_cancel_recovers_by_its_exact_clerk_order_identity(
    repo: ClerkSqliteRepository,
) -> None:
    trade = FakeTrade(repo=repo, cancel_unavailable=True)
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        trade=trade,
    )
    assert submitted.leg.order_ref is not None

    unknown = await submit_manual_order_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        order_ref=submitted.leg.order_ref,
        cancel_request_id="17b88970-a97d-46a4-b3e8-655d44e74b4c",
        trade=trade,
    )

    assert unknown.cancellation.state == "UNKNOWN"
    assert unknown.effect.state == "unknown"
    trade.cancel_unavailable = False
    recovered = await resolve_manual_order_cancellation(
        repo,
        effect_operation_id=unknown.effect.effect_operation_id,
        trade=trade,
    )

    assert recovered.cancellation.state == "SUCCEEDED"
    assert len(trade.cancel_calls) == 2
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "CANCELED"
    assert ticket.legs[0].state == "CANCELED"


@pytest.mark.asyncio
async def test_ticket_cancel_does_not_reclassify_a_completed_ticket_as_canceled(
    repo: ClerkSqliteRepository,
) -> None:
    ticket_legs = (ManualTicketLeg(leg_id=LEG_ID, instruction=market_buy()),)
    accepted = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        leg_id=LEG_ID,
        leg=market_buy(),
        ticket_legs=ticket_legs,
    )
    assert accepted.leg.effect_operation_id is not None
    repo._conn.execute(
        "UPDATE effect_operations SET state = 'succeeded' WHERE effect_operation_id = ?",
        (accepted.leg.effect_operation_id,),
    )
    repo._conn.execute(
        "UPDATE manual_order_legs SET state = 'SUCCEEDED' WHERE ticket_id = ? AND leg_id = ?",
        (TICKET_ID, LEG_ID),
    )
    repo._conn.execute(
        "UPDATE manual_order_tickets SET state = 'COMPLETED' WHERE ticket_id = ?",
        (TICKET_ID,),
    )
    repo._conn.commit()

    result = await submit_manual_ticket_cancellation(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id=TICKET_ID,
        cancel_request_id="1edfbf29-e36c-4c35-8e4e-19797c433fa3",
        trade=FakeTrade(repo=repo),
    )

    assert result == ()
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "COMPLETED"
    assert ticket.legs[0].state == "SUCCEEDED"
