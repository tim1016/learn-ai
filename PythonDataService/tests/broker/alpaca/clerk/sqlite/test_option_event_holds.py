"""What the Clerk holds a bot for when an option event cannot be booked as it reads (#2913).

One test for each hold. Each is raised for the bot it can be tied to and
account-wide otherwise, blocks that bot's entries, and leaves reductions
open. The rows are Alpaca's documented ones with a contract, a date or a
count laid over them, read by the real port. Nothing was recorded from
Alpaca.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.option_events import acknowledge_option_event_hold
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import ReductionIntent, admit_new_exposure, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import Capability
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import (
    bookings,
    books_at,
    broker_holds,
    held,
    holds_option,
    later,
    option_holds,
    poll,
)

CALL = "AAPL230721C00150000"
OTHER_CALL = "AAPL230721C00160000"
THE_DAY_BEFORE = date(2023, 7, 20)
SATURDAY = date(2023, 7, 22)


def _expiry(**changes: Any) -> dict[str, Any]:
    return documented_option_activity("trading_api", "otm_option_expiry", **{"id": "exp-1", "qty": "-1", **changes})


def _exercise(**changes: Any) -> dict[str, Any]:
    return documented_option_activity("trading_api", "option_exercise", **{"id": "exc-1", "qty": "-1", **changes})


def _assignment(**changes: Any) -> dict[str, Any]:
    return documented_option_activity("trading_api", "option_assignment", **{"id": "asn-1", "qty": "1", **changes})


def _share_trade(shares: int, **changes: Any) -> dict[str, Any]:
    return documented_option_activity(
        "trading_api", "option_exercise", 1,
        **{"id": "trd-1", "qty": str(shares), "price": "150", "net_amount": str(-150 * shares), **changes},
    )


def _one_bot(tmp_path: Path, contracts: int = 1, *, day: date = THE_DAY_BEFORE) -> ClerkSqliteRepository:
    repo = books_at(tmp_path, day, {"bot-a": "AAPL", "bot-b": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=contracts)
    return repo


async def _read(repo: ClerkSqliteRepository, account: _FakeReadPort, *rows: dict[str, Any]) -> None:
    """One more read of the account's option events, a minute after the last."""
    account.option_events.rows = list(rows)
    later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
    await poll(repo, account, asked_for=True)


def _cause(repo: ClerkSqliteRepository, bot: str | None, reason_code: str) -> list[dict[str, Any]]:
    return json.loads(option_holds(repo)[(bot, reason_code)]["facts_json"])["cause_facts"]["items"]


def _entry_refused_for(repo: ClerkSqliteRepository, bot: str) -> str | None:
    """Why the bot's next entry would be refused, or None when it would be taken.

    A bot that holds a position takes no second entry whatever else is
    true, under a reason of its own. A hold is asked first, so a held bot
    is refused under the hold's own code.
    """
    return admit_new_exposure(repo, strategy_instance_id=bot).reason_code


def _may_reduce(repo: ClerkSqliteRepository, bot: str, symbol: str, quantity: float) -> bool:
    return decide_capability(
        repo, capability=Capability.REDUCE, strategy_instance_id=bot,
        reduction_intent=ReductionIntent(symbol=symbol, side="SELL" if quantity > 0 else "BUY", quantity=abs(quantity)),
    ).allowed


# ── a hold is the bot's own ──────────────────────────────────────────────────


async def test_a_hold_stops_that_bots_entries_and_no_other_bots_and_leaves_its_reductions_open(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-0.5"))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENT_UNREADABLE")}
        assert _entry_refused_for(repo, "bot-a") == "OPTION_EVENT_UNREADABLE"
        assert _entry_refused_for(repo, "bot-b") is None
        assert _may_reduce(repo, "bot-a", CALL, 1)
    finally:
        repo.close()


async def test_a_row_that_names_no_bots_contract_holds_the_account(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(symbol="MSFT230721C00300000", qty="-0.5"))

        assert option_holds(repo).keys() == {(None, "OPTION_EVENT_UNREADABLE")}
        assert _entry_refused_for(repo, "bot-a") == _entry_refused_for(repo, "bot-b") == "OPTION_EVENT_UNREADABLE"
        assert _may_reduce(repo, "bot-a", CALL, 1)
    finally:
        repo.close()


# ── one test for each hold ───────────────────────────────────────────────────


