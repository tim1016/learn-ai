"""Structures close from durable custody, and sold legs retain exact money (#2913)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.option_position_money import option_position_money
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.option_close import OptionClose
from app.broker.alpaca.clerk.sqlite.option_order_evidence import (
    multi_leg_order_evidence,
    with_the_simulations_own_leg_records,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_submission_response
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import ACCOUNT_ID, RUN_ID
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    STRADDLE,
    STRADDLE_PICKS,
    worked_entry,
)
from tests.broker.alpaca.clerk.sqlite.test_several_legs_admission import IRON_CONDOR, IRON_CONDOR_PICKS
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import _bind
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import world as _world

world = _world


async def opened(world, plan, picks, units=2):
    books, port = world
    entry = worked_entry(plan, picks, units=units, now_ms=NOON)
    accepted = accept_option_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="open", lifecycle_run_id=RUN_ID,
        entry=entry,
    )
    _bind(port, accepted.order_ref, entry)
    answer = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
    evidence = await with_the_simulations_own_leg_records(port, multi_leg_order_evidence(answer))
    fold_order_submission_response(books, effect_operation_id=accepted.effect_operation_id, order=evidence, trade=port)
    return accepted, entry


@pytest.mark.parametrize("plan,picks,basis,collateral", [
    (PAID_SPREAD, PAID_PICKS, "388", "0"),
    (RECEIVED_SPREAD, RECEIVED_PICKS, "386", "1000"),
    (STRADDLE, STRADDLE_PICKS, "1200", "0"),
    (IRON_CONDOR, IRON_CONDOR_PICKS, "716", "1000"),
])
async def test_a_structure_closes_once_in_one_market_order_and_counts_every_leg(world, plan, picks, basis, collateral):
    books, port = world
    accepted, entry = await opened(world, plan, picks)
    records = effective_fill_records(books._conn, account_id=ACCOUNT_ID)
    assert option_position_money(compute_fifo_pnl(records).open_lots) == (Decimal(basis), Decimal(collateral))
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close", entry_order_ref=accepted.order_ref, lifecycle_run_id=RUN_ID,
    )
    for _ in range(2):
        await resolve_exit(
            books, effect_operation_id=closing.effect_operation_id, trade=port, pricing=UNPRICEABLE_RECOVERY, read=None,
        )
    assert books.effect_operation(closing.effect_operation_id).state == "succeeded"
    assert not any(books.attributed_positions_for_strategy("bot-a").values())
    orders = books.orders_for_effect_operation(closing.effect_operation_id)
    parent = next(order for order in orders if order.role == "REDUCING")
    assert len(books.order_legs(parent.order_ref)) == len(entry.legs)
    assert books.fills_for_order(parent.order_ref) == []
    answer = await port.get_option_order_by_client_order_id(parent.order_ref)
    assert answer.status == "filled" and len(answer.legs) == len(entry.legs)
    records = effective_fill_records(books._conn, account_id=ACCOUNT_ID)
    fifo = compute_fifo_pnl(records)
    # Each leg loses its four-cent spread: 2 contracts × 100 × .04 per leg.
    assert fifo.exact_realized_pnl == Decimal(-8 * len(entry.legs))
    assert option_position_money(fifo.open_lots) == (Decimal(0), Decimal(0))


async def test_a_close_with_one_unpriced_leg_records_no_leg_fill(world):
    books, port = world
    accepted, entry = await opened(world, PAID_SPREAD, PAID_PICKS)
    # A newer one-sided quote cannot be replaced by the entry's old whole quote.
    from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide

    port._options._kept_quotes.keep_all({
        entry.legs[1].contract.occ_symbol: HeldQuote(
            bid=QuoteSide(Decimal("1.2"), NOON), ask=None, data_type=QuoteDataType.LIVE, generation=4,
        ),
    }, kept_at_ms=NOON)
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close", entry_order_ref=accepted.order_ref, lifecycle_run_id=RUN_ID,
    )
    await resolve_exit(books, effect_operation_id=closing.effect_operation_id, trade=port, pricing=UNPRICEABLE_RECOVERY, read=None)
    assert books.effect_operation(closing.effect_operation_id).state == "failed"
    assert len(effective_fill_records(books._conn, account_id=ACCOUNT_ID)) == 2


@pytest.mark.parametrize("kept_quote_usable", [True, False])
async def test_a_missing_recorded_quote_collection_uses_the_kept_quote_or_refuses(
    world: tuple[ClerkSqliteRepository, SyntheticBroker], monkeypatch: pytest.MonkeyPatch, kept_quote_usable: bool,
) -> None:
    from app.broker.alpaca.clerk.sqlite import exit_resolution
    from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide

    books, port = world
    accepted, entry = await opened(world, PAID_SPREAD, PAID_PICKS)
    read_close = exit_resolution.recorded_option_close
    if not kept_quote_usable:
        port._options._kept_quotes.keep_all({
            entry.legs[1].contract.occ_symbol: HeldQuote(
                bid=QuoteSide(Decimal("1.2"), NOON), ask=None, data_type=QuoteDataType.LIVE, generation=4,
            ),
        }, kept_at_ms=NOON)

    def without_quotes(repo: ClerkSqliteRepository, effect_id: str, facts: ExitReducingOrderCreatedFacts) -> OptionClose | None:
        close = read_close(repo, effect_id, facts)
        return None if close is None else replace(close, quotes={
            symbol: quotes for symbol, quotes in close.quotes.items() if symbol != entry.legs[1].contract.occ_symbol
        })

    monkeypatch.setattr(exit_resolution, "recorded_option_close", without_quotes)
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close",
        entry_order_ref=accepted.order_ref, lifecycle_run_id=RUN_ID,
    )
    await resolve_exit(books, effect_operation_id=closing.effect_operation_id, trade=port, pricing=UNPRICEABLE_RECOVERY, read=None)
    if kept_quote_usable:
        assert books.effect_operation(closing.effect_operation_id).state == "succeeded"
        assert not any(books.attributed_positions_for_strategy("bot-a").values())
        assert len(effective_fill_records(books._conn, account_id=ACCOUNT_ID)) == 4
        return
    assert books.effect_operation(closing.effect_operation_id).state == "failed"
    assert any("OPTION_EXIT_UNPRICED" in row["facts_json"] for row in books.custody_transitions())
    assert len(effective_fill_records(books._conn, account_id=ACCOUNT_ID)) == 2
    assert not any(
        row["transition_kind"] == "ORDER_SUBMIT_REQUESTED" and row["effect_operation_id"] == closing.effect_operation_id
        for row in books.custody_transitions()
    )


@pytest.mark.parametrize("plan,picks,cost,collateral", [
    (PAID_SPREAD, PAID_PICKS, "194", "0"),
    (RECEIVED_SPREAD, RECEIVED_PICKS, "193", "500"),
    (STRADDLE, STRADDLE_PICKS, "600", "0"),
])
async def test_the_registered_bot_can_enter_read_its_budget_and_close(tmp_path, plan, picks, cost, collateral):
    from app.broker.alpaca.clerk.account_money import money_bar
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
    from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
    from app.services import market_liveness
    from tests._helpers.market_quotes import publish_quotes
    from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock
    from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import evidence
    from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import RUN, SID, _binding, _open

    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    try:
        account = await _open(tmp_path, _TestClock(NOON), _binding(plan))
        await account.clerk.register_strategy_run(account.binding)
        receipt = await account.enter(evidence(NOON, picks), plan=plan)
        assert receipt.state is EffectOperationState.SUBMITTED, receipt.explanation
        from app.services.option_buying_power_views import option_buying_power_checks

        shown = option_buying_power_checks(account.repo, strategy_instance_id=SID, simulation=True, judged=False)
        [check] = shown.checks
        assert check.contracts_asked == len(picks)
        assert set(check.contract.split(", ")) == {pick[0].occ_symbol for pick in picks.values()}
        unseen = account.repo.account_money(cash=1000, seen_before_ms=NOON)
        money_bar(unseen)
        reading = account.account_reading()
        assert reading.unrealized_pl_usd == Decimal(-4 * len(picks))
        fees = account.fees().total_for(f"bot:{SID}")
        budget = account.repo.account_budget(cash=reading.cash_available_usd, seen_before_ms=NOON + 1)
        item = budget.deployments[0]
        assert (item.position_cost, item.option_collateral) == (Decimal(cost), Decimal(collateral))
        assert item.free == Decimal(1000) - Decimal(cost) - fees
        assert budget.available == 0
        money = account.repo.account_money(cash=reading.cash_available_usd, seen_before_ms=NOON + 1)
        money_bar(money)  # Exact conservation is enforced before drawing a segment.
        closed = await account.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id="close", purpose=EffectPurpose.EXIT,
            action_plan=plan, quantity=1,
        )
        assert closed.state is EffectOperationState.FLAT, closed.explanation
        assert account.held() == {}
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


async def test_the_deadline_keeper_closes_a_stopped_credit_spread_as_one_order(tmp_path):
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
    from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
    from app.broker.alpaca.clerk.sqlite.scheduled_close import CloseStanding, held_contracts, scheduled_close_state
    from app.services import market_liveness
    from tests._helpers.market_quotes import publish_quotes
    from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock
    from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import evidence
    from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import RUN, SID, _binding, _open

    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    try:
        account = await _open(tmp_path, _TestClock(NOON), _binding(RECEIVED_SPREAD))
        await account.clerk.register_strategy_run(account.binding)
        await account.enter(evidence(NOON, RECEIVED_PICKS), plan=RECEIVED_SPREAD)
        submit_stop_run(
            account.repo, account_id=account.repo.account_id, strategy_instance_id=SID, lifecycle_run_id=RUN,
            operator_reason="owner stopped", clock=account.clock,
        )
        held = held_contracts(account.repo, NOON).held
        assert len(held) == 2 and held[0].owed == held[1].owed
        due = held[0].owed.due_ms
        account.wait(due - NOON)
        runtime = get_clerk_runtime(account.binding.sealed_account_id)
        await runtime.deadline_keeper.tick()
        assert account.held() == {}
        states = scheduled_close_state(account.repo, SID)
        assert {state.contract for state in states} == {pick[0].occ_symbol for pick in RECEIVED_PICKS.values()}
        assert all(state.standing is CloseStanding.FLAT and state.held == 0 for state in states)
        assert all(state.contract in state.sentence for state in states)
        assert len({(state.due_ms, state.taken_at_ms) for state in states}) == 1
        assert account.repo._conn.execute("SELECT COUNT(*) FROM orders WHERE role='REDUCING'").fetchone()[0] == 1
        await runtime.deadline_keeper.tick()
        assert account.repo._conn.execute("SELECT COUNT(*) FROM orders WHERE role='REDUCING'").fetchone()[0] == 1
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


class Venue:
    """A documented broker answer double: submit acknowledges; exact lookup supplies cumulative executions."""

    broker_id = "alpaca"
    submission_response_is_authoritative_evidence = False

    def __init__(self, port, entry, *, lose_close_answer=False, close_counts=None):
        self.port, self.entry = port, entry
        self.lose_close_answer = lose_close_answer
        self.sent = []
        self.close_counts = close_counts
        self.close_answers = {}

    def capabilities(self):
        return self.port.capabilities()

    async def submit_option_order(self, order, *, client_order_id):
        from app.broker.alpaca.clerk.sqlite.option_close import order_reductions
        from app.broker.contract.errors import BrokerUnavailable

        self.sent.append(order)
        for symbol, side, _quantity in order_reductions(order):
            self.port.bind_option_close_quotes(client_order_id, symbol=symbol, side=side.value, quotes=(), usable_generation=None)
        answer = await self.port.submit_option_order(order, client_order_id=client_order_id)
        if self.close_counts is not None:
            answer = answer.model_copy(update={
                "status": "canceled", "filled_quantity": min(self.close_counts),
                "filled_avg_price": answer.filled_avg_price if min(self.close_counts) else None,
                "legs": tuple(leg.model_copy(update={
                    "filled_quantity": count, "filled_avg_price": leg.filled_avg_price if count else None,
                    "status": "canceled",
                }) for leg, count in zip(answer.legs, self.close_counts, strict=True)),
            })
        self.close_answers[client_order_id] = answer
        if self.lose_close_answer:
            self.lose_close_answer = False
            raise BrokerUnavailable("the broker received the close but its response was lost", broker="alpaca")
        return answer

    async def get_option_order_by_client_order_id(self, client_order_id):
        if client_order_id == self.entry.client_order_id:
            return self.entry
        return self.close_answers.get(client_order_id) or await self.port.get_option_order_by_client_order_id(client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id):
        if order_id == self.entry.order_id:
            return self.entry
        return await self.port.get_option_order_by_broker_order_id(order_id)

    async def get_order_by_client_order_id(self, client_order_id):
        return await self.port.get_order_by_client_order_id(client_order_id)

    async def get_order_by_broker_order_id(self, order_id):
        return await self.port.get_order_by_broker_order_id(order_id)

    async def cancel(self, order_id):
        await self.port.cancel(order_id)


@pytest.mark.parametrize("counts", [(1, 1), (1, 0), (0, 1), (2, 1)])
@pytest.mark.parametrize("lost", [False, True])
async def test_a_partial_entry_closes_exact_confirmed_contracts_and_recovers_a_lost_answer(world, counts, lost):
    from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence

    books, port = world
    entry = worked_entry(PAID_SPREAD, PAID_PICKS, units=2, now_ms=NOON)
    accepted = accept_option_enter(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="open", lifecycle_run_id=RUN_ID,
        entry=entry,
    )
    _bind(port, accepted.order_ref, entry)
    full = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
    answer = full.model_copy(update={
        "status": "canceled", "filled_quantity": min(counts),
        "legs": tuple(leg.model_copy(update={
            "filled_quantity": count, "filled_avg_price": leg.filled_avg_price if count else None, "status": "canceled",
        }) for leg, count in zip(full.legs, counts, strict=True)),
    })
    venue = Venue(port, answer, lose_close_answer=lost)
    fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=multi_leg_order_evidence(answer))
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close", entry_order_ref=accepted.order_ref,
        lifecycle_run_id=RUN_ID,
    )
    for _ in range(3):
        await resolve_exit(books, effect_operation_id=closing.effect_operation_id, trade=venue, pricing=UNPRICEABLE_RECOVERY, read=None)
    assert books.effect_operation(closing.effect_operation_id).state == "succeeded"
    assert not any(books.attributed_positions_for_strategy("bot-a").values())
    [sent] = venue.sent
    from app.broker.alpaca.clerk.sqlite.option_close import order_reductions

    assert {symbol: quantity for symbol, side, quantity in order_reductions(sent)} == {
        leg.contract.occ_symbol: count for leg, count in zip(entry.legs, counts, strict=True) if count
    }
    assert all(side.value == ("sell" if leg.position == "long" else "buy") for symbol, side, quantity in order_reductions(sent)
               for leg in entry.legs if leg.contract.occ_symbol == symbol)


@pytest.mark.parametrize("counts", [(1, 0), (0, 1), (1, 1)])
async def test_a_terminal_partial_close_never_claims_that_any_remaining_leg_is_flat(world, counts):
    books, port = world
    accepted, entry = await opened(world, PAID_SPREAD, PAID_PICKS)
    source = await port.get_option_order_by_client_order_id(accepted.order_ref)
    venue = Venue(port, source, close_counts=counts)
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close", entry_order_ref=accepted.order_ref,
        lifecycle_run_id=RUN_ID,
    )
    for _ in range(3):
        await resolve_exit(books, effect_operation_id=closing.effect_operation_id, trade=venue, pricing=UNPRICEABLE_RECOVERY, read=None)
    assert books.effect_operation(closing.effect_operation_id).state == "failed"
    assert books.attributed_positions_for_strategy("bot-a") == {
        leg.contract.occ_symbol: (2 - count) * (1 if leg.position == "long" else -1)
        for leg, count in zip(entry.legs, counts, strict=True)
    }
    assert len(venue.sent) == 1


async def test_a_credit_spreads_expiry_books_both_signed_cash_values_and_replays_once(tmp_path):
    from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
    from app.broker.alpaca.clerk.sqlite import option_events
    from app.services import market_liveness
    from tests._helpers.market_quotes import publish_quotes
    from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock
    from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import evidence
    from tests.broker.alpaca.clerk.sqlite.test_dry_run_option_expiry import _booked, _keeps_spy
    from tests.broker.alpaca.clerk.sqlite.test_option_close_fences import SATURDAY_NOON, _wait_until
    from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import SID, _binding, _open

    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    try:
        account = await _open(tmp_path, _TestClock(NOON), _binding(RECEIVED_SPREAD))
        await account.clerk.register_strategy_run(account.binding)
        await account.enter(evidence(NOON, RECEIVED_PICKS), plan=RECEIVED_SPREAD)
        _keeps_spy(account, "590.25")
        _wait_until(account, SATURDAY_NOON)
        await account.clerk.reconcile_once()
        assert account.held() == {}
        assert account.realized_pnl() == Decimal(-193)  # $307 credit less $500 exercise-value spread.
        assert len(_booked(account)) == 2
        settled = account.clerk._trade.settle_expired_options(after_ms=0)
        assert sorted(item.record.cash_cents for item in settled.settled) == [-102500, 52500]
        assert await account.clerk._read.list_positions() == []
        reading = account.account_reading()
        fees = account.fees().total_for(f"bot:{SID}")
        assert reading.cash_available_usd == Decimal(807) - fees
        budget = account.repo.account_budget(cash=reading.cash_available_usd, seen_before_ms=SATURDAY_NOON + 1)
        assert budget.option_collateral == 0 and budget.deployments[0].position_cost == 0
        account.repo.account_money(cash=reading.cash_available_usd, seen_before_ms=SATURDAY_NOON + 1)
        sequence = account.repo.last_custody_sequence()
        replay = option_events.book_option_events(
            account.repo, option_events.OptionEventRead(after_ms=0, history_complete=True), settled=settled,
        )
        assert (replay.booked, replay.held) == ((), ())
        assert account.repo.last_custody_sequence() == sequence
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


@pytest.mark.parametrize("working_entry", [False, True])
async def test_a_terminal_parent_with_a_working_child_keeps_custody_and_sends_no_second_close(world, working_entry):
    books, port = world
    if working_entry:
        entry = worked_entry(PAID_SPREAD, PAID_PICKS, units=2, now_ms=NOON)
        accepted = accept_option_enter(
            books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="open", lifecycle_run_id=RUN_ID,
            entry=entry,
        )
        _bind(port, accepted.order_ref, entry)
        source = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
        source = source.model_copy(update={
            "status": "canceled", "legs": (
                source.legs[0].model_copy(update={"status": "partially_filled"}), source.legs[1],
            ),
        })
        from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence

        fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=multi_leg_order_evidence(source))
        venue = Venue(port, source, close_counts=(1, 1))
    else:
        accepted, _entry = await opened(world, PAID_SPREAD, PAID_PICKS)
        source = await port.get_option_order_by_client_order_id(accepted.order_ref)
        venue = Venue(port, source, close_counts=(1, 1))
        submit = venue.submit_option_order

        async def child_still_works(order, *, client_order_id):
            answer = await submit(order, client_order_id=client_order_id)
            answer = answer.model_copy(update={"legs": (
                answer.legs[0].model_copy(update={"status": "partially_filled"}), answer.legs[1],
            )})
            venue.close_answers[client_order_id] = answer
            return answer

        venue.submit_option_order = child_still_works
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close", entry_order_ref=accepted.order_ref,
        lifecycle_run_id=RUN_ID,
    )
    for _ in range(3):
        await resolve_exit(books, effect_operation_id=closing.effect_operation_id, trade=venue, pricing=UNPRICEABLE_RECOVERY, read=None)
    assert books.active_exit_for_strategy("bot-a").effect_operation_id == closing.effect_operation_id
    assert books.effect_operation(closing.effect_operation_id).state in ("accepted", "unknown", "in_progress")
    assert len(venue.sent) == (0 if working_entry else 1)
    if not working_entry:
        parent = next(order for order in books.orders_for_effect_operation(closing.effect_operation_id) if order.role == "REDUCING")
        assert books.order_has_unproven_legs(parent.order_ref)


@pytest.mark.parametrize("kind", ["absent", "exists"])
@pytest.mark.parametrize("residual", [False, True])
async def test_operator_attestation_cannot_settle_a_structure_exit_from_one_contract(world, kind, residual):
    from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedUnknownExitOrder
    from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
    from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import (
        OrderOutcomeAttestation,
        UnknownOrderSettlementRefused,
        settle_unknown_exit_order,
    )

    books, port = world
    if residual:
        from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence

        entry = worked_entry(PAID_SPREAD, PAID_PICKS, units=2, now_ms=NOON)
        accepted = accept_option_enter(
            books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="open", lifecycle_run_id=RUN_ID,
            entry=entry,
        )
        _bind(port, accepted.order_ref, entry)
        full = await port.submit_option_order(entry.order, client_order_id=accepted.order_ref)
        source = full.model_copy(update={
            "status": "canceled", "filled_quantity": 0, "filled_avg_price": None,
            "legs": (
                full.legs[0].model_copy(update={"filled_quantity": 1, "status": "canceled"}),
                full.legs[1].model_copy(update={"filled_quantity": 0, "filled_avg_price": None, "status": "canceled"}),
            ),
        })
        fold_order_evidence(books, effect_operation_id=accepted.effect_operation_id, order=multi_leg_order_evidence(source))
    else:
        accepted, entry = await opened(world, PAID_SPREAD, PAID_PICKS)
        source = await port.get_option_order_by_client_order_id(accepted.order_ref)
    venue = Venue(port, source, lose_close_answer=True)

    async def absent_after_send(client_order_id):
        return source if client_order_id == accepted.order_ref else None

    venue.get_option_order_by_client_order_id = absent_after_send
    closing = accept_exit(
        books, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="close", entry_order_ref=accepted.order_ref,
        lifecycle_run_id=RUN_ID,
    )
    await resolve_exit(books, effect_operation_id=closing.effect_operation_id, trade=venue, pricing=UNPRICEABLE_RECOVERY, read=None)
    assert books.effect_operation(closing.effect_operation_id).state == "unknown"
    assert books.unknown_exit_orders("bot-a") == ()
    parent = next(order for order in books.orders_for_effect_operation(closing.effect_operation_id) if order.role == "REDUCING")
    presented = ProjectedUnknownExitOrder(
        effect_operation_id=closing.effect_operation_id, order_ref=parent.order_ref,
        symbol=entry.legs[0].contract.occ_symbol, side="SELL", quantity=2, created_at_ms=NOON,
        first_sent_at_ms=NOON, latest_send_sequence=books.last_custody_sequence(), lookup_wait_from_ms=NOON,
        answered_as_client_order_id=None, uncertainty_id="unsupported-parent",
    )
    sequence = books.last_custody_sequence()
    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await settle_unknown_exit_order(
            books, read=venue, trade=venue, intake=ReentrantAsyncLock(), strategy_instance_id="bot-a",
            presented=presented, attestation=OrderOutcomeAttestation(
                kind=kind, broker_order_id="aaaa0000-0000-0000-0000-000000000001" if kind == "exists" else None,
            ), operator="test-owner", operator_reason="reviewed",
        )
    assert refused.value.reason_code == "MULTI_LEG_ATTESTATION_NOT_SUPPORTED"
    assert books.last_custody_sequence() == sequence and len(venue.sent) == 1
    assert books.active_exit_for_strategy("bot-a").effect_operation_id == closing.effect_operation_id
