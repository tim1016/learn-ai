"""What one round of reading and booking option events does at its edges (#2913).

A stop between a booking and its holds, two callers due at once, a read
that keeps failing, a clock set back, a share trade that is another event's,
and the Clerk's own retry beside an order it did not place. The books are
the Clerk's real ones and the rows are Alpaca's documented ones with a
contract, a date or a count laid over them. Nothing was recorded from Alpaca.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import exit_watchdog, option_events
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.exit_watchdog import BrokerSymbolView
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline, to_thread
from app.broker.alpaca.clerk.sqlite.option_event_books import outside_order_reduces_a_bots_contract
from app.broker.alpaca.clerk.sqlite.option_events import book_option_events, poll_option_events
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import admit_new_exposure
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import UNEXPLAINED_ORDER_HOLD_REASON_CODE
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _broker_order_fixture,
    _FakeReadPort,
    _FakeTradePort,
    _TestClock,
    documented_option_activity,
)
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import (
    bookings,
    books_at,
    broker_holds,
    fifo,
    held,
    holds_option,
    later,
    option_holds,
    poll,
)

CALL = "AAPL230721C00150000"
THE_DAY_BEFORE = date(2023, 7, 20)
MINUTE = option_events.OPTION_EVENT_POLL_RETRY_MS


def _expiry(**changes: Any) -> dict[str, Any]:
    return documented_option_activity("trading_api", "otm_option_expiry", **{"id": "exp-1", "qty": "-1", **changes})


def _exercise(**changes: Any) -> dict[str, Any]:
    return documented_option_activity("trading_api", "option_exercise", **{"id": "exc-1", "qty": "-1", **changes})


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
    account.option_events.rows = list(rows)
    later(repo, MINUTE)
    await poll(repo, account, asked_for=True)


# ── a stop between a booking and its holds ───────────────────────────────────


async def test_a_stop_between_a_booking_and_its_holds_loses_no_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exercise was booked and the process died before any hold was written. The bot holds 100 shares.

    It holds no option any more, so nothing else would bring a read. The
    next round finds the shares on the books and holds the bot for them,
    though it books nothing itself.
    """
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    account.option_events.rows = [_exercise(), _share_trade(100)]
    real = option_events._sync_holds

    def dies(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the process died after the booking was written")

    monkeypatch.setattr(option_events, "_sync_holds", dies)
    try:
        with pytest.raises(RuntimeError, match="died"):
            await poll(repo, account)
        assert held(repo, "bot-a") == {"AAPL": 100.0} and option_holds(repo) == {}
        monkeypatch.setattr(option_events, "_sync_holds", real)

        later(repo, MINUTE)
        done = await poll(repo, account)

        assert done.round is not None and done.round.booked == ()
        assert ("bot-a", "OPTION_SHARES_HELD") in option_holds(repo)
        assert admit_new_exposure(repo, strategy_instance_id="bot-a").reason_code == "OPTION_SHARES_HELD"
    finally:
        repo.close()


# ── two callers, one read ────────────────────────────────────────────────────


async def test_a_caller_that_finds_a_read_under_way_neither_waits_behind_it_nor_reads_again(tmp_path: Path) -> None:
    """After a restart the keeper and the pass both find a read due. The keeper's is out at Alpaca.

    The pass does not wait for that walk to end, and does not then walk the
    account's activities a second time. One read, one booking.
    """
    repo = _one_bot(tmp_path, contracts=2)
    later(repo, 2 * 86_400_000)
    account = _FakeReadPort()
    account.option_events.rows = [_expiry(qty="-2")]
    answered, asked = asyncio.Event(), 0
    real_read = account.option_events.read_option_event_activities
    real_work = option_events.option_work_on_the_books
    both_have_asked_the_books = asyncio.Event()
    waiting = 0

    async def slow(**asked_for: Any) -> Any:
        nonlocal asked
        asked += 1
        await answered.wait()
        return await real_read(**asked_for)

    async def off_loop(work: Any) -> Any:
        # Both callers are past their first look at the cadence before either goes on.
        nonlocal waiting
        result = await to_thread(work)
        waiting += 1
        if waiting == 2:
            both_have_asked_the_books.set()
        if waiting <= 2:
            await both_have_asked_the_books.wait()
        return result

    account.option_events.read_option_event_activities = slow  # type: ignore[method-assign]
    assert real_work(repo)
    try:
        as_the_keeper = asyncio.create_task(poll_option_events(repo, read=account, off_loop=off_loop))
        as_the_pass = asyncio.create_task(poll_option_events(repo, read=account, off_loop=off_loop))

        second = await asyncio.wait_for(asyncio.wait({as_the_keeper, as_the_pass}, return_when=asyncio.FIRST_COMPLETED), 5)
        [finished] = second[0]
        assert finished.result().read is False, "it came back while the other's read was still out"
        answered.set()
        first = await asyncio.wait_for(next(iter(second[1])), 5)

        assert first.read is True and asked == 1
        assert len(bookings(repo)) == 1 and held(repo, "bot-a") == {}
    finally:
        answered.set()
        repo.close()


async def test_the_keepers_stop_ends_the_read_it_started(tmp_path: Path) -> None:
    """A keeper that has stopped books nothing more: its read is ended with it, not left to finish by itself."""
    repo = _one_bot(tmp_path)
    later(repo, 2 * 86_400_000)
    account = _FakeReadPort()
    account.option_events.rows = [_expiry()]
    answered, asked = asyncio.Event(), 0
    real_read = account.option_events.read_option_event_activities

    async def slow(**asked_for: Any) -> Any:
        nonlocal asked
        asked += 1
        await answered.wait()
        return await real_read(**asked_for)

    account.option_events.read_option_event_activities = slow  # type: ignore[method-assign]
    keeper = DeadlineKeeper(repo=repo, trade=_FakeTradePort(), read=account, off_loop=run_inline)
    try:
        await keeper.tick()
        await asyncio.sleep(0)
        assert asked == 1

        await keeper.stop()
        answered.set()
        for _ in range(5):
            await asyncio.sleep(0)

        assert bookings(repo) == [] and held(repo, "bot-a") == {CALL: 1.0}
        assert await keeper.option_events_read() is None
    finally:
        answered.set()
        repo.close()


# ── a read that fails ────────────────────────────────────────────────────────


async def test_a_read_that_fails_in_other_words_each_minute_writes_its_hold_once(tmp_path: Path) -> None:
    """Alpaca's errors carry a request id that changes every time. The hold says the read failed, once.

    The error itself is in the Clerk's log. Written into the hold it would
    be a new record every minute, for every bot that holds an option.
    """
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        account.option_events.error = BrokerUnavailable("503 from Alpaca, request id req-0", broker="alpaca")
        later(repo, MINUTE)
        await poll(repo, account)
        written = repo.last_custody_sequence()
        assert ("bot-a", "OPTION_EVENTS_NOT_READ") in option_holds(repo)

        for minute in range(1, 6):
            account.option_events.error = BrokerUnavailable(f"503 from Alpaca, request id req-{minute}", broker="alpaca")
            later(repo, MINUTE)
            await poll(repo, account)

        assert len(account.option_events.reads) == 6 and repo.last_custody_sequence() == written
        assert "req-" not in option_holds(repo)[("bot-a", "OPTION_EVENTS_NOT_READ")]["facts_json"]
    finally:
        repo.close()


async def test_a_row_the_adapter_cannot_take_at_all_is_a_failed_read_and_is_held_for(tmp_path: Path) -> None:
    """A fault raised from inside the read is no reason to stop reading: it is a read that was not whole."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    account.option_events.error = ValueError("a page that is not a list of activities")
    try:
        later(repo, MINUTE)
        done = await poll(repo, account)

        assert done.read and done.round is not None and not done.round.read_whole
        assert ("bot-a", "OPTION_EVENTS_NOT_READ") in option_holds(repo)
    finally:
        repo.close()


class _TwoPagesTheLastNotWhole(_FakeReadPort):
    """The rows come on the first page. The walk then stops before the window's start."""

    async def read_option_event_activities(self, *, after_ms: int, resume: BrokerOptionEventResume | None = None) -> Any:
        evidence: BrokerOptionEventEvidence = await self.option_events.read_option_event_activities(after_ms=after_ms)
        if resume is None:
            return evidence.model_copy(update={
                "history_complete": False,
                "resume": BrokerOptionEventResume(page_token="page-2", last_page_oldest_ms=after_ms),
            })
        return evidence.model_copy(update={"events": (), "share_trades": (), "unreadable": (), "history_complete": False})


async def test_a_bot_the_round_has_just_emptied_is_not_held_for_the_rest_of_a_read_that_was_not_whole(
    tmp_path: Path,
) -> None:
    """The walk was cut short, and what it did read ended bot-a's position. Bot-b still holds a contract.

    Bot-b may be missing a row and is held. Bot-a holds no option any more:
    nothing the unread part could hold would be its own.
    """
    repo = _one_bot(tmp_path)
    holds_option(repo, "bot-b", "AAPL230721C00160000", contracts=1)
    later(repo, 2 * 86_400_000)
    account = _TwoPagesTheLastNotWhole()
    account.option_events.rows = [_expiry()]
    try:
        await poll(repo, account)

        assert held(repo, "bot-a") == {} and len(bookings(repo)) == 1
        assert option_holds(repo).keys() == {("bot-b", "OPTION_EVENTS_NOT_READ")}
    finally:
        repo.close()


async def test_a_store_fault_in_the_passes_option_step_is_the_passes_own_to_meet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other fault in the step is logged and the pass goes on. A fault of the books themselves is not hidden."""
    repo = _one_bot(tmp_path)

    async def the_store_fails(*_args: Any, **_kwargs: Any) -> Any:
        raise ClerkSqliteError("the books could not be written")

    monkeypatch.setattr(option_events, "poll_option_events", the_store_fails)
    try:
        with pytest.raises(ClerkSqliteError):
            await option_events.poll_option_events_in_a_pass(
                repo, read=_FakeReadPort(), simulation=None, off_loop=run_inline, broker_positions=[]
            )
    finally:
        repo.close()


# ── when a read is made ──────────────────────────────────────────────────────


async def test_a_clock_that_was_set_back_does_not_stop_the_reads(tmp_path: Path) -> None:
    """The last read is stamped later than now. How long ago it was cannot be told, so the events are read."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    try:
        later(repo, 3_600_000)
        assert (await poll(repo, account)).read
        clock.value -= 3_600_000
        repo.revive_execution_lease()

        again = await poll(repo, account)

        assert again.read and len(account.option_events.reads) == 2
    finally:
        repo.close()


async def test_a_position_that_differs_brings_one_read_a_minute_and_not_one_a_pass(tmp_path: Path) -> None:
    """The pass runs every fifteen seconds. While the broker's position differs each pass asks for a read."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        assert (await poll(repo, account, broker_positions=broker_holds(**{CALL: 1}))).read
        for _pass in range(3):
            later(repo, 15_000)
            await poll(repo, account, broker_positions=[])
        assert len(account.option_events.reads) == 1, "a read was made a moment ago: the difference waits its minute"

        later(repo, 15_000)
        await poll(repo, account, broker_positions=[])
        assert len(account.option_events.reads) == 2, "a minute after the last read, not the five of the quiet cadence"
    finally:
        repo.close()


async def test_a_contract_that_still_trades_is_not_held_for_as_expired_whatever_the_broker_shows(tmp_path: Path) -> None:
    """The broker shows none of a contract with a session still to go. That is the pass's drift, not an expiry."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await poll(repo, account, broker_positions=[])

        assert option_holds(repo) == {}
    finally:
        repo.close()


# ── which row is whose ───────────────────────────────────────────────────────


async def test_a_share_trade_the_broker_groups_with_another_event_is_not_this_events(tmp_path: Path) -> None:
    """The exercise and the share trade agree in every figure, and the broker's own link says they are not one."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(group_id="group-1"), _share_trade(100, group_id="group-2"))

        assert held(repo, "bot-a") == {"AAPL": 100.0}
        assert {reason for _bot, reason in option_holds(repo)} == {
            "OPTION_SHARES_HELD", "OPTION_SHARE_TRADE_MISSING", "OPTION_SHARE_TRADE_UNPAIRED",
        }
    finally:
        repo.close()


async def test_a_share_trade_the_broker_does_not_report_as_executed_is_held_for(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(), _share_trade(100, status="canceled"))

        [named] = json.loads(option_holds(repo)[("bot-a", "OPTION_EVENT_NOT_EXECUTED")]["facts_json"])["cause_facts"]["items"]
        assert named["ref"] == "share-trade:trd-1" and "canceled" in named["detail"]
    finally:
        repo.close()


async def test_a_row_that_cannot_be_read_and_names_an_underlying_holds_the_bots_under_it_and_not_the_account(
    tmp_path: Path,
) -> None:
    """A share trade for half a share of AAPL. Bot-a holds an AAPL contract. Bot-b holds nothing."""
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _share_trade(100, qty="0.5"))

        assert option_holds(repo).keys() == {("bot-a", "OPTION_EVENT_UNREADABLE")}
    finally:
        repo.close()


async def test_an_exercise_that_reads_another_date_under_an_id_already_booked_is_held_for(tmp_path: Path) -> None:
    repo = _one_bot(tmp_path)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _exercise(), _share_trade(100))
        await _read(repo, account, _exercise(date="2023-07-20"), _share_trade(100, date="2023-07-20"))

        assert ("bot-a", "OPTION_EVENT_ROW_CHANGED") in option_holds(repo)
        assert len(bookings(repo)) == 1 and held(repo, "bot-a") == {"AAPL": 100.0}
    finally:
        repo.close()


async def test_a_lot_another_bot_closed_on_the_events_own_date_is_not_one_closed_after_it(tmp_path: Path) -> None:
    """Two bots sold the call. The first bought its own back on the day the other was assigned.

    A close on the event's own date came before the event took effect at
    the close: the assignment could not have been that lot's. Nobody is
    told it might have been.
    """
    repo = books_at(tmp_path, date(2023, 6, 29), {"first": "AAPL", "second": "AAPL"})
    holds_option(repo, "first", CALL, contracts=-1, price=3.07)
    holds_option(repo, "second", CALL, contracts=-1, price=2.80)
    later(repo, 86_400_000)
    holds_option(repo, "first", CALL, contracts=1, price=1.20)
    account = _FakeReadPort()
    assignment = documented_option_activity(
        "trading_api", "option_assignment", id="asn-1", qty="1", date="2023-06-30"
    )
    try:
        await _read(repo, account, assignment, _share_trade(-100, date="2023-06-30"))

        assert held(repo, "second") == {"AAPL": -100.0}
        assert option_holds(repo).keys() == {("second", "OPTION_SHARES_HELD")}
    finally:
        repo.close()


# ── when the books count a booking as having happened ───────────────────────


async def test_an_event_is_never_counted_before_the_lot_it_takes_was_opened(tmp_path: Path) -> None:
    """The lot was bought on the expiry date, after the close its expiry is counted at.

    Counted before the purchase, the close would open a short for the
    purchase to cover: the same money, with the lot turned round. So the
    booking is counted no earlier than the lot it closes.
    """
    repo = books_at(tmp_path, date(2023, 7, 21), {"bot-a": "AAPL"})
    later(repo, 5 * 3_600_000)
    bought_at = repo.clock()
    holds_option(repo, "bot-a", CALL, contracts=1, price=1.94)
    account = _FakeReadPort()
    try:
        await _read(repo, account, _expiry())

        booked = json.loads(bookings(repo)[0]["facts_json"])
        assert booked["booked_as_of_ms"] >= bought_at
        [closed] = fifo(repo, "bot-a").closed_lots
        assert (closed.entry_price, closed.exit_price) == (1.94, 0.0)
    finally:
        repo.close()


def test_a_sale_outside_is_never_counted_before_the_lot_it_takes_was_opened(tmp_path: Path) -> None:
    """The broker stamps the order earlier than the bot's purchase. The sale still closes the lot it sold."""
    call = "SPY260918C00585000"
    repo = books_at(tmp_path, date(2026, 9, 8), {"bot-a": "SPY"})
    bought_at = repo.clock()
    holds_option(repo, "bot-a", call, contracts=1, price=1.94)
    try:
        # The fixture's order is stamped in 2023.
        observe_external_order(repo, order=_broker_order_fixture(
            "not-ours-1", order_id="alpaca-sells-1", symbol=call, status="filled", side="sell", quantity=1,
            filled_quantity=1, filled_avg_price=0.40,
        ))
        book_option_events(repo, None)

        booked = json.loads(bookings(repo)[0]["facts_json"])
        assert booked["booked_as_of_ms"] >= bought_at
        [closed] = fifo(repo, "bot-a").closed_lots
        assert (closed.entry_price, closed.exit_price) == (1.94, 0.40)
    finally:
        repo.close()


# ── an order the Clerk did not place, after its sale is booked ───────────────


def test_a_booked_sale_stays_its_bots_and_never_goes_back_on_the_accounts_hold(tmp_path: Path) -> None:
    """The bot holds nothing after the sale. The order is still the one that sold its contract.

    Seen once more by the stream, it is not an unexplained order that
    stops every bot of the account.
    """
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)

    def the_sale(**changes: Any) -> Any:
        return _broker_order_fixture(
            "not-ours-1", order_id="alpaca-sells-1", symbol=CALL, status="filled", side="sell", quantity=1,
            filled_quantity=1, filled_avg_price=0.40,
        ).model_copy(update=changes)

    try:
        observe_external_order(repo, order=the_sale())
        book_option_events(repo, None)
        assert held(repo, "bot-a") == {}

        observe_external_order(repo, order=the_sale(updated_at_ms=1_700_000_009_000, observed_at_ms=1_700_000_009_000))
        book_option_events(repo, None)

        with repo.write_fence() as conn:
            assert outside_order_reduces_a_bots_contract(
                conn, symbol=CALL, side="SELL", broker_order_id="alpaca-sells-1"
            )
        assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE) is None
        assert option_holds(repo) == {} and len(bookings(repo)) == 1
    finally:
        repo.close()


