"""The poll of option events on a whole account: a stock-only one, a Paper one, and beside an outside order (#2913).

The Paper account is the real SQLite Clerk of ``resting_entry_harness``, with
its venue and its account read as doubles. Nothing was sent to Alpaca or
recorded from it.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import option_events, reads
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.option_events import book_option_events, poll_option_events
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionRequest, execute_recovery_action
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryPolicyContext, build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError, ReductionIntent, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import UNEXPLAINED_ORDER_HOLD_REASON_CODE
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import Capability
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerActivity, BrokerOrderEvent
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _broker_order_fixture,
    _FakeReadPort,
    _FakeTradePort,
    documented_option_activity,
)
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import (
    bookings,
    books_at,
    fifo,
    held,
    holds_option,
    option_holds,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (  # noqa: F401
    SYMBOL,
    PaperOptions,
    paper,
)
from tests.broker.alpaca.clerk.sqlite.test_resting_entries import (
    _a_stock_account_with_a_working_entry,
    _StepsCounted,
)

SATURDAY_NOON = et_minute_of_day_ms(date(2026, 9, 19), 12 * 60)


class _NeverAsked(_FakeReadPort):
    """An account read that fails the test when it is asked for option events or positions."""

    async def read_option_event_activities(self, **_asked: Any) -> Any:
        pytest.fail("a stock-only account's option events were asked for")

    async def list_positions(self) -> Any:
        pytest.fail("a stock-only account's positions were read for option events")


# ── a stock-only account ─────────────────────────────────────────────────────


async def test_a_stock_only_accounts_poll_asks_alpaca_nothing_and_writes_nothing(tmp_path: Path) -> None:
    repo = await _a_stock_account_with_a_working_entry(tmp_path, transitions=0)
    try:
        written = repo.last_custody_sequence()
        keeper = DeadlineKeeper(repo=repo, trade=_FakeTradePort(), read=_NeverAsked(), off_loop=run_inline)

        await keeper.tick()
        as_the_keeper = await keeper.option_events_read()
        as_the_pass = await poll_option_events(
            repo, read=_NeverAsked(), off_loop=run_inline, broker_positions=[], act_on_the_books_between_reads=True
        )
        booked = book_option_events(repo, None)

        assert as_the_keeper is not None and not as_the_keeper.read
        assert not as_the_pass.read and as_the_pass.round is None
        assert booked == option_events.OptionEventRound()
        assert repo.last_custody_sequence() == written
    finally:
        repo.close()


async def test_a_stock_only_accounts_books_are_never_read_for_option_events_however_long_their_log(
    tmp_path: Path,
) -> None:
    """Counted in the steps SQLite's machine takes, which grow with every row read: a log three thousand
    records longer costs not one step more, for the keeper's round, the pass's step and the batch function."""
    costs: dict[int, tuple[int, int, int]] = {}
    for transitions in (0, 3_000):
        repo = await _a_stock_account_with_a_working_entry(tmp_path / str(transitions), transitions=transitions)
        try:
            written = repo.last_custody_sequence()
            keeper = DeadlineKeeper(repo=repo, trade=_FakeTradePort(), read=_NeverAsked(), off_loop=run_inline)
            with _StepsCounted(repo) as the_round:
                await keeper.tick()
                await keeper.option_events_read()
            with _StepsCounted(repo) as the_passes_step:
                await poll_option_events(
                    repo, read=_NeverAsked(), off_loop=run_inline, broker_positions=[],
                    act_on_the_books_between_reads=True,
                )
            with _StepsCounted(repo) as the_batch:
                book_option_events(repo, None)
            assert repo.last_custody_sequence() == written
            costs[transitions] = (the_round.steps, the_passes_step.steps, the_batch.steps)
        finally:
            repo.close()

    assert costs[0] == costs[3_000]
    assert all(0 < steps < 150 for steps in costs[0])


async def test_between_two_reads_the_keepers_round_does_not_touch_the_books_for_option_events(tmp_path: Path) -> None:
    """The keeper wakes every few seconds. Until a read is due its option step runs no query at all."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", "AAPL230721C00150000", contracts=1)
    account = _FakeReadPort()
    try:
        first = await poll_option_events(repo, read=account, off_loop=run_inline)
        with _StepsCounted(repo) as between:
            second = await poll_option_events(repo, read=account, off_loop=run_inline)

        assert first.read and not second.read
        assert between.steps == 0 and len(account.option_events.reads) == 1
    finally:
        repo.close()


