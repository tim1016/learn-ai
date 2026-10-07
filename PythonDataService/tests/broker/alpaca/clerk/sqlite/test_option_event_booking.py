"""An assignment, an exercise and an expiry are read from Alpaca's activities and booked for the bot (#2913).

The books are the Clerk's real ones and the rows are the ones Alpaca's pages
document, read by the real port and adapter. No account has been assigned,
has exercised or has let an option expire under this code: nothing here was
recorded from Alpaca.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.option_event_folds import OPTION_POSITION_ENDED
from app.broker.alpaca.clerk.sqlite.option_events import OUTSIDE_THE_BOOKS, OptionEventRead, book_option_events
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.lean_sidecar.trading_calendar import session_window_for_date
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture, _FakeReadPort, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import ACCOUNT_ID, option_fold_registry
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
    share_cash,
)
from tests.fixtures.golden_support.registry import default as registry

FIXTURE_ID = "OEV-001"
_CASES = {case["case"]: case for case in json.loads(registry.resolve(FIXTURE_ID, "input").read_text())["cases"]}
_EXPECTED = {case["case"]: case for case in json.loads(registry.resolve(FIXTURE_ID, "output").read_text())["cases"]}
_WITH_A_SHARE_TRADE = sorted(name for name, case in _EXPECTED.items() if case["share_trade"] is not None)
_WITH_NONE = sorted(name for name, case in _EXPECTED.items() if case["share_trade"] is None)

AAPL_CALL = "AAPL230721C00150000"
EXPIRY = date(2023, 7, 21)


def _documented_case(name: str) -> list[dict[str, Any]]:
    """The rows Alpaca documents under one heading, each with an id of its own.

    The Trading API page prints one id on every row of an example. An event
    and its share trade are kept apart by kind, so that changes nothing
    here; the ids are left as printed.
    """
    product, heading = name.split("/")
    return [documented_option_activity(product, heading, index) for index in range(len(_rows(product, heading)))]


def _rows(product: str, heading: str) -> list[dict[str, Any]]:
    from tests.broker.alpaca.documented_examples import documented

    return documented("option_activities", "documented_activities.json")[product][heading]


def _books_for(tmp_path: Path, name: str) -> tuple[ClerkSqliteRepository, str, int]:
    """Books whose bot holds exactly what one golden case's event ends, opened the day before the event."""
    case = _CASES[name]
    symbol = case["contract"]
    root = symbol[: len(symbol) - 15]
    event_day = date.fromisoformat(_documented_case(name)[0]["date"])
    repo = books_at(tmp_path, date.fromordinal(event_day.toordinal() - 1), {"bot-a": root})
    # A negative change ends a bought position: the bot holds that many bought. A positive one, sold.
    holds_option(repo, "bot-a", symbol, contracts=-case["position_change"])
    return repo, root, case["position_change"]


@pytest.mark.parametrize("name", _WITH_A_SHARE_TRADE)
async def test_the_books_hold_the_shares_and_cash_alpaca_documents_after_each_exercise_and_assignment(
    tmp_path: Path, name: str
) -> None:
    """OEV-001: an exercised call and an assigned put buy the shares at the strike; the other two sell them."""
    repo, root, _change = _books_for(tmp_path, name)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case(name)
    try:
        await poll(repo, account)

        expected = _EXPECTED[name]
        shares = Decimal(expected["share_trade"]["qty"])
        assert held(repo, "bot-a") == {root: float(shares)}
        assert share_cash(repo, "bot-a", root) == Decimal(expected["share_trade"]["net_amount"])
        [share_lot] = [lot for lot in fifo(repo, "bot-a").open_lots if lot.symbol == root]
        # The strike, which is what the page's sentence and its own net amount give.
        assert share_lot.exact_cost == Decimal(expected["price_from_net_amount"])
        assert share_lot.exact_qty == abs(shares)
    finally:
        repo.close()


@pytest.mark.parametrize("name", _WITH_NONE)
async def test_an_expiry_alpaca_documents_ends_the_position_and_moves_no_shares(tmp_path: Path, name: str) -> None:
    repo, _root, _change = _books_for(tmp_path, name)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case(name)
    try:
        await poll(repo, account)

        assert held(repo, "bot-a") == {}
        assert [row["summary_code"] for row in bookings(repo)] == ["OPTION_EXPIRED"]
        assert option_holds(repo) == {}
    finally:
        repo.close()


