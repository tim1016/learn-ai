"""The account-wide day-P&L fact the loss hold judges (ADR 0059 D4).

The window is the ET calendar day containing ``now_ms``, so a lot opened on a
prior session and closed today counts once, today; the entry's own cash
outflow never reads as a loss. Fees net out only when the broker reported
every one of them, and an external order observed today makes the whole fact
*unknown* rather than optimistically zero.

Every stamp here is an explicit ``int64 ms UTC`` built from
``et_minute_of_day_ms``; the repository clock is fixed at ``NOON``. Nothing
reads the wall clock.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import AccountObservation
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_at
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _TestClock

ACCOUNT_ID = "PA-DAY-PNL"
SID = "day-pnl-bot"
RUN_ID = "run-day-pnl"
SYMBOL = "SPY"

# 2026-09-08 is a Tuesday; the 7th is Labor Day, so "yesterday" is Friday the 4th.
NOON = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
TODAY_OPEN = et_minute_of_day_ms(date(2026, 9, 8), 9 * 60 + 30)
YESTERDAY_NOON = et_minute_of_day_ms(date(2026, 9, 4), 12 * 60)
YESTERDAY_OPEN = et_minute_of_day_ms(date(2026, 9, 4), 9 * 60 + 30)


def _observation(*, unrealized: float) -> AccountObservation:
    return AccountObservation(
        observed_at_ms=NOON,
        broker_cash_usd=100_000.0,
        cash_available_usd=100_000.0,
        last_equity_usd=100_000.0,
        unrealized_pl_usd=unrealized,
        position_count=1,
    )


@pytest.fixture
def clock() -> _TestClock:
    return _clock_at(NOON)


@pytest.fixture
def repo(tmp_path: Path, clock: _TestClock) -> Iterator[ClerkSqliteRepository]:
    """One authority with a registered, running instance — every stamp from ``clock``."""
    clerk = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    clerk.register_strategy_instance(
        strategy_instance_id=SID, symbol=SYMBOL, config_hash="day-pnl-config"
    )
    submit_start_run(
        clerk,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID,
        clock=clock,
    )
    yield clerk
    clerk.close()


@pytest.fixture
def reader(repo: ClerkSqliteRepository) -> Iterator[SqliteEconomicProjectionReader]:
    """One read-only projection over ``repo``, closed with the test.

    Constructed after the seeding fixtures have committed; it holds its own
    ``mode=ro`` connection, so it must be closed like the sibling economic
    suite closes its readers.
    """
    projection = SqliteEconomicProjectionReader.from_repository(repo)
    yield projection
    projection.close()


def _accept(repo: ClerkSqliteRepository, *, decision_id: str) -> EnterSubmission:
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol=SYMBOL, side="buy", quantity=10),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    return accepted


def _append_slice(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    side: str,
    quantity: float,
    price: float,
    occurred_at_ms: int,
    fee: float | None = None,
    fee_fidelity: str = "not_reported",
) -> None:
    """Fold one websocket execution slice at an explicit economic time.

    ``source_event_at_ms`` is the fill's economic time — what FIFO orders by
    and what the day window filters on — so it, not the fixed repository
    clock, is what puts a fill on Friday or on Tuesday.
    """
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol=SYMBOL,
        side=side,
        slice_qty=quantity,
        slice_price=price,
        fee=fee,
        fee_fidelity=fee_fidelity,
        evidence_source="websocket",
        source_event_at_ms=occurred_at_ms,
    )
    result = repo.append_execution_slice_if_absent(
        execution_id=execution_id,
        order_ref=accepted.order_ref or "",
        build_transition=lambda: TransitionInput(
            strategy_instance_id=SID,
            run_id=accepted.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        ),
        build_coverage_conflict=lambda: (_ for _ in ()).throw(
            AssertionError("an exact first execution has nothing to conflict with")
        ),
    )
    assert result == "appended"


def _observe_foreign_order(repo: ClerkSqliteRepository, *, observed_at_ms: int) -> None:
    """Record one order the Clerk did not place, observed at an explicit instant."""
    observe_external_order(
        repo,
        order=BrokerOrder(
            broker="alpaca",
            order_id="external-order-1",
            client_order_id="alpaca-console:external-1",
            symbol="MSFT",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=3.0,
            filled_quantity=3.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=50.0,
            status="filled",
            submitted_at_ms=observed_at_ms,
            created_at_ms=observed_at_ms,
            updated_at_ms=observed_at_ms,
            filled_at_ms=observed_at_ms,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=observed_at_ms,
        ),
    )


@pytest.fixture
def seeded_round_trip(repo: ClerkSqliteRepository) -> None:
    """BUY 10 @ 100 on Friday, SELL 10 @ 110 today at noon with a $0.05 fee."""
    accepted = _accept(repo, decision_id="d-round-trip")
    _append_slice(
        repo,
        accepted,
        execution_id="exec-buy-friday",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=YESTERDAY_NOON,
        fee=0.0,
        fee_fidelity="reported",
    )
    _append_slice(
        repo,
        accepted,
        execution_id="exec-sell-today",
        side="SELL",
        quantity=10.0,
        price=110.0,
        occurred_at_ms=NOON,
        fee=0.05,
        fee_fidelity="reported",
    )


@pytest.fixture
def seeded_round_trip_without_fees(repo: ClerkSqliteRepository) -> None:
    """The same round trip, with no commission data on the closing fill."""
    accepted = _accept(repo, decision_id="d-round-trip-no-fees")
    _append_slice(
        repo,
        accepted,
        execution_id="exec-buy-friday",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=YESTERDAY_NOON,
    )
    _append_slice(
        repo,
        accepted,
        execution_id="exec-sell-today",
        side="SELL",
        quantity=10.0,
        price=110.0,
        occurred_at_ms=NOON,
    )


@pytest.fixture
def seeded_round_trip_closed_yesterday(repo: ClerkSqliteRepository) -> None:
    """A whole round trip that opened and closed on Friday — none of it is today's."""
    accepted = _accept(repo, decision_id="d-closed-yesterday")
    _append_slice(
        repo,
        accepted,
        execution_id="exec-buy-friday-open",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=YESTERDAY_OPEN,
        fee=0.0,
        fee_fidelity="reported",
    )
    _append_slice(
        repo,
        accepted,
        execution_id="exec-sell-friday-noon",
        side="SELL",
        quantity=10.0,
        price=110.0,
        occurred_at_ms=YESTERDAY_NOON,
        fee=0.0,
        fee_fidelity="reported",
    )