async def test_a_row_that_cannot_be_read_is_held_for_and_nothing_of_it_is_booked(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-0.5"))

        [row] = _cause(repo, "bot-a", "OPTION_EVENT_UNREADABLE")
        assert row["ref"] == "option-activity:exp-1" and row["symbol"] == CALL
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_an_option_corporate_action_is_listed_and_held_for(tmp_path: Path) -> None:
    """Corporate actions and cash deliverables are not read. Each is named, never skipped."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(activity_type="OPCA", id="ca-1"))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENT_UNREADABLE")}
        assert held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_a_row_not_reported_as_executed_is_held_for_and_matched_to_nothing(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(status="canceled"))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENT_NOT_EXECUTED")}
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_an_event_in_a_contract_a_bot_traded_and_holds_no_more_is_held_account_wide(tmp_path: Path) -> None:
    """The books recorded the contract and say nobody holds it. The broker says a position in it ended."""
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-a", CALL, contracts=-1)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry())

        assert option_holds(repo).keys() == {(None, "OPTION_EVENT_CONTRACT_NOT_HELD")}
        assert bookings(repo) == []
    finally:
        repo.close()


HAND_TRADED = "MSFT230721C00300000"


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([{"kind": "expiry", "symbol": HAND_TRADED}], id="it expired"),
        pytest.param(
            [{"kind": "exercise", "symbol": HAND_TRADED}, {"kind": "shares", "shares": 100}],
            id="it was exercised, with its share trade",
        ),
    ],
)
async def test_an_event_in_a_contract_the_books_never_recorded_holds_nobody(
    tmp_path: Path, rows: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """The owner traded an option by hand in the same account, and the Clerk never saw its order.

    Its end concerns no bot: no bot can hold a contract the books never
    recorded. So nothing is booked and nobody is held, the option bot and
    the stock bot alike. It is logged, and the read after it finds the same.
    """
    made = {
        "expiry": lambda row: _expiry(symbol=row["symbol"]),
        "exercise": lambda row: _exercise(symbol=row["symbol"]),
        "shares": lambda row: _share_trade(row["shares"], symbol="MSFT", price="300", net_amount="-30000"),
    }
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"option-bot": "AAPL", "stock-bot": "SPY"})
    holds_option(repo, "option-bot", CALL, contracts=1)
    account = _FakeReadPort()
    written = repo.last_custody_sequence()
    try:
        with caplog.at_level("INFO", logger=option_events.logger.name):
            await _read(repo, account, *(made[row["kind"]](row) for row in rows))
        await _read(repo, account, *(made[row["kind"]](row) for row in rows))

        assert option_holds(repo) == {} and bookings(repo) == []
        assert repo.last_custody_sequence() == written
        assert decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id="stock-bot").allowed
        assert _entry_refused_for(repo, "option-bot") not in option_events.OPTION_EVENT_HOLD_REASON_CODES
        assert any(
            getattr(record, "action", None) == "option_event_in_a_contract_never_recorded"
            and getattr(record, "symbol", None) == HAND_TRADED
            for record in caplog.records
        )
    finally:
        repo.close()


async def test_an_event_for_more_than_a_hand_bought_holding_the_books_saw_part_of_holds_nobody(
    tmp_path: Path,
) -> None:
    """The owner bought two by hand. The Clerk saw the order for one of them. Both expired.

    The row is for more than the books know of, in a contract no bot ever
    traded. That is the owner's own holding and no bot's.
    """
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
    from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture

    repo = books_at(tmp_path, THE_DAY_BEFORE, {"option-bot": "AAPL"})
    holds_option(repo, "option-bot", CALL, contracts=1)
    observe_external_order(
        repo,
        order=_broker_order_fixture(
            "by-hand-1", order_id="alpaca-by-hand-1", symbol=HAND_TRADED, status="filled", side="buy", quantity=1,
            filled_quantity=1, filled_avg_price=1.5,
        ),
    )
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(symbol=HAND_TRADED, qty="-2"))

        assert option_holds(repo) == {} and bookings(repo) == []
    finally:
        repo.close()


async def test_an_assignment_of_a_contract_held_only_bought_is_held_as_the_wrong_side(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _assignment(date="2023-07-21"))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENT_WRONG_SIDE")}
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_an_expiry_of_a_contract_one_bot_bought_and_another_sold_holds_both(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", CALL, contracts=-1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry())

        assert option_holds(repo).keys() == {
            ("bot-a", "OPTION_EVENT_HELD_ON_BOTH_SIDES"), ("bot-b", "OPTION_EVENT_HELD_ON_BOTH_SIDES"),
        }
        assert bookings(repo) == []
    finally:
        repo.close()


async def test_an_event_for_more_contracts_than_are_held_takes_none(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-2"))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENT_MORE_CONTRACTS_THAN_HELD")}
        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_cash_that_is_not_whole_cents_is_held_for_and_never_rounded(tmp_path: Path) -> None:
    """One share a contract at a strike of $150.005: the exercise costs $150.005."""
    odd = "AAPL230721C00150005"
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", odd, contracts=1, multiplier=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(symbol=odd))

        assert ("bot-a", "OPTION_EVENT_CASH_NOT_WHOLE_CENTS") in option_holds(repo)
        assert bookings(repo) == [] and held(repo, "bot-a") == {odd: 1.0}
    finally:
        repo.close()


async def test_an_exercise_whose_share_trade_was_not_reported_is_booked_and_held_until_it_is(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise())

        assert held(repo, "bot-a") == {"AAPL": 100.0}
        assert ("bot-a", "OPTION_SHARE_TRADE_MISSING") in option_holds(repo)

        await _read(repo, account, _exercise(), _share_trade(100))

        assert ("bot-a", "OPTION_SHARE_TRADE_MISSING") not in option_holds(repo)
        assert len(bookings(repo)) == 1
    finally:
        repo.close()


async def test_a_share_trade_that_belongs_to_no_event_read_is_held_for(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _share_trade(100))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_SHARE_TRADE_UNPAIRED")}
        assert held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_a_share_trade_that_differs_from_the_contracts_terms_is_named_and_the_books_keep_the_strike(
    tmp_path: Path,
) -> None:
    """Alpaca's own example prints a price of 90 on a strike of 150. The books say 150 and name the 90."""
    repo = _one_bot(tmp_path, contracts=2)
    account = _FakeReadPort()
    rows = [documented_option_activity("trading_api", "option_exercise", index) for index in (0, 1)]
    try:
        await _read(repo, account, *rows)

        [difference] = _cause(repo, "bot-a", "OPTION_SHARE_TRADE_DISAGREES")
        assert "90" in difference["detail"] and "150" in difference["detail"]
        assert held(repo, "bot-a") == {"AAPL": 200.0}
        booked = json.loads(bookings(repo)[0]["facts_json"])
        assert booked["shares"]["price"] == "150" and booked["share_trade"]["agrees"] is False
    finally:
        repo.close()


async def test_an_expiry_signed_for_the_other_side_closes_the_only_side_held_and_is_held_for(tmp_path: Path) -> None:
    """A sold option's expiry signed as a bought one's. The lot is closed, and the owner is told the sign."""
    repo = _one_bot(tmp_path, contracts=-1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-1"))

        assert held(repo, "bot-a") == {}
        assert option_holds(repo).keys() == {("bot-a", "OPTION_EXPIRY_SIGNED_FOR_OTHER_SIDE")}
    finally:
        repo.close()


async def test_an_assignment_read_after_the_oldest_lot_was_closed_falls_on_the_next_and_says_so(tmp_path: Path) -> None:
    """Two bots sold one call each. One is assigned on the 30th. On the 3rd, before the row is read, the
    bot that sold first buys its call back. The assignment is booked to the other, the books equal the
    broker's, and that bot is told the first one's close came in between."""
    repo = books_at(tmp_path, date(2023, 6, 29), {"first": "AAPL", "second": "AAPL"})
    holds_option(repo, "first", CALL, contracts=-1, price=3.07)
    holds_option(repo, "second", CALL, contracts=-1, price=2.80)
    later(repo, 4 * 86_400_000)
    holds_option(repo, "first", CALL, contracts=1, price=1.20)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _assignment(date="2023-06-30"), _share_trade(-100, date="2023-06-30"))

        assert held(repo, "first") == {} and held(repo, "second") == {"AAPL": -100.0}
        [said] = _cause(repo, "second", "OPTION_EVENT_AFTER_A_LOT_CLOSED")
        assert "first" in said["detail"]
        assert ("first", "OPTION_EVENT_AFTER_A_LOT_CLOSED") not in option_holds(repo)
    finally:
        repo.close()


async def test_a_lot_still_open_after_its_contract_expired_is_set_against_the_brokers_position(tmp_path: Path) -> None:
    """The broker holds none of the expired contract, the books still do, and no row explains it: held.
    While the broker still shows the position there is nothing to say yet. The row, when it comes, ends it."""
    repo = _one_bot(tmp_path)
    later(repo, 2 * 86_400_000)
    still_at_the_broker = _FakeReadPort(positions=broker_holds(**{CALL: 1}))
    gone_at_the_broker = _FakeReadPort()
    try:
        await _read(repo, still_at_the_broker)
        assert option_holds(repo) == {}

        await _read(repo, gone_at_the_broker)
        [said] = _cause(repo, "bot-a", "OPTION_EXPIRED_NOT_SETTLED")
        assert said["ref"] == f"contract:{CALL}"
        assert _entry_refused_for(repo, "bot-a") == "OPTION_EXPIRED_NOT_SETTLED"

        await _read(repo, gone_at_the_broker, _expiry())
        assert option_holds(repo) == {} and held(repo, "bot-a") == {}
    finally:
        repo.close()


@pytest.mark.parametrize(
    "at_the_broker",
    [
        pytest.param({}, id="the broker shows nothing, as a contract held both ways nets to"),
        pytest.param({CALL: 1}, id="the broker shows another net than the books"),
    ],
)
async def test_a_contract_held_on_both_sides_that_gets_no_row_is_held_for_once_it_has_expired(
    tmp_path: Path, at_the_broker: dict[str, float]
) -> None:
    """Bought by one bot and sold by another, it nets to nothing at the broker and may never get a row.

    The broker showing nothing is then no agreement: both lots are still
    open on the books. Each holder is held by name, and the bot that sold
    is told its side is not ended at a word.
    """
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", CALL, contracts=-1)
    later(repo, 2 * 86_400_000)
    account = _FakeReadPort(positions=broker_holds(**at_the_broker))
    try:
        await _read(repo, account)

        assert option_holds(repo).keys() == {
            ("bot-a", "OPTION_EXPIRED_NOT_SETTLED"), ("bot-b", "OPTION_EXPIRED_NOT_SETTLED"),
        }
        [bought], [sold] = (_cause(repo, bot, "OPTION_EXPIRED_NOT_SETTLED") for bot in ("bot-a", "bot-b"))
        assert "+1 for this bot" in bought["detail"] and "-1 for this bot" in sold["detail"]
        assert "sold" in sold["detail"] and "not ended at the owner's word" in sold["detail"]
        assert "not ended at the owner's word" not in bought["detail"]
    finally:
        repo.close()


async def test_while_the_broker_still_shows_an_expired_contract_as_the_books_hold_it_nobody_is_held(
    tmp_path: Path,
) -> None:
    """Two bots bought it. The broker still shows two: its record of the end is yet to come, and nothing differs."""
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", CALL, contracts=1)
    later(repo, 2 * 86_400_000)
    try:
        await _read(repo, _FakeReadPort(positions=broker_holds(**{CALL: 2})))

        assert option_holds(repo) == {}
    finally:
        repo.close()


async def test_a_read_that_failed_holds_the_bots_that_hold_options_and_ends_nothing(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-0.5"))
        account.option_events.error = BrokerUnavailable("no answer", broker="alpaca")
        await _read(repo, account)

        # The unreadable row was not read this time. That is not the same as its being gone.
        assert option_holds(repo).keys() == {
            ("bot-a", "OPTION_EVENT_UNREADABLE"), ("bot-a", "OPTION_EVENTS_NOT_READ"),
        }
        assert _entry_refused_for(repo, "bot-b") is None

        account.option_events.error = None
        await _read(repo, account)
        assert option_holds(repo) == {}
    finally:
        repo.close()


async def test_a_read_that_did_not_reach_the_windows_start_is_never_read_as_nothing_happened(tmp_path: Path) -> None:
    """The contract has expired and the broker holds none of it. A walk cut short proves nothing about that:
    the row may be in the part that was not read."""
    repo = _one_bot(tmp_path)
    later(repo, 2 * 86_400_000)
    account = _FakeReadPort()
    account.option_events.cut_short = True
    try:
        await _read(repo, account)

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENTS_NOT_READ")}
        assert held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_a_walk_that_never_ends_is_stopped_and_is_not_whole(tmp_path: Path) -> None:
    """A broker that always says there is another page is followed a bounded number of times."""
    repo = _one_bot(tmp_path)
    asked: list[BrokerOptionEventResume | None] = []

    class _AlwaysAnotherPage(_FakeReadPort):
        async def read_option_event_activities(
            self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
        ) -> BrokerOptionEventEvidence:
            asked.append(resume)
            return BrokerOptionEventEvidence(
                events=(), share_trades=(), history_complete=False,
                resume=BrokerOptionEventResume(page_token=f"page-{len(asked)}", last_page_oldest_ms=after_ms),
            )

    try:
        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        await poll(repo, _AlwaysAnotherPage(), asked_for=True)

        assert len(asked) == option_events.MAX_OPTION_EVENT_PAGES
        # Each read was handed the place the one before it stopped at.
        assert asked[0] is None and [resume.page_token for resume in asked[1:3]] == ["page-1", "page-2"]
        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENTS_NOT_READ")}
    finally:
        repo.close()


async def test_a_row_that_changes_under_an_id_already_booked_is_held_for_and_the_booking_stands(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path, contracts=2)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-1"))
        await _read(repo, account, _expiry(qty="-2"))

        assert ("bot-a", "OPTION_EVENT_ROW_CHANGED") in option_holds(repo)
        assert len(bookings(repo)) == 1 and held(repo, "bot-a") == {CALL: 1.0}
    finally:
        repo.close()


async def test_an_exercise_whose_shares_are_written_under_another_symbol_than_the_bots_is_not_booked(
    tmp_path: Path,
) -> None:
    """A share-class ticker: the contract's root drops the dot the bot's symbol has. It is not matched back."""
    berkshire = "BRKB230721C00150000"
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "BRK.B"})
    holds_option(repo, "bot-a", berkshire, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(symbol=berkshire))

        assert ("bot-a", "OPTION_EVENT_SHARES_IN_ANOTHER_SYMBOL") in option_holds(repo)
        assert bookings(repo) == [] and held(repo, "bot-a") == {berkshire: 1.0}
    finally:
        repo.close()