async def test_an_expiry_closes_the_lot_at_nothing_so_the_premium_paid_is_the_loss(tmp_path: Path) -> None:
    """Two calls bought at $1.94 expire: 2 x 1.94 x 100 = $388.00 lost, inside the expiry session's own P&L."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=2, price=1.94)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/otm_option_expiry")
    try:
        await poll(repo, account)

        result = fifo(repo, "bot-a")
        assert result.exact_realized_pnl == Decimal("-388")
        [closed] = result.closed_lots
        assert (closed.entry_price, closed.exit_price, closed.qty) == (1.94, 0.0, 2.0)
        session = session_window_for_date(EXPIRY)
        assert session.open_ms_utc <= closed.closed_at_ms < session.close_ms_utc
    finally:
        repo.close()


async def test_a_call_assigned_to_a_bot_with_no_shares_leaves_it_short_the_stock_at_the_strike_with_the_cash(
    tmp_path: Path,
) -> None:
    """Two calls sold at $3.07 are assigned: the bot is short 200 AAPL at $150 and holds $30,000 for them."""
    repo = books_at(tmp_path, date(2023, 6, 30), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=-2, price=3.07)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/option_assignment")
    try:
        await poll(repo, account)

        assert held(repo, "bot-a") == {"AAPL": -200.0}
        assert share_cash(repo, "bot-a", "AAPL") == Decimal(30000)
        result = fifo(repo, "bot-a")
        # The premium received stays in the option lot and is realised there: 2 x 3.07 x 100.
        assert result.exact_realized_pnl == Decimal("614")
        [short] = result.open_lots
        assert (short.symbol, short.side.value, short.exact_qty, short.exact_cost) == ("AAPL", "sell", 200, 150)
    finally:
        repo.close()


async def test_a_row_delivered_twice_is_booked_once(tmp_path: Path) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=2)
    account = _FakeReadPort()
    expiry = _documented_case("trading_api/otm_option_expiry")
    account.option_events.rows = [*expiry, *expiry]
    try:
        first = await poll(repo, account, asked_for=True)
        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        # The books hold nothing an event could change any more, so the row is not even read again.
        second = await poll(repo, account, asked_for=True)
        again = book_option_events(repo, await _read_whole(repo, account))

        assert len(first.round.booked) == 1
        assert second.read is False
        assert again.booked == ()
        assert len(bookings(repo)) == 1
        assert held(repo, "bot-a") == {}
    finally:
        repo.close()


async def _read_whole(repo: ClerkSqliteRepository, account: _FakeReadPort) -> OptionEventRead:
    """What a read of the account's option events from 2023 on finds now."""
    return await option_events._read_events(repo, account, after_ms=1_672_531_200_000)


