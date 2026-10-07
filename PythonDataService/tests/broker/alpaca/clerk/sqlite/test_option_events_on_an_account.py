"""The poll of option events on a whole account: a stock-only one, a Paper one, and beside an outside order (#2913).

The Paper account is the real SQLite Clerk of ``resting_entry_harness``, with
its venue and its account read as doubles. Nothing was sent to Alpaca or
recorded from it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.option_events import book_option_events, poll_option_events
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryPolicyContext, build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import ReductionIntent, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import UNEXPLAINED_ORDER_HOLD_REASON_CODE
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import Capability
from app.services.session_authority import et_minute_of_day_ms
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

        round_ = await keeper.tick()
        as_the_pass = await poll_option_events(
            repo, read=_NeverAsked(), off_loop=run_inline, broker_positions=[], act_on_the_books_between_reads=True
        )
        booked = book_option_events(repo, None)

        assert round_.option_events is not None and not round_.option_events.read
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

    done = await keeper.tick()

    assert done.option_events is not None and done.option_events.read
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

    assert option_holds(paper.repo).keys() == {("a", "OPTION_EVENT_UNREADABLE")}
    assert decide_capability(paper.repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id="b").allowed


async def test_the_shares_an_exercise_left_on_paper_are_offered_to_the_flatten_and_sold_by_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """One call struck at 585 is exercised: the bot holds 100 SPY. The owner stops it and flattens."""
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

    async def context() -> RecoveryPolicyContext:
        found = await read_recovery_context(paper.clerk, strategy_instance_id="a")
        assert found is not None
        return found

    await paper.clerk.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    flatten = next(item for item in build_recovery_catalog(await context()) if item.action_id == "prepare_safe_flatten")

    assert flatten.available, (flatten.unavailable_reason_code, flatten.unavailable_reason)
    assert flatten.reduction_plan is not None
    assert [(leg.symbol, leg.quantity) for leg in flatten.reduction_plan.legs] == [("SPY", 100.0)]


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


def _bot_may_sell(repo: ClerkSqliteRepository, bot: str, symbol: str, contracts: float) -> bool:
    return decide_capability(
        repo, capability=Capability.REDUCE, strategy_instance_id=bot,
        reduction_intent=ReductionIntent(symbol=symbol, side="SELL", quantity=contracts),
    ).allowed


def test_a_working_sale_the_clerk_did_not_place_holds_the_bot_that_holds_the_contract_and_not_the_account(
    tmp_path: Path,
) -> None:
    """Today such an order holds the whole account and refuses reductions, the bot's own close included."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "bot-b": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    try:
        observe_external_order(repo, order=_outside("alpaca-sells-1", status="new"))
        book_option_events(repo, None)

        assert _account_hold(repo) is None
        assert option_holds(repo).keys() == {("bot-a", "OPTION_ORDER_NOT_OURS")}
        assert _bot_may_sell(repo, "bot-a", CALL, 1)
        assert decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id="bot-b").allowed
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
    assert _bot_may_sell(paper.repo, "a", SYMBOL, 1)