# ── a Paper account ──────────────────────────────────────────────────────────


async def _holding_one_call(account: PaperOptions, sid: str = "a") -> str:
    order_ref = await account.resting(sid)
    await account.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id=f"x-{sid}-buy")
    assert account.held(sid) == {SYMBOL: 1.0}
    return order_ref


def _on_saturday(account: PaperOptions) -> None:
    account.clock.value = SATURDAY_NOON
    account.repo.revive_execution_lease()


def _expiry_row(**changes: Any) -> dict[str, Any]:
    return documented_option_activity(
        "trading_api", "otm_option_expiry", **{"id": "exp-1", "symbol": SYMBOL, "date": "2026-09-18", "qty": "-1", **changes}
    )


async def test_a_pass_that_sees_an_expired_contract_gone_reads_its_expiry_books_it_and_ends_clean(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The broker's position is gone, and the books still hold it. The same pass reads why, books it and is clean.

    The pass's reads go through the Clerk's guarded ports, which refuse any
    broker call made while the intake lock is held.
    """
    await _holding_one_call(paper)
    _on_saturday(paper)
    paper.account.settled.add(SYMBOL)
    paper.account.option_events.rows = [_expiry_row()]
    read, trade = guard_broker_ports(read=paper.account, trade=paper.venue, intake=paper.clerk.intake)

    result = await reconcile_account(
        paper.repo, read=read, trade=trade, intake=paper.clerk.intake, pricing=paper.clerk.recovery_pricing
    )

    assert result.verdict == "clean"
    assert paper.held() == {}
    assert [row["summary_code"] for row in bookings(paper.repo)] == ["OPTION_EXPIRED"]
    assert fifo(paper.repo, "a").exact_realized_pnl == -194


async def test_until_the_activity_is_published_the_account_is_held_as_drifted_and_the_next_pass_books_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Paper publishes an expiry the next morning. Until then the books hold what the broker does not."""
    await _holding_one_call(paper)
    _on_saturday(paper)
    paper.account.settled.add(SYMBOL)

    before = await paper.pass_()

    assert before.verdict == "position_drift" and before.drifted_symbols == (SYMBOL,)
    assert ("a", "OPTION_EXPIRED_NOT_SETTLED") in option_holds(paper.repo)
    assert paper.held() == {SYMBOL: 1.0}

    paper.account.option_events.rows = [_expiry_row()]
    paper.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    after = await paper.pass_()

    assert after.verdict == "clean" and paper.held() == {}
    assert option_holds(paper.repo) == {}


async def test_the_keepers_round_books_an_expiry_with_no_pass_and_starts_none(paper: PaperOptions) -> None:  # noqa: F811
    await _holding_one_call(paper)
    _on_saturday(paper)
    paper.account.settled.add(SYMBOL)
    paper.account.option_events.rows = [_expiry_row()]
    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, read=paper.account, off_loop=run_inline)

    await keeper.tick()
    read = await keeper.option_events_read()

    assert read is not None and read.read
    assert paper.held() == {}
    # No pass ran: nothing was reconciled, and no entry was refused for one in progress.
    kinds = {kind for kind, _summary, _facts in paper.log()}
    assert "OPTION_POSITION_ENDED" in kinds and "RECONCILIATION_ATTEMPTED" not in kinds
    paper.repo.begin_reconciliation()
    paper.repo.end_reconciliation()


async def test_the_other_bot_on_the_account_is_not_held_by_one_bots_option_hold(paper: PaperOptions) -> None:  # noqa: F811
    await _holding_one_call(paper, "a")
    _on_saturday(paper)
    paper.account.option_events.rows = [_expiry_row(qty="-0.5")]
    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, read=paper.account, off_loop=run_inline)

    await keeper.tick()
    await keeper.option_events_read()

    assert option_holds(paper.repo).keys() == {("a", "OPTION_EVENT_UNREADABLE")}
    assert decide_capability(paper.repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id="b").allowed