async def test_a_restart_between_reading_and_booking_books_once(tmp_path: Path) -> None:
    """The rows were read, the process died, and the next one reads them again: one booking."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/option_exercise")
    clock = repo.clock
    read_before_the_restart = await _read_whole(repo, account)
    repo.close()

    reopened = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, fold_registry=option_fold_registry()
    )
    try:
        # A process that has just opened the books reads at once, whatever the last one read.
        after = await poll(reopened, account)
        late = book_option_events(reopened, read_before_the_restart)

        assert after.read is True and len(after.round.booked) == 1
        assert late.booked == ()
        assert held(reopened, "bot-a") == {"AAPL": 200.0}
        assert len(bookings(reopened)) == 1
    finally:
        reopened.close()


def _two_bots_sold_one_call_each(tmp_path: Path) -> ClerkSqliteRepository:
    """Bot ``first`` sold a call on 2023-06-29, bot ``second`` sold one the day after."""
    repo = books_at(tmp_path, date(2023, 6, 29), {"first": "AAPL", "second": "AAPL"})
    holds_option(repo, "first", AAPL_CALL, contracts=-1, price=3.07)
    later(repo, 86_400_000)
    holds_option(repo, "second", AAPL_CALL, contracts=-1, price=2.80)
    return repo


def _one_assigned_one_expired() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    assignment = documented_option_activity("trading_api", "option_assignment", id="asn-1", qty="1")
    share_trade = documented_option_activity(
        "trading_api", "option_assignment", 1, id="trd-1", qty="-100", net_amount="15000"
    )
    # A sold option's expiry adds back to the position. No page shows one; the sign is this repo's reading.
    expiry = documented_option_activity("trading_api", "otm_option_expiry", id="exp-1", qty="1")
    return assignment, share_trade, expiry


@pytest.mark.parametrize("first_read", ["the assignment", "the expiry", "both"])
async def test_the_bot_that_sold_first_is_assigned_whichever_row_is_read_first(
    tmp_path: Path, first_read: str
) -> None:
    """Two bots sold the same call. One contract is assigned and one expires: the older lot is the one assigned."""
    repo = _two_bots_sold_one_call_each(tmp_path)
    assignment, share_trade, expiry = _one_assigned_one_expired()
    account = _FakeReadPort()
    account.option_events.rows = {
        "the assignment": [assignment, share_trade], "the expiry": [expiry], "both": [expiry, assignment, share_trade],
    }[first_read]
    try:
        await poll(repo, account, asked_for=True)
        account.option_events.rows = [assignment, share_trade, expiry]
        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        await poll(repo, account, asked_for=True)

        assert held(repo, "first") == {"AAPL": -100.0}
        assert held(repo, "second") == {}
        assert {row["strategy_instance_id"]: row["summary_code"] for row in bookings(repo)} == {
            "first": "OPTION_ASSIGNED", "second": "OPTION_EXPIRED",
        }
        # The other bot's lot was ended by the expiry, not closed by an order of its own: nothing is said
        # of a lot closed before the row was read, whichever row was booked first.
        assert ("first", "OPTION_EVENT_AFTER_A_LOT_CLOSED") not in option_holds(repo)
        assert {reason for bot, reason in option_holds(repo) if bot == "first"} <= {
            "OPTION_SHARES_HELD", "OPTION_SHARE_TRADE_MISSING",
        }
    finally:
        repo.close()


async def test_an_assignment_of_two_bots_lots_is_booked_for_each_and_each_record_carries_the_whole_allocation(
    tmp_path: Path,
) -> None:
    repo = _two_bots_sold_one_call_each(tmp_path)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/option_assignment")
    try:
        await poll(repo, account)

        assert held(repo, "first") == {"AAPL": -100.0}
        assert held(repo, "second") == {"AAPL": -100.0}
        allocations = [json.loads(row["facts_json"])["allocation"] for row in bookings(repo)]
        assert allocations[0] == allocations[1]
        assert [(lot["owner"], lot["contracts"]) for lot in allocations[0]] == [("first", 1), ("second", 1)]
    finally:
        repo.close()


async def test_a_crash_between_two_bots_bookings_is_finished_from_the_record_and_never_matched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first bot's part was booked and the process died. The second's is booked from the first's record."""
    repo = _two_bots_sold_one_call_each(tmp_path)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/option_assignment")
    real_append = option_events._append_booking
    appended: list[str] = []

    def dies_after_the_first(repository: ClerkSqliteRepository, **booking: Any) -> Any:
        if appended:
            raise RuntimeError("the process died here")
        appended.append(booking["sid"])
        return real_append(repository, **booking)

    monkeypatch.setattr(option_events, "_append_booking", dies_after_the_first)
    try:
        with pytest.raises(RuntimeError, match="died"):
            await poll(repo, account)
        assert held(repo, "first") == {"AAPL": -100.0}
        assert held(repo, "second") == {AAPL_CALL: -1.0}
        monkeypatch.setattr(option_events, "_append_booking", real_append)
        # A third bot sells the same call before the row is read again. A second matching would see its lot.
        matched: list[Any] = []
        monkeypatch.setattr(option_events, "allocation_of", lambda *a, **k: matched.append(a) or pytest.fail("matched again"))

        later(repo, option_events.OPTION_EVENT_POLL_RETRY_MS)
        await poll(repo, account, asked_for=True)

        assert matched == []
        assert held(repo, "second") == {"AAPL": -100.0}
        assert [row["strategy_instance_id"] for row in bookings(repo)] == ["first", "second"]
    finally:
        repo.close()