async def test_a_bot_left_holding_shares_takes_no_entry_until_it_holds_none(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(), _share_trade(100))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_SHARES_HELD")}
        assert _entry_refused_for(repo, "bot-a") == "OPTION_SHARES_HELD" and _may_reduce(repo, "bot-a", "AAPL", 100)
    finally:
        repo.close()


# ── the owner's acknowledgement ──────────────────────────────────────────────


async def test_an_acknowledged_row_is_not_held_for_again_and_a_new_row_is(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-0.5"))

        assert acknowledge_option_event_hold(
            repo, strategy_instance_id="bot-a", reason_code="OPTION_EVENT_UNREADABLE", operator="owner"
        )
        await _read(repo, account, _expiry(qty="-0.5"))
        assert option_holds(repo) == {}
        assert _entry_refused_for(repo, "bot-a") != "OPTION_EVENT_UNREADABLE"

        await _read(repo, account, _expiry(qty="-0.5"), _expiry(qty="-0.5", id="exp-2"))
        [row] = _cause(repo, "bot-a", "OPTION_EVENT_UNREADABLE")
        assert row["ref"] == "option-activity:exp-2"
    finally:
        repo.close()


async def test_an_acknowledgement_covers_what_was_wrong_then_and_a_new_fault_in_the_same_row_is_raised(
    tmp_path: Path,
) -> None:
    """The owner looked at a row that read half a contract. The same id now reads as a corporate action.

    That is something they have not seen. It is held for again.
    """
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-0.5"))
        [first] = _cause(repo, "bot-a", "OPTION_EVENT_UNREADABLE")
        assert acknowledge_option_event_hold(
            repo, strategy_instance_id="bot-a", reason_code="OPTION_EVENT_UNREADABLE", operator="owner"
        )

        await _read(repo, account, _expiry(activity_type="OPCA"))

        [again] = _cause(repo, "bot-a", "OPTION_EVENT_UNREADABLE")
        assert again["ref"] == first["ref"] and again["detail"] != first["detail"]
    finally:
        repo.close()


async def test_a_hold_that_ends_only_by_proof_cannot_be_acknowledged_away(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(), _share_trade(100))

        with pytest.raises(ValueError, match="proof"):
            acknowledge_option_event_hold(
                repo, strategy_instance_id="bot-a", reason_code="OPTION_SHARES_HELD", operator="owner"
            )
        assert ("bot-a", "OPTION_SHARES_HELD") in option_holds(repo)
        assert not acknowledge_option_event_hold(
            repo, strategy_instance_id="bot-b", reason_code="OPTION_EVENT_UNREADABLE", operator="owner"
        )
    finally:
        repo.close()


# ── what a hold is not raised for, and what a read asks for ─────────────────


async def test_a_walk_cut_short_proves_nothing_even_when_the_pass_hands_over_the_brokers_positions(
    tmp_path: Path,
) -> None:
    """The pass knows the broker holds none of the expired contract. The row may still be in what was not read."""
    repo = _one_bot(tmp_path)
    later(repo, 2 * 86_400_000)
    account = _FakeReadPort()
    account.option_events.cut_short = True
    try:
        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        await poll(repo, account, broker_positions=[])

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENTS_NOT_READ")}
    finally:
        repo.close()


async def test_a_share_trade_of_another_day_is_not_taken_for_an_exercises_own(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(date="2023-07-21"), _share_trade(100, date="2023-07-20"))

        assert held(repo, "bot-a") == {"AAPL": 100.0}
        assert {reason for _bot, reason in option_holds(repo)} == {
            "OPTION_SHARES_HELD", "OPTION_SHARE_TRADE_MISSING", "OPTION_SHARE_TRADE_UNPAIRED",
        }
    finally:
        repo.close()


async def test_a_bot_that_closed_an_older_lot_of_its_own_before_the_row_was_read_is_told_nothing_of_it(
    tmp_path: Path,
) -> None:
    """Inside one bot's books it does not matter which of its lots an event took: nothing is in doubt."""
    repo = books_at(tmp_path, date(2023, 6, 29), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=-1, price=3.07)
    holds_option(repo, "bot-a", CALL, contracts=-1, price=2.80)
    later(repo, 4 * 86_400_000)
    holds_option(repo, "bot-a", CALL, contracts=1, price=1.20)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _assignment(date="2023-06-30"), _share_trade(-100, date="2023-06-30"))

        assert held(repo, "bot-a") == {"AAPL": -100.0}
        assert option_holds(repo).keys() == {("bot-a", "OPTION_SHARES_HELD")}
    finally:
        repo.close()