async def _exercised_into_a_hundred_shares(paper: PaperOptions) -> None:  # noqa: F811
    """One call struck at 585 is exercised: the bot holds 100 SPY at $585.00. The owner then stops it."""
    await _holding_one_call(paper)
    _on_saturday(paper)
    paper.account.settled.add(SYMBOL)
    paper.account.shares["SPY"] = 100.0
    paper.account.option_events.rows = [
        documented_option_activity(
            "trading_api", "option_exercise", id="exc-1", symbol=SYMBOL, date="2026-09-18", qty="-1"
        ),
        documented_option_activity(
            "trading_api", "option_exercise", 1, id="trd-1", symbol="SPY", date="2026-09-18", qty="100", price="585",
            net_amount="-58500",
        ),
    ]
    await paper.pass_()
    assert paper.held() == {"SPY": 100.0}
    assert option_holds(paper.repo).keys() == {("a", "OPTION_SHARES_HELD")}
    submit_stop_run(paper.repo, account_id=paper.repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a")


async def _recovery_context(paper: PaperOptions) -> RecoveryPolicyContext:  # noqa: F811
    found = await read_recovery_context(paper.clerk, strategy_instance_id="a")
    assert found is not None
    return found


async def test_the_shares_an_exercise_left_on_paper_are_offered_to_the_flatten(paper: PaperOptions) -> None:  # noqa: F811
    await _exercised_into_a_hundred_shares(paper)

    await paper.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    catalog = build_recovery_catalog(await _recovery_context(paper))
    flatten = next(item for item in catalog if item.action_id == "prepare_safe_flatten")

    assert flatten.available, (flatten.unavailable_reason_code, flatten.unavailable_reason)
    assert flatten.reduction_plan is not None
    assert [(leg.symbol, leg.quantity) for leg in flatten.reduction_plan.legs] == [("SPY", 100.0)]


async def test_the_owners_flatten_sells_the_shares_an_exercise_left_at_the_venue_and_the_bot_is_flat(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """No ENTER bought these shares, and the order they were booked on was never at the broker.

    The owner's Flatten sells them all the same: one order, to sell 100 SPY,
    reaches the venue. Nothing asks the venue about the order the shares
    were booked on, which it never had. The venue fills the sale at $590.25:
    100 x (590.25 - 585.00) = $525.00 on the shares, less the $194.00 the
    call cost. The bot is flat, the hold for the shares ends, and the
    account agrees.
    """
    await _exercised_into_a_hundred_shares(paper)
    with paper.repo.write_fence() as conn:
        [shares_order] = [
            row[0] for row in conn.execute("SELECT order_ref FROM orders WHERE order_ref LIKE '%:os%'")
        ]
    monday_morning = et_minute_of_day_ms(date(2026, 9, 21), 10 * 60)
    paper.clock.value = monday_morning
    paper.repo.revive_execution_lease()
    publish_quotes(monday_morning, SPY=590.25)
    await paper.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    catalog = build_recovery_catalog(await _recovery_context(paper))
    presented = next(item for item in catalog if item.action_id == "execute_safe_flatten")
    assert presented.available, (presented.unavailable_reason_code, presented.unavailable_reason)

    sent = await execute_recovery_action(
        paper.clerk,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten", concurrency_token=presented.concurrency_token,
            execution_ref=presented.execution_ref, reason="owner-flatten",
        ),
        current_context=lambda: _recovery_context(paper),
    )

    # The sale reached the venue: one stock order, for exactly the shares the exercise left.
    assert [(leg.symbol, leg.side, leg.quantity) for leg in paper.venue.submitted_legs] == [("SPY", "sell", 100.0)]
    assert shares_order not in paper.venue.lookup_calls and paper.venue.cancel_calls == []
    [selling] = sent.orders
    filled = _broker_order_fixture(
        selling.order_ref, order_id=f"bo-{selling.order_ref}", symbol="SPY", status="filled", side="sell",
        quantity=100, filled_quantity=100, filled_avg_price=590.25,
    )
    sink = SqliteTradeUpdateEvidenceSink(repo=paper.repo, intake=paper.clerk.intake, reconciler=paper.clerk)
    await sink.record_lifecycle_event(
        client_order_id=selling.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=paper.clock(), price=590.25, quantity=100, execution_id="x-a-flatten",
        ),
        event_key="execution:x-a-flatten", order=filled, recovery_source=None, recovery_window_limit=None,
    )
    paper.account.shares.clear()
    paper.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    after = await paper.pass_()

    assert paper.held() == {}
    assert fifo(paper.repo, "a").exact_realized_pnl == Decimal("331")
    assert option_holds(paper.repo) == {} and after.verdict == "clean"