async def test_an_event_of_a_contract_only_an_outside_order_holds_is_booked_for_no_bot_and_holds_nobody(
    tmp_path: Path,
) -> None:
    """The owner bought two calls by hand, and they expired. The books recorded the order; no bot is touched."""
    from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture, observe_external_order

    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    # The books know the contract: bot-a traded it earlier and is flat in it now.
    holds_option(repo, "bot-a", AAPL_CALL, contracts=1)
    holds_option(repo, "bot-a", AAPL_CALL, contracts=-1)
    observe_external_order(
        repo,
        order=_broker_order_fixture(
            "by-hand-1", order_id="alpaca-by-hand-1", symbol=AAPL_CALL, status="filled", side="buy", quantity=2,
            filled_quantity=2, filled_avg_price=1.5,
        ),
    )
    account = _FakeReadPort()
    try:
        done = book_option_events(
            repo, await _read_rows(repo, account, _documented_case("trading_api/otm_option_expiry"))
        )

        assert done.booked == ()
        assert option_holds(repo) == {}
        assert held(repo, "bot-a") == {}
    finally:
        repo.close()


async def _read_rows(repo: ClerkSqliteRepository, account: _FakeReadPort, rows: list[dict[str, Any]]) -> OptionEventRead:
    account.option_events.rows = rows
    return await _read_whole(repo, account)


async def test_a_bots_lot_and_an_outside_holding_of_one_contract_are_taken_oldest_first(tmp_path: Path) -> None:
    """The bot bought one call first, the owner bought one by hand after. One is exercised: the bot's."""
    from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture, observe_external_order

    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=1)
    observe_external_order(
        repo,
        order=_broker_order_fixture(
            "by-hand-2", order_id="alpaca-by-hand-2", symbol=AAPL_CALL, status="filled", side="buy", quantity=1,
            filled_quantity=1, filled_avg_price=1.5,
        ),
    )
    account = _FakeReadPort()
    exercise = documented_option_activity("trading_api", "option_exercise", id="exc-1", qty="-1")
    share_trade = documented_option_activity(
        "trading_api", "option_exercise", 1, id="trd-1", qty="100", price="150", net_amount="-15000"
    )
    expiry = documented_option_activity("trading_api", "otm_option_expiry", id="exp-1", qty="-1")
    try:
        book_option_events(repo, await _read_rows(repo, account, [exercise, share_trade, expiry]))

        assert held(repo, "bot-a") == {"AAPL": 100.0}
        [booked] = bookings(repo)
        allocation = json.loads(booked["facts_json"])["allocation"]
        assert [lot["owner"] for lot in allocation] == ["bot-a"]
        # The expiry took what was left: the holding outside the books. It is booked for no bot.
        assert OUTSIDE_THE_BOOKS not in {row["strategy_instance_id"] for row in bookings(repo)}
        assert option_holds(repo).keys() == {("bot-a", "OPTION_SHARES_HELD")}
    finally:
        repo.close()


async def test_what_an_event_took_from_outside_the_books_is_not_offered_to_the_event_read_after_it(
    tmp_path: Path,
) -> None:
    """The owner bought one call by hand, then the bot bought two. Two are exercised: the owner's and one of
    the bot's. A read later, one more is exercised. It can only be the bot's last: the owner's is gone."""
    from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture, observe_external_order

    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    # The books know the contract before the owner's order is seen: the bot traded it and is flat.
    holds_option(repo, "bot-a", AAPL_CALL, contracts=1)
    holds_option(repo, "bot-a", AAPL_CALL, contracts=-1)
    observe_external_order(
        repo,
        order=_broker_order_fixture(
            "by-hand-3", order_id="alpaca-by-hand-3", symbol=AAPL_CALL, status="filled", side="buy", quantity=1,
            filled_quantity=1, filled_avg_price=1.5,
        ),
    )
    later(repo, 1_000)
    holds_option(repo, "bot-a", AAPL_CALL, contracts=2)
    account = _FakeReadPort()
    first = [
        documented_option_activity("trading_api", "option_exercise", id="exc-1", qty="-2"),
        documented_option_activity("trading_api", "option_exercise", 1, id="trd-1", price="150"),
    ]
    second = [
        documented_option_activity("trading_api", "option_exercise", id="exc-2", qty="-1"),
        documented_option_activity("trading_api", "option_exercise", 1, id="trd-2", qty="100", price="150", net_amount="-15000"),
    ]
    try:
        book_option_events(repo, await _read_rows(repo, account, first))
        assert held(repo, "bot-a") == {AAPL_CALL: 1.0, "AAPL": 100.0}

        book_option_events(repo, await _read_rows(repo, account, [*first, *second]))

        assert held(repo, "bot-a") == {"AAPL": 200.0}
    finally:
        repo.close()