def _the_owner_writes_off(repo: ClerkSqliteRepository, bot: str, symbol: str, contracts: float) -> None:
    """The owner's write-off of one bot's position the broker no longer holds, as the Clerk records it."""
    from app.broker.alpaca.clerk.sqlite.facts import AttributedResidueDischargedFacts
    from app.broker.alpaca.clerk.sqlite.models import TransitionInput

    repo.append_transition(TransitionInput(
        strategy_instance_id=bot, transition_kind="ATTRIBUTED_RESIDUE_DISCHARGED", custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK", operation_state="succeeded", clerk_observed_at_ms=repo.clock(),
        summary_code="ATTRIBUTED_RESIDUE_DISCHARGED",
        facts_json=AttributedResidueDischargedFacts(
            symbol=symbol, discharged_qty=contracts, broker_qty=0.0, account_attributed_qty=contracts,
            broker_observed_at_ms=repo.clock(), uncertainty_ids=[], operator_reason="expired, no row",
        ).to_facts_json(),
    ))
    assert held(repo, bot) == {}


async def test_a_position_the_owner_wrote_off_is_not_closed_a_second_time_by_a_row_that_comes_later(
    tmp_path: Path,
) -> None:
    """The broker held none of the expired contract and no row had come, so the owner wrote the position off.

    The expiry's row comes the next morning. The write-off already took the
    contract off the books and the row moves nothing more: nothing is
    booked, and nobody is held for a row that only confirms it.
    """
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    written = repo.last_custody_sequence()
    try:
        await _read(repo, account, _expiry())

        assert bookings(repo) == [] and held(repo, "bot-a") == {}
        assert option_holds(repo) == {}
        assert repo.last_custody_sequence() == written
    finally:
        repo.close()