# ── an order the Clerk did not place, in a contract a bot holds ──────────────

CALL = "AAPL230721C00150000"


def _outside(order_id: str, *, status: str, side: str = "sell", quantity: float = 1, filled: float = 0,
             price: float | None = None, symbol: str = CALL) -> Any:
    return _broker_order_fixture(
        f"not-ours-{order_id}", order_id=order_id, symbol=symbol, status=status, side=side, quantity=quantity,
        filled_quantity=filled, filled_avg_price=price,
    )


def _account_hold(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_hold(scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE)


def _sale_refused_for(repo: ClerkSqliteRepository, bot: str, symbol: str, contracts: float) -> str | None:
    """Why the bot's own order to sell ``contracts`` of ``symbol`` would not be sent, or None when it would."""
    return decide_capability(
        repo, capability=Capability.REDUCE, strategy_instance_id=bot,
        reduction_intent=ReductionIntent(symbol=symbol, side="SELL", quantity=contracts),
    ).reason_code


def _bot_may_sell(repo: ClerkSqliteRepository, bot: str, symbol: str, contracts: float) -> bool:
    return _sale_refused_for(repo, bot, symbol, contracts) is None


OTHER_CALL = "AAPL230721C00160000"


def test_a_working_sale_the_clerk_did_not_place_holds_the_bot_that_holds_the_contract_and_not_the_account(
    tmp_path: Path,
) -> None:
    """Before, such an order held the whole account and refused every reduction of every bot.

    Now it stands against the one bot in the one contract. That bot's own
    order to close the same contracts is not sent beside it: it would be a
    second closing order. Its other contract, and every other bot, go on.
    """
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "bot-b": "AAPL", "bot-c": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    holds_option(repo, "bot-a", OTHER_CALL, contracts=1)
    holds_option(repo, "bot-c", OTHER_CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="new"))
        book_option_events(repo, None)

        assert _account_hold(repo) is None
        assert option_holds(repo).keys() == {("bot-a", "OPTION_ORDER_NOT_OURS")}
        assert _sale_refused_for(repo, "bot-a", CALL, 1) == "OPTION_ORDER_NOT_OURS"
        assert _bot_may_sell(repo, "bot-a", OTHER_CALL, 1)
        assert _bot_may_sell(repo, "bot-c", OTHER_CALL, 1)
        assert decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id="bot-b").allowed
    finally:
        repo.close()


def test_the_bots_own_close_waits_from_the_moment_the_order_is_on_the_books_before_any_hold_is_raised(
    tmp_path: Path,
) -> None:
    """Alpaca's stream can show the order minutes before a pass or a poll raises the bot's hold.

    The record of the order is itself what every close of the bot asks. So
    no second order to sell the same contracts goes out in between.
    """
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        assert _bot_may_sell(repo, "bot-a", CALL, 1)

        observe_external_order(repo, order=_outside("alpaca-sells-1", status="new"))

        assert option_holds(repo) == {} and _account_hold(repo) is None
        assert _sale_refused_for(repo, "bot-a", CALL, 1) == "OPTION_ORDER_NOT_OURS"
    finally:
        repo.close()