async def test_a_rebuild_from_the_mirror_books_every_event_again_as_it_was(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/option_exercise")
    await poll(repo, account)
    before = _money_tables(repo)
    clock = repo.clock
    replay_root = tmp_path_factory.mktemp("replay")
    shutil.copytree(tmp_path, replay_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("clerk.db*"))
    repo.close()

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=replay_root, clock=clock, fold_registry=option_fold_registry()
    )
    try:
        assert _money_tables(rebuilt) == before
    finally:
        rebuilt.close()


def test_a_rebuild_from_the_mirror_still_counts_a_booked_sale_once(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The booking's record names the outside order it booked. A rebuild reads that name back from the mirror."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=1)
    observe_external_order(
        repo,
        order=_broker_order_fixture(
            "not-ours-1", order_id="alpaca-sells-1", symbol=AAPL_CALL, status="filled", side="sell", quantity=1,
            filled_quantity=1, filled_avg_price=0.40,
        ),
    )
    book_option_events(repo, None)
    before = _money_tables(repo)
    assert repo.external_order_resources() == ()
    clock = repo.clock
    replay_root = tmp_path_factory.mktemp("replay")
    shutil.copytree(tmp_path, replay_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("clerk.db*"))
    repo.close()

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=replay_root, clock=clock, fold_registry=option_fold_registry()
    )
    try:
        assert _money_tables(rebuilt) == before
        assert rebuilt.external_order_resources() == ()
        assert rebuilt.external_order_by_broker_order_id("alpaca-sells-1") is not None
    finally:
        rebuilt.close()


def _money_tables(repo: ClerkSqliteRepository) -> dict[str, list[tuple]]:
    def rows(conn: sqlite3.Connection, table: str, order: str) -> list[tuple]:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]

    with repo.write_fence() as conn:
        return {
            "orders": rows(conn, "orders", "order_ref"),
            "fills": rows(conn, "fills", "fill_id"),
            "positions": rows(conn, "positions", "subject_id, symbol"),
            "order_contracts": rows(conn, "order_contracts", "order_ref"),
            "commands": [
                tuple(row) for row in conn.execute(
                    "SELECT command_id, idempotency_key, payload_hash, kind, action, state FROM commands ORDER BY command_id"
                )
            ],
            "effects": [
                tuple(row) for row in conn.execute(
                    "SELECT effect_operation_id, kind, state FROM effect_operations ORDER BY effect_operation_id"
                )
            ],
        }


async def test_a_booked_event_is_a_finished_piece_of_work_no_pass_looks_up_at_the_broker(tmp_path: Path) -> None:
    """The orders an event is booked on were never sent anywhere, and nothing lists them as work to resolve."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = _documented_case("trading_api/option_exercise")
    try:
        await poll(repo, account)

        assert [effect.kind for effect in repo.reconcilable_effect_operations()] == ["ENTER"]
        with repo.write_fence() as conn:
            event_orders = conn.execute(
                "SELECT o.role, o.broker_state, e.kind, e.state FROM orders o JOIN effect_operations e "
                "ON e.effect_operation_id = o.effect_operation_id WHERE e.kind = 'RECONCILE' ORDER BY o.order_ref"
            ).fetchall()
            kinds = {row[0] for row in conn.execute("SELECT DISTINCT transition_kind FROM custody_transitions")}
            read_from = {
                row[0] for row in conn.execute("SELECT evidence_source FROM fills WHERE fill_id LIKE 'option-event:%'")
            }
        # On a real account an event's fills are evidence read from the broker's activities.
        assert read_from == {"activity_recovery"}
        # The order that closes the lot, and the order the shares came on, which stands where an entry's would.
        assert sorted(tuple(row) for row in event_orders) == [
            ("ENTRY", "filled", "RECONCILE", "succeeded"), ("OTHER", "filled", "RECONCILE", "succeeded"),
        ]
        assert OPTION_POSITION_ENDED in kinds
        assert broker_holds(AAPL=200)[0].quantity == 200
    finally:
        repo.close()