async def test_an_exercise_read_after_its_position_was_written_off_is_not_booked_and_holds_that_bot_by_name(
    tmp_path: Path,
) -> None:
    """The contract the owner wrote off as expired was exercised: the account now holds 100 shares.

    Booking the exercise would close a lot the write-off already took off
    the books. So nothing is booked. The shares are real and on no bot's
    books, and the bot that held the contract is told so by name. No other bot is held.
    """
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(), _share_trade(100))

        assert bookings(repo) == [] and held(repo, "bot-a") == {}
        assert ("bot-a", "OPTION_EVENT_CONTRACT_NOT_HELD") in option_holds(repo)
        assert not any(bot is None or bot == "bot-b" for bot, _reason in option_holds(repo))
    finally:
        repo.close()


async def test_a_write_off_of_one_contract_does_not_explain_an_expiry_of_two(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-2"))

        assert bookings(repo) == []
        assert (None, "OPTION_EVENT_CONTRACT_NOT_HELD") in option_holds(repo)
    finally:
        repo.close()


async def test_a_write_off_of_another_contract_does_not_explain_an_expiry_of_this_one(tmp_path: Path) -> None:
    """The bot bought this call and sold it. It wrote off a different one. A row for this one is nobody's."""
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-a", CALL, contracts=-1)
    holds_option(repo, "bot-a", OTHER_CALL, contracts=1)
    holds_option(repo, "bot-b", "AAPL230915C00150000", contracts=1)
    _the_owner_writes_off(repo, "bot-a", OTHER_CALL, 1.0)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry())

        assert bookings(repo) == []
        assert (None, "OPTION_EVENT_CONTRACT_NOT_HELD") in option_holds(repo)
    finally:
        repo.close()