def test_an_outside_sale_stands_in_the_way_of_the_bot_it_reduces_and_not_of_a_bot_short_the_same_contract(
    tmp_path: Path,
) -> None:
    """One bot bought the call and another sold it. A sale from outside reduces the first and adds to the second."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bought": "AAPL", "sold": "AAPL"})
    holds_option(repo, "bought", CALL, contracts=1)
    holds_option(repo, "sold", CALL, contracts=-1)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="new"))

        assert _sale_refused_for(repo, "bought", CALL, 1) == "OPTION_ORDER_NOT_OURS"
        bought_back = decide_capability(
            repo, capability=Capability.REDUCE, strategy_instance_id="sold",
            reduction_intent=ReductionIntent(symbol=CALL, side="BUY", quantity=1),
        )
        assert bought_back.allowed, bought_back.why
    finally:
        repo.close()


def test_once_the_order_has_ended_with_nothing_filled_the_bots_own_close_goes_on(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="new"))
        book_option_events(repo, None)
        assert not _bot_may_sell(repo, "bot-a", CALL, 1)

        observe_external_order(repo, order=_outside("alpaca-sells-1", status="canceled"))

        assert _bot_may_sell(repo, "bot-a", CALL, 1)
        book_option_events(repo, None)
        assert option_holds(repo) == {} and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


def test_a_sale_that_filled_holds_the_bots_own_close_back_until_it_is_booked_and_then_only_what_is_left_is_sold(
    tmp_path: Path,
) -> None:
    """The bot holds two. The order sold one. Until that is booked the books still say two, and a sale of two would oversell."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=2)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="filled", filled=1, price=0.40))
        assert _sale_refused_for(repo, "bot-a", CALL, 2) == "OPTION_ORDER_NOT_OURS"

        book_option_events(repo, None)

        assert held(repo, "bot-a") == {CALL: 1.0}
        assert _bot_may_sell(repo, "bot-a", CALL, 1)
    finally:
        repo.close()


def test_an_order_the_owner_has_reviewed_holds_nothing_back_and_its_fill_is_still_booked(tmp_path: Path) -> None:
    """The owner's review of an outside order released the account's hold before. It releases the bot's the same way."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1, price=1.94)
    try:
        observe_external_order(repo, order=_outside("by-hand-1", status="new"))
        book_option_events(repo, None)
        [order] = repo.external_order_resources()

        acknowledge_external_order(repo, external_order_id=order.external_order_id, operator="owner")
        book_option_events(repo, None)

        assert option_holds(repo) == {}
        assert _bot_may_sell(repo, "bot-a", CALL, 1)

        observe_external_order(repo, order=_outside("by-hand-1", status="filled", filled=1, price=0.40))
        book_option_events(repo, None)

        assert held(repo, "bot-a") == {} and fifo(repo, "bot-a").exact_realized_pnl == -154
        assert option_holds(repo) == {}
    finally:
        repo.close()


def test_acknowledging_the_bots_hold_for_the_order_lets_its_own_close_go_on(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("by-hand-1", status="new"))
        book_option_events(repo, None)

        assert option_events.acknowledge_option_event_hold(
            repo, strategy_instance_id="bot-a", reason_code="OPTION_ORDER_NOT_OURS", operator="owner"
        )

        assert _bot_may_sell(repo, "bot-a", CALL, 1)
        book_option_events(repo, None)
        assert option_holds(repo) == {}
    finally:
        repo.close()


def test_an_outside_order_in_a_contract_no_bot_holds_still_holds_the_account_as_it_always_did(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("by-hand-1", status="new", symbol="AAPL230721C00160000"))
        # A purchase adds to nothing the bot holds: it is not on the reducing side.
        observe_external_order(repo, order=_outside("by-hand-2", status="new", side="buy"))
        observe_external_order(repo, order=_outside("by-hand-3", status="new", symbol="AAPL"))
        book_option_events(repo, None)

        hold = _account_hold(repo)
        assert hold is not None
        assert sorted(json.loads(hold["evidence_refs_json"])) == ["by-hand-1", "by-hand-2", "by-hand-3"]
        assert option_holds(repo) == {}
    finally:
        repo.close()


def test_a_sale_by_the_broker_that_has_filled_is_booked_against_the_bot_at_the_brokers_price(tmp_path: Path) -> None:
    """One call bought at $1.94 is sold by an order the Clerk did not place at $0.40: (0.40 - 1.94) x 100 = -$154.00."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1, price=1.94)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="new"))
        book_option_events(repo, None)
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="filled", filled=1, price=0.40))

        first = book_option_events(repo, None)
        again = book_option_events(repo, None)

        assert len(first.booked) == 1 and again.booked == ()
        assert held(repo, "bot-a") == {}
        assert fifo(repo, "bot-a").exact_realized_pnl == -154
        [booked] = bookings(repo)
        assert booked["summary_code"] == "OPTION_SOLD_OUTSIDE"
        assert option_holds(repo) == {} and _account_hold(repo) is None
    finally:
        repo.close()


