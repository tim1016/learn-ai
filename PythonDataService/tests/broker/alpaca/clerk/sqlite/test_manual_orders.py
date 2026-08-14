"""Manual market-tracer custody tests: one operator subject, never a pseudo-bot."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
    ManualOrderCancelResultFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.manual_order_cancellation import (
    ManualOrderCancelOwnershipError,
    ManualOrderCancelTerminalError,
    resolve_manual_order_cancellation,
    submit_manual_order_cancellation,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import (
    accept_manual_order,
    submit_manual_order,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from conftest import _clock_at

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
    ) -> None:
        self.repo = repo
        self.unavailable = unavailable
        self.cancel_unavailable = cancel_unavailable
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
            ticket
            and ticket.legs[0].order_ref == client_order_id
            and ticket.legs[0].effect_operation_id is not None
        )
        if self.unavailable:
            raise BrokerUnavailable("response lost", broker="alpaca")
        if self.unexpected:
            raise RuntimeError("malformed broker response")
        order = BrokerOrder(
            broker="alpaca",
            order_id="broker-order-1",
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
async def test_coverage_resolution_completes_a_filled_manual_ticket(
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

    assert repo.append_execution_slice_if_absent(
        execution_id=exact.execution_id,
        order_ref=submitted.leg.order_ref,
        build_transition=exact_transition,
        build_coverage_conflict=coverage_conflict,
    ) == "coverage_conflict_raised"
    uncertainty = repo.active_uncertainties_for_admission(
        subject_id=manual_operator_subject_id(OPERATOR_ID),
    )
    assert len(uncertainty) == 1
    meta = repo.control_meta_snapshot()

    receipt = repo.resolve_execution_coverage_conflict(
        uncertainty_id=uncertainty[0]["uncertainty_id"],
        expected_authority_generation=meta.authority_generation,
        expected_db_identity_token=meta.db_identity_token,
        expected_control_revision=meta.control_revision,
    )

    assert receipt.applied is True
    ticket = repo.manual_order_ticket(TICKET_ID)
    assert ticket is not None
    assert ticket.state == "COMPLETED"
    assert ticket.legs[0].state == "SUCCEEDED"
    assert repo.effect_operation(submitted.leg.effect_operation_id).state == "succeeded"  # type: ignore[union-attr]


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

    reduced = accept_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id=OPERATOR_ID,
        ticket_id="b5d667d3-6820-4c60-927a-2130f3c02aaf",
        leg_id="91cd2b42-12b1-4c04-9caa-ffdfc346fbc2",
        leg=market_sell(quantity=1.5),
    )

    assert reduced.created is True
    assert repo.manual_reduction_available_quantity(
        subject_id=manual_operator_subject_id(OPERATOR_ID), symbol="SPY"
    ) == pytest.approx(0.5, abs=1e-9, rel=0)
    with pytest.raises(AdmissionBlockedError, match="MANUAL_LONG_QUANTITY_UNAVAILABLE"):
        accept_manual_order(
            repo,
            account_id=ACCOUNT_ID,
            operator_id=OPERATOR_ID,
            ticket_id="f1d052f8-e2a2-4d16-a9a2-96b6c6c9d8a1",
            leg_id="cf0342f0-541c-4e71-9ab0-1951127e45c6",
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
async def test_manual_cancel_never_activated_leg_stays_local_and_refuses_foreign(
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

    assert cancelled.cancellation.state == "SUCCEEDED"
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