async def test_a_write_off_of_a_bought_contract_does_not_explain_an_assignment_of_it(tmp_path: Path) -> None:
    """An assignment ends a sold position. The bot wrote off a bought one, so the row is still nobody's."""
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _assignment(date="2023-07-21"), _share_trade(-100))

        assert bookings(repo) == []
        assert (None, "OPTION_EVENT_CONTRACT_NOT_HELD") in option_holds(repo)
    finally:
        repo.close()


async def test_a_read_starts_on_the_date_of_the_first_fill_in_a_contract_still_held_however_long_ago(
    tmp_path: Path,
) -> None:
    from app.utils.session_anchors import et_midnight_ms

    repo = _one_bot(tmp_path)
    later(repo, 9 * 86_400_000)
    account = _FakeReadPort()
    try:
        await _read(repo, account)

        assert account.option_events.reads == [et_midnight_ms(THE_DAY_BEFORE)]
    finally:
        repo.close()


async def test_a_read_goes_back_to_the_oldest_row_an_open_hold_names(tmp_path: Path) -> None:
    """The contract the bot holds was bought today. The row it is held for is dated weeks before."""
    from app.utils.session_anchors import et_midnight_ms

    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    unreadable = _expiry(symbol="MSFT230721C00300000", qty="-0.5", date="2023-07-21")
    try:
        await _read(repo, account, unreadable)
        assert (None, "OPTION_EVENT_UNREADABLE") in option_holds(repo)
        later(repo, 30 * 86_400_000)
        holds_option(repo, "bot-b", "AAPL230915C00150000", contracts=1)
        holds_option(repo, "bot-a", CALL, contracts=-1)

        await _read(repo, account, unreadable)

        assert account.option_events.reads[-1] == et_midnight_ms(date(2023, 7, 21))
        assert (None, "OPTION_EVENT_UNREADABLE") in option_holds(repo)
    finally:
        repo.close()