def test_a_sale_of_two_bots_contracts_takes_the_oldest_lot_first(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"first": "AAPL", "second": "AAPL"})
    holds_option(repo, "first", CALL, contracts=1)
    holds_option(repo, "second", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="filled", filled=1, price=0.40))

        book_option_events(repo, None)

        assert held(repo, "first") == {} and held(repo, "second") == {CALL: 1.0}
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("order", "why"),
    [
        pytest.param({"status": "filled", "filled": 2, "price": 0.40, "quantity": 2}, "more than", id="more than the bots hold"),
        pytest.param({"status": "filled", "filled": 1, "price": None}, "no price", id="no price"),
    ],
)
def test_a_sale_the_books_cannot_count_is_not_booked_and_the_hold_says_why(
    tmp_path: Path, order: dict[str, Any], why: str
) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", **order))

        book_option_events(repo, None)

        hold = option_holds(repo)[("bot-a", "OPTION_ORDER_NOT_OURS")]
        assert why in hold["facts_json"]
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


def test_a_sale_is_not_booked_while_another_outside_order_holds_the_same_contract(tmp_path: Path) -> None:
    """The owner bought one by hand and the bot bought one. One is sold from outside: whose is not known."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("by-hand-buy", status="filled", side="buy", filled=1, price=1.5))
        observe_external_order(repo, order=_outside("a-sale", status="filled", filled=1, price=0.40))

        book_option_events(repo, None)

        assert ("bot-a", "OPTION_ORDER_NOT_OURS") in option_holds(repo)
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_a_pass_does_not_put_a_bots_outside_order_back_on_the_accounts_hold(paper: PaperOptions) -> None:  # noqa: F811
    """The pass sets the account's unexplained-order hold from the open orders it sees, every time it runs."""
    await _holding_one_call(paper)
    paper.account.outside_orders = [_outside("alpaca-sells-1", status="new", symbol=SYMBOL)]

    await paper.pass_()

    assert _account_hold(paper.repo) is None
    assert option_holds(paper.repo).keys() == {("a", "OPTION_ORDER_NOT_OURS")}
    assert _sale_refused_for(paper.repo, "a", SYMBOL, 1) == "OPTION_ORDER_NOT_OURS"


def test_a_sale_that_has_filled_in_part_and_is_still_working_is_booked_only_once_it_has_ended(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=2)
    try:
        observe_external_order(
            repo, order=_outside("alpaca-sells-1", status="partially_filled", quantity=2, filled=1, price=0.40)
        )
        book_option_events(repo, None)
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 2.0}

        observe_external_order(repo, order=_outside("alpaca-sells-1", status="filled", quantity=2, filled=2, price=0.45))
        book_option_events(repo, None)

        assert held(repo, "bot-a") == {} and len(bookings(repo)) == 1
        assert fifo(repo, "bot-a").exact_realized_pnl == (45 - 194) * 2
    finally:
        repo.close()