@pytest.fixture
def seeded_open_buy(repo: ClerkSqliteRepository) -> None:
    """BUY 10 @ 100 at today's open, still held — nothing realized yet."""
    accepted = _accept(repo, decision_id="d-open-buy")
    _append_slice(
        repo,
        accepted,
        execution_id="exec-buy-today",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=TODAY_OPEN,
        fee=0.0,
        fee_fidelity="reported",
    )


@pytest.fixture
def seeded_external_order_today(repo: ClerkSqliteRepository) -> None:
    _observe_foreign_order(repo, observed_at_ms=TODAY_OPEN)


@pytest.fixture
def seeded_external_order_yesterday(repo: ClerkSqliteRepository) -> None:
    _observe_foreign_order(repo, observed_at_ms=YESTERDAY_NOON)


def test_realized_counts_only_lots_closed_today_and_nets_reported_fees(
    repo: ClerkSqliteRepository,
    seeded_round_trip: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=25.0), now_ms=NOON)
    assert (pnl.day_start_ms, pnl.day_end_ms) == et_day_window_ms(NOON)
    assert pnl.realized_usd == pytest.approx(100.0)
    assert pnl.fee_usd == pytest.approx(0.05) and pnl.fee_fidelity == "reported"
    assert pnl.unrealized_usd == 25.0
    assert pnl.total_usd == pytest.approx(124.95)
    assert pnl.known
    assert pnl.execution_coverage == "complete"


def test_a_lot_closed_yesterday_is_not_todays_realized(
    repo: ClerkSqliteRepository,
    seeded_round_trip_closed_yesterday: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    """The window is what makes this a *day* P&L, not a lifetime one."""
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.day_start_ms > YESTERDAY_NOON
    assert pnl.realized_usd == pytest.approx(0.0)
    assert pnl.total_usd == pytest.approx(0.0)


def test_an_open_position_contributes_only_the_brokers_unrealized(
    repo: ClerkSqliteRepository,
    seeded_open_buy: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    """No lot has closed, so the day's whole P&L is the broker's own figure."""
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=-40.0), now_ms=NOON)
    assert pnl.realized_usd == pytest.approx(0.0)
    assert pnl.total_usd == pytest.approx(-40.0)


def test_an_external_order_seen_today_makes_the_fact_unknown(
    repo: ClerkSqliteRepository,
    seeded_external_order_today: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.external_orders_today == 1 and not pnl.known
    # The fixture's foreign order is filled (status="filled", filled_avg_price=50.0,
    # quantity=3.0), so the projection's own external_fill_exists check trips and its
    # verdict is "incomplete" — independent of, and carried alongside, `known`.
    assert pnl.execution_coverage == "incomplete"


def test_an_external_order_seen_yesterday_does_not(
    repo: ClerkSqliteRepository,
    seeded_external_order_yesterday: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    assert day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON).known


def test_net_cash_spent_is_buys_less_sells_over_every_subject(
    seeded_round_trip: None, reader: SqliteEconomicProjectionReader
) -> None:
    assert reader.account_net_cash_spent_usd() == pytest.approx(1_000.0 - 1_100.0)


def test_unreported_fees_net_nothing_and_say_so(
    repo: ClerkSqliteRepository,
    seeded_round_trip_without_fees: None,
    reader: SqliteEconomicProjectionReader,
) -> None:
    pnl = day_pnl_at(reader, repo, observation=_observation(unrealized=0.0), now_ms=NOON)
    assert pnl.fee_usd == 0.0 and pnl.fee_fidelity == "not_reported"