async def test_a_hold_that_still_stands_is_written_once_however_often_the_same_rows_are_read(tmp_path: Path) -> None:
    """While a hold stands the events are read every minute. A read that finds what the last one found writes nothing."""
    repo = _one_bot(tmp_path, contracts=2)
    account = _FakeReadPort()
    rows = [_expiry(qty="-0.5"), _exercise(qty="-1", id="exc-9")]
    try:
        await _read(repo, account, *rows)
        written = repo.last_custody_sequence()
        assert {reason for _bot, reason in option_holds(repo)} == {
            "OPTION_EVENT_UNREADABLE", "OPTION_SHARE_TRADE_MISSING", "OPTION_SHARES_HELD",
        }

        for _ in range(3):
            await _read(repo, account, *rows)

        assert repo.last_custody_sequence() == written
        assert len(account.option_events.reads) == 4
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("row", "booked"),
    [
        pytest.param("-1", 1, id="a row for the one it holds"),
        pytest.param("-2", 1, id="a row that also counts the one written off"),
    ],
)
async def test_a_bot_that_wrote_a_position_off_and_bought_the_contract_again_has_the_lot_it_holds_booked(
    tmp_path: Path, row: str, booked: int
) -> None:
    """A lot was written off in the contract's life. The same bot then bought the contract again.

    The write-off closed no lot, so the fills hold one more than the bot
    does. The older one is the written-off one. The lot it holds now is its
    own, and the expiry closes that: the bot is flat, as the broker is.
    What the row counts of the written-off one moves nothing more.
    """
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-a", CALL, contracts=1)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty=row))

        assert [(made["strategy_instance_id"], made["summary_code"]) for made in bookings(repo)] == [
            ("bot-a", "OPTION_EXPIRED")
        ] * booked
        assert held(repo, "bot-a") == {} and option_holds(repo) == {}
    finally:
        repo.close()


async def test_one_bots_write_off_does_not_stop_the_other_bots_expiry_from_being_booked(tmp_path: Path) -> None:
    """Two bots each bought one of the same call at $1.94. The owner wrote off bot-b's. The row is for both.

    Bot-a's lot is still on the books and the expiry closes it: $194.00
    lost. Bot-b's left the books at the owner's word, and the row moves
    nothing more for it. Before, the row was for more than the lots
    offered, so nothing was booked and bot-a's premium was never a loss.
    """
    from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import fifo

    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", CALL, contracts=1)
    _the_owner_writes_off(repo, "bot-b", CALL, 1.0)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-2"))
        written = repo.last_custody_sequence()
        await _read(repo, account, _expiry(qty="-2"))

        assert [(made["strategy_instance_id"], made["summary_code"]) for made in bookings(repo)] == [
            ("bot-a", "OPTION_EXPIRED")
        ]
        assert held(repo, "bot-a") == {} and fifo(repo, "bot-a").exact_realized_pnl == -194
        assert option_holds(repo) == {}
        assert repo.last_custody_sequence() == written
    finally:
        repo.close()


async def test_an_exercise_of_two_books_the_lot_still_held_and_holds_the_bot_that_wrote_its_own_off_by_name(
    tmp_path: Path,
) -> None:
    """Bot-a holds one call and bot-b wrote its one off. Both were exercised: the account holds 200 shares.

    Bot-a's 100 are booked at the strike. Bot-b's 100 are real and on no
    bot's books, and bot-b is told so by name, on this read and on the next.
    """
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", CALL, contracts=1)
    _the_owner_writes_off(repo, "bot-b", CALL, 1.0)
    account = _FakeReadPort()
    rows = [_exercise(qty="-2"), _share_trade(200)]
    try:
        for _ in range(2):
            await _read(repo, account, *rows)

            assert [(made["strategy_instance_id"], made["summary_code"]) for made in bookings(repo)] == [
                ("bot-a", "OPTION_EXERCISED")
            ]
            assert held(repo, "bot-a") == {"AAPL": 100.0} and held(repo, "bot-b") == {}
            assert option_holds(repo).keys() == {
                ("bot-a", "OPTION_SHARES_HELD"), ("bot-b", "OPTION_EVENT_CONTRACT_NOT_HELD"),
            }
            [said] = _cause(repo, "bot-b", "OPTION_EVENT_CONTRACT_NOT_HELD")
            assert "1 of" in said["detail"] and "on no bot's books" in said["detail"]
    finally:
        repo.close()