async def test_a_pass_that_sees_a_contracts_position_differ_reads_at_once_and_not_at_the_next_cadence(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The last read was whole and left nothing pending, so the next is five minutes off. A minute later the
    broker's position is gone: the pass does not wait the other four."""
    await _holding_one_call(paper)
    await paper.pass_()
    assert len(paper.account.option_events.reads) == 1 and option_holds(paper.repo) == {}
    paper.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    await paper.pass_()
    assert len(paper.account.option_events.reads) == 1, "nothing differs, so nothing is read before the cadence"

    _on_saturday(paper)
    paper.account.settled.add(SYMBOL)
    paper.account.option_events.rows = [_expiry_row()]
    # The clock is set to a minute after the last read, not five: only the difference asks for this read.
    paper.clock.value = SATURDAY_NOON
    state = option_events._poll_state(paper.repo)
    state.read_at_ms = SATURDAY_NOON - option_events.OPTION_EVENT_POLL_RETRY_MS
    await paper.pass_()

    assert len(paper.account.option_events.reads) == 2 and paper.held() == {}


async def test_a_fault_in_the_option_step_does_not_fail_the_pass_and_the_position_still_shows_as_drift(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """The pass is what every bot's custody rests on. It reaches its verdict whatever the option step did."""
    await _holding_one_call(paper)
    _on_saturday(paper)
    paper.account.settled.add(SYMBOL)
    paper.account.option_events.rows = [_expiry_row()]

    def breaks(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a fault in the booking of option events")

    monkeypatch.setattr(option_events, "book_option_events", breaks)

    result = await paper.pass_()

    assert result.verdict == "position_drift" and result.drifted_symbols == (SYMBOL,)
    assert paper.held() == {SYMBOL: 1.0}


async def test_a_poll_that_raises_is_tried_again_a_minute_later_and_not_on_the_keepers_next_round(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    await _holding_one_call(paper)
    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, read=paper.account, off_loop=run_inline)
    real_booking = option_events.book_option_events

    def breaks(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a fault in the booking of option events")

    monkeypatch.setattr(option_events, "book_option_events", breaks)
    # The read's failure is its own: the round that started it is not failed by it.
    await keeper.tick()
    assert await keeper.option_events_read() is None
    paper.wait(5_000)

    await keeper.tick()
    assert (await keeper.option_events_read()).read is False
    assert len(paper.account.option_events.reads) == 1

    monkeypatch.setattr(option_events, "book_option_events", real_booking)
    paper.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    await keeper.tick()
    assert (await keeper.option_events_read()).read is True


async def test_a_slow_read_of_option_events_does_not_hold_the_keepers_round_or_the_next_one(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """A resting entry's cancel is owed within two seconds. The walk of the account's activities can take
    longer, so the round starts it and does not wait: two rounds finish while one read is still out."""
    import asyncio

    await _holding_one_call(paper)
    answered = asyncio.Event()
    real_read = paper.account.option_events.read_option_event_activities
    asked = 0

    async def slow(**asked_for: Any) -> Any:
        nonlocal asked
        asked += 1
        await answered.wait()
        return await real_read(**asked_for)

    paper.account.option_events.read_option_event_activities = slow  # type: ignore[method-assign]
    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, read=paper.account, off_loop=run_inline)
    try:
        await asyncio.wait_for(keeper.tick(), timeout=5)
        await asyncio.sleep(0)
        await asyncio.wait_for(keeper.tick(), timeout=5)
        assert asked == 1, "a second read was started while the first was still out"

        answered.set()
        read = await keeper.option_events_read()
        assert read is not None and read.read
    finally:
        answered.set()
        await keeper.stop()



async def test_a_bots_exit_sends_no_second_closing_order_beside_the_brokers_own_sale_and_ends_flat_when_it_filled(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Alpaca is selling the bot's one call. The bot decides to exit in the same minute.

    The exit is taken and kept, and no order of the bot's is sent: two
    orders would be selling one contract. When Alpaca's sale has filled it is
    booked against the bot, and the exit that was kept ends with nothing left to sell.
    """
    await _holding_one_call(paper)
    paper.account.outside_orders = [_outside("alpaca-sells-1", status="new", symbol=SYMBOL)]
    await paper.pass_()
    sent_before = list(paper.venue.option_submit_calls)

    with pytest.raises(AdmissionBlockedError) as refused:
        await paper.exit()

    assert refused.value.decision.reason_code == "OPTION_ORDER_NOT_OURS"
    kept = paper.repo.active_exit_for_strategy("a")
    assert kept is not None and paper.venue.option_submit_calls == sent_before

    # Alpaca's stream reports the sale filled, and the account no longer holds the contract.
    observe_external_order(
        paper.repo, order=_outside("alpaca-sells-1", status="filled", filled=1, price=0.40, symbol=SYMBOL)
    )
    paper.account.outside_orders = []
    paper.account.settled.add(SYMBOL)
    await paper.pass_()
    await paper.pass_()

    assert paper.held() == {} and option_holds(paper.repo) == {}
    assert [booked["summary_code"] for booked in bookings(paper.repo)] == ["OPTION_SOLD_OUTSIDE"]
    assert paper.venue.option_submit_calls == sent_before
    assert paper.repo.active_exit_for_strategy("a") is None


# ── a booked sale beside the account's money ─────────────────────────────────


def _sale_at(at_ms: int, **changes: Any) -> Any:
    """Alpaca's own sale of bot ``a``'s one call, filled at $0.40, as the stream shows it at ``at_ms``."""
    order = _outside("alpaca-sells-1", **{"status": "filled", "filled": 1, "price": 0.40, "symbol": SYMBOL, **changes})
    return order.model_copy(update={"submitted_at_ms": at_ms, "created_at_ms": at_ms, "updated_at_ms": at_ms, "observed_at_ms": at_ms})


def _the_sales_execution(at_ms: int) -> BrokerActivity:
    """The one execution of that sale, as the account's trade activities list it."""
    return BrokerActivity(
        broker="alpaca", activity_id="20260918::alpaca-sells-1-fill", native_order_id="alpaca-sells-1",
        activity_type="FILL", category="trade_activity", symbol=SYMBOL, side="sell", quantity=1, price=0.40,
        net_amount=None, occurred_at_ms=at_ms, observed_at_ms=at_ms,
    )


async def test_a_sale_booked_against_a_bot_is_counted_once_by_the_fees_and_the_money_and_is_an_outside_order_no_more(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Bot ``a`` bought one call at $1.94. Alpaca sells it at $0.40 and the sale is booked against the bot.

    The one execution is then the bot's and nobody else's. Before, it was
    counted a second time as an outside order's: priced once more by the
    stock fee model, added to the sales the cash has not seen at 40 cents
    for one "share", and named for good as one contract sold short by
    outside orders, on an account that is flat.

    By hand, the day's fees over the bot's two fills (one contract bought,
    one sold at $0.40), each fee type rounded up to the cent once for the
    day: SEC 40.00 x 0.0000206, 1 cent; TAF 0.00329, 1; CAT 2 x 0.0003, 1;
    ORF 2 x 0.015, 3; OCC 2 x 0.025, 5. Eleven cents. The sale brings in
    1 x 0.40 x 100 = $40.00.
    """
    await _holding_one_call(paper)
    sold_at = paper.clock()
    observe_external_order(paper.repo, order=_sale_at(sold_at))
    record_fee_evidence(paper.repo, [_the_sales_execution(sold_at)], checked_at_ms=sold_at, history_complete=True)

    done = book_option_events(paper.repo, None)
    paper.wait(1_000)
    record_fee_evidence(paper.repo, [], checked_at_ms=paper.clock(), history_complete=True)

    assert len(done.booked) == 1 and paper.held() == {}
    fees = paper.repo.fee_attribution(now_ms=paper.clock())
    assert fees.known, fees.unresolved
    assert fees.external_fills == ()
    assert fees.predicted_total == fees.total_for(bot_subject_id("a")) == Decimal("0.11")
    # The cash was last read before the sale: its proceeds are still to come, once.
    money = paper.repo.account_money(cash=100_000 - 194, seen_before_ms=sold_at)
    assert money.unseen_sales == Decimal("40")
    assert money.unvalued == ()
    # No read that lists outside orders lists it again: the record of what was observed stays.
    assert paper.repo.external_order_resources() == ()
    with paper.repo.write_fence() as conn:
        assert reads.filled_outside_order_ids(conn) == frozenset()
    assert paper.repo.external_order_by_broker_order_id("alpaca-sells-1") is not None


async def test_until_it_is_booked_a_filled_sale_is_still_the_outside_order_it_always_was(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Only the booking makes the execution the bot's. Before it, the order is counted outside, as any is."""
    await _holding_one_call(paper)
    sold_at = paper.clock()
    observe_external_order(paper.repo, order=_sale_at(sold_at))
    record_fee_evidence(paper.repo, [_the_sales_execution(sold_at)], checked_at_ms=sold_at, history_complete=True)

    fees = paper.repo.fee_attribution(now_ms=paper.clock())

    assert [fill.native_order_id for fill in fees.external_fills] == ["alpaca-sells-1"]
    assert [order.broker_order_id for order in paper.repo.external_order_resources()] == ["alpaca-sells-1"]