# ── the Clerk's own retry of a close, beside an order it did not place ──────


def _retry(repo: ClerkSqliteRepository, broker: BrokerSymbolView) -> Any:
    """The watchdog's retry of bot-a's close of its one call, as the pass asks for it."""
    return exit_watchdog._accept_admissible_redrive(
        repo, broker_symbol=lambda _symbol: broker, strategy_instance_id="bot-a", symbol=CALL,
        decision_id="redrive-1", entry_order_ref="unused", confirmed_shape=None,
    )


def test_the_clerks_retry_of_a_close_beside_an_outside_order_is_a_wait_and_spends_no_time_toward_a_stuck_exit(
    tmp_path: Path,
) -> None:
    """Alpaca is selling the bot's contract. The Clerk's retry of the bot's own close waits for that sale.

    A wait is no failure. Counted as one, an order that works all day would
    have the bot told as stuck, for a close that is in fact under way.
    """
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=1)
    working_at_the_broker = BrokerSymbolView(broker_qty=1.0, attributed_qty=1.0, working=True, agrees=False)
    try:
        without_it = _retry(repo, working_at_the_broker)
        observe_external_order(repo, order=_broker_order_fixture(
            "not-ours-1", order_id="alpaca-sells-1", symbol=CALL, status="new", side="sell", quantity=1,
        ))

        beside_it = _retry(repo, working_at_the_broker)

        # Any other order working on the symbol is still the failure it always was.
        assert (without_it.outcome, without_it.reason_code) == ("failure", "EXIT_OTHER_ORDER_WORKING")
        assert (beside_it.outcome, beside_it.reason_code) == ("hold", "OPTION_ORDER_NOT_OURS")
        assert "alpaca-sells-1" in (beside_it.explanation or "")
    finally:
        repo.close()
