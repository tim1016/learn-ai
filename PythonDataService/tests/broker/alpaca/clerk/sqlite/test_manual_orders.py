"""Manual market-tracer custody tests: one operator subject, never a pseudo-bot."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.manual_orders import (
    accept_manual_order,
    submit_manual_order,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from conftest import _clock_at

ACCOUNT_ID = "PA-TEST"
OPERATOR_ID = "operator"
TICKET_ID = "7de3a77c-b698-4e0d-a5d1-2f624574ed35"
LEG_ID = "09d6d63e-6375-4e6d-8d20-3b1bf70c2465"


class FakeTrade:
    broker_id = "alpaca"

    def __init__(self, *, repo: ClerkSqliteRepository, unavailable: bool = False) -> None:
        self.repo = repo
        self.unavailable = unavailable
        self.submit_calls: list[str] = []
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
        return BrokerOrder(
            broker="alpaca",
            order_id="broker-order-1",
            client_order_id=client_order_id,
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

    async def cancel(self, order_id: str) -> None:  # pragma: no cover - S3
        raise NotImplementedError

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:  # pragma: no cover - S3
        return None


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