async def test_a_lot_that_was_written_off_never_takes_a_row_from_a_lot_still_on_the_books(tmp_path: Path) -> None:
    """Bot-b's lot is the newer one, and the owner wrote it off. The expiry is for one contract.

    An expiry takes the newest lot first, and the newest is the written-off
    one. But a lot on the books is what the broker's row is about: booking
    it is what leaves the books equal to the broker. So bot-a's is closed.
    """
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", CALL, contracts=1)
    _the_owner_writes_off(repo, "bot-b", CALL, 1.0)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry())

        assert [(made["strategy_instance_id"], made["summary_code"]) for made in bookings(repo)] == [
            ("bot-a", "OPTION_EXPIRED")
        ]
        assert held(repo, "bot-a") == {} and option_holds(repo) == {}
    finally:
        repo.close()


async def test_a_bot_whose_lots_add_up_to_neither_what_it_holds_nor_what_it_wrote_off_is_held_by_name(
    tmp_path: Path,
) -> None:
    """The bot wrote one bought contract off, then sold one. In the fills that sale closed the written-off lot.

    So the books hold the contract sold and the fills hold no lot of it at
    all. Which lot is the bot's own is not known, and an assignment cannot
    be matched to it. It is held by name, in words that are true of it.
    """
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-a", CALL, contracts=-1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _assignment(date="2023-07-21"), _share_trade(-100))

        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: -1.0}
        assert set(option_holds(repo)) == {("bot-a", "OPTION_EVENT_CONTRACT_NOT_HELD")}
        [said] = _cause(repo, "bot-a", "OPTION_EVENT_CONTRACT_NOT_HELD")
        assert "holds -1" in said["detail"] and "wrote off +1" in said["detail"]
        assert "cannot be matched" in said["detail"]
    finally:
        repo.close()


async def test_a_write_off_of_a_sold_contract_does_not_explain_an_exercise_of_it(tmp_path: Path) -> None:
    """An exercise ends a bought position. The bot wrote off a sold one, so the row is still nobody's."""
    repo = _one_bot(tmp_path, contracts=-1)
    _the_owner_writes_off(repo, "bot-a", CALL, -1.0)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(), _share_trade(100))

        assert bookings(repo) == []
        assert (None, "OPTION_EVENT_CONTRACT_NOT_HELD") in option_holds(repo)
        assert ("bot-a", "OPTION_EVENT_CONTRACT_NOT_HELD") not in option_holds(repo)
    finally:
        repo.close()


async def test_a_position_that_was_written_off_is_not_counted_for_two_rows(tmp_path: Path) -> None:
    """One contract was written off. Two expiries of one each are read: the second is nobody's."""
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(), _expiry(id="exp-2"))

        assert bookings(repo) == []
        [named] = _cause(repo, None, "OPTION_EVENT_CONTRACT_NOT_HELD")
        assert named["ref"] == "option-activity:exp-2"
    finally:
        repo.close()


async def test_what_a_booked_row_counted_of_a_written_off_position_is_not_counted_again_for_a_later_row(
    tmp_path: Path,
) -> None:
    """Bot-a wrote one off and bought one again. A row for two closed the one it held and counted the other.

    A second row for one more comes the next minute. The written-off
    contract was counted already, though the fills now show another lot
    open in its place. The second row is nobody's.
    """
    repo = _one_bot(tmp_path)
    _the_owner_writes_off(repo, "bot-a", CALL, 1.0)
    holds_option(repo, "bot-a", CALL, contracts=1)
    holds_option(repo, "bot-b", OTHER_CALL, contracts=1)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry(qty="-2"))
        assert len(bookings(repo)) == 1 and option_holds(repo) == {}

        await _read(repo, account, _expiry(qty="-2"), _expiry(id="exp-2"))

        assert len(bookings(repo)) == 1
        [named] = _cause(repo, None, "OPTION_EVENT_CONTRACT_NOT_HELD")
        assert named["ref"] == "option-activity:exp-2"
    finally:
        repo.close()


async def test_the_real_write_off_of_a_position_an_event_already_closed_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A booked expiry left the position at zero. The owner's write-off, through its own executor, finds
    nothing to write off: the loss stays the $194.00 the expiry booked, and the same row read once more
    books nothing more."""
    from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
    from app.broker.alpaca.clerk.sqlite.residue_discharge import ResidueDischargeRefused, discharge_attributed_residue
    from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import fifo

    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry())
        assert [row["summary_code"] for row in bookings(repo)] == ["OPTION_EXPIRED"]
        written = repo.last_custody_sequence()

        with pytest.raises(ResidueDischargeRefused) as refused:
            await discharge_attributed_residue(
                repo, read=account, intake=ReentrantAsyncLock(), strategy_instance_id="bot-a", symbol=CALL,
                operator_reason="expired",
            )
        await _read(repo, account, _expiry())

        assert refused.value.reason_code == "NO_ATTRIBUTED_RESIDUE"
        assert repo.last_custody_sequence() == written
        assert fifo(repo, "bot-a").exact_realized_pnl == -194
    finally:
        repo.close()
