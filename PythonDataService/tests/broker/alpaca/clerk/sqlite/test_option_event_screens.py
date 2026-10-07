"""What History and Activity are handed about an option position that ended with no order of the bot's (#2913).

A closed set of endings, every figure as a field, and one sentence, written
by the server. Each is read here where it is served: Activity's pages and
its detail, and the History service's own run. The screens themselves are
another slice's.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.option_event_books import recorded_option_endings
from app.broker.alpaca.clerk.sqlite.option_event_folds import OptionEnding
from app.broker.alpaca.clerk.sqlite.option_events import book_option_events
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.options.contract import OptionContract
from app.schemas.bot_history import BotHistoryOrders, BotHistoryRun
from app.schemas.clerk_transaction_projection import ClerkTransactionSummaryRow
from app.schemas.option_endings import OptionEndingKind, OptionEndingView
from app.services.broker_v2_panel import bot_history, sqlite_roster_status
from app.services.option_ending_views import option_ending_view, price_usd, usd
from app.services.sqlite_clerk_transaction_projection import sqlite_transaction_detail, sqlite_transaction_history
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture, _FakeReadPort, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import ACCOUNT_ID
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import books_at, holds_option, later, poll
from tests.broker.alpaca.clerk.sqlite.test_dry_run_option_expiry import _keeps_spy
from tests.broker.alpaca.clerk.sqlite.test_option_close_fences import EXPIRY_CLOSE, SATURDAY_NOON, _wait_until
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (  # noqa: F401
    SYMBOL,
    _DryRun,
    dry_run,
)

CALL = "AAPL230721C00150000"
EXPIRY_DATE = date(2023, 7, 21)
_OCC_CODE_SHAPE = "230721C0015"


def test_the_endings_a_screen_may_be_handed_are_the_ones_the_books_record() -> None:
    assert set(get_args(OptionEndingKind)) == {ending.value for ending in OptionEnding}


def _served(repo: ClerkSqliteRepository) -> list[OptionEndingView]:
    with repo.write_fence() as conn:
        return [option_ending_view(ending) for ending in recorded_option_endings(conn)]


@pytest.fixture
def served_account(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    """Books with two bots, read through the account's active Clerk as Activity's routes read them."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "stock-bot": "MSFT"})
    account = _FakeReadPort()
    set_active_clerk_runtime(ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=account),  # type: ignore[arg-type]
    ))
    try:
        yield repo
    finally:
        set_active_clerk_runtime(None)
        repo.close()


def _history(**narrowed: Any) -> list[ClerkTransactionSummaryRow]:
    page = sqlite_transaction_history(
        account_id=ACCOUNT_ID, limit=25, cursor=None, lifecycle_state=None, strategy_instance_id=None, run_id=None,
        **{"origin": None, **narrowed},
    )
    assert page is not None
    return list(page.rows)


# ── History ──────────────────────────────────────────────────────────────


async def test_an_exercise_is_served_with_its_date_its_contract_its_shares_and_its_cash_and_counts_as_no_order(
    tmp_path: Path,
) -> None:
    """Two calls struck at 150 are exercised on Friday 2023-07-21: 200 shares bought for $30,000.00.

    Nobody sent the exercise, so the run's and the bot's counts of orders
    are what they were before it. Its two fills, the lot's close and the
    shares, are transactions beside the entry's.
    """
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL", "stock-bot": "MSFT"})
    holds_option(repo, "bot-a", CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [documented_option_activity("trading_api", "option_exercise", index) for index in (0, 1)]
    try:
        before = {bot.strategy_instance_id: bot for bot in repo.bot_history().bots}["bot-a"]
        later(repo, 3 * 86_400_000)
        await poll(repo, account)
        booked_at_ms = repo.clock()

        history = {bot.strategy_instance_id: bot for bot in repo.bot_history().bots}
        [run] = history["bot-a"].runs
        [shown] = [option_ending_view(ending) for ending in run.option_endings]

        assert (shown.kind, shown.contracts, shown.held, shown.simulated) == ("exercised", 2, "bought", False)
        # The contract as an object, for the one shared renderer. Its expiry is a date: the 16:00 New York anchor.
        contract = OptionContract.from_occ_symbol(CALL)
        assert shown.contract.model_dump() == {
            "occ_symbol": CALL, "underlying": "AAPL", "expiry_ms": contract.expiry_ms, "right": "call",
            "strike": Decimal(150),
        }
        # The date the position ended is its own field, and is not the day the Clerk booked it, three days on.
        assert shown.ended_on_ms == et_midnight_ms(EXPIRY_DATE)
        assert shown.recorded_at_ms == booked_at_ms and shown.recorded_at_ms > shown.ended_on_ms + 2 * 86_400_000
        assert (shown.shares, shown.share_symbol, shown.share_price, shown.share_price_usd) == (
            200, "AAPL", Decimal(150), "$150.00"
        )
        assert (shown.share_cash_cents, shown.share_cash_usd) == (-3_000_000, "-$30,000.00")
        assert (shown.close_price, shown.close_cash_cents, shown.settled_from) == (None, None, None)
        # One sentence, the server's. It names no contract code: the screen writes the contract's name.
        assert shown.headline == "2 contracts were exercised: 200 AAPL bought at $150.00 a share, $30,000.00 paid."
        assert _OCC_CODE_SHAPE not in shown.headline
        # The exercise added no order to the run's counts or the bot's, and two transactions to each.
        assert (before.runs[0].transactions, before.transactions) == (1, 1)
        assert (run.orders, run.transactions) == (before.runs[0].orders, 3)
        assert (history["bot-a"].orders, history["bot-a"].transactions) == (before.orders, 3)
        assert [run.option_endings for run in history["stock-bot"].runs] == [()]
    finally:
        repo.close()


async def test_history_serves_a_dry_runs_settlement_on_its_run_as_a_simulations_and_counts_no_order_for_it(
    dry_run: _DryRun, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The History service's own run, as the page is handed it. One call the simulation sold at 5.25 a share.

    The run sent one order, its entry. The settlement is no order of its
    own, and it is served as a simulation's: History's words for a real
    ending are the broker's.
    """
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25")
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    for module in (bot_history, sqlite_roster_status):
        monkeypatch.setattr(module, "live_artifacts_root", lambda: dry_run.artifacts_root)

    bots, gaps = bot_history.compose_bots(
        dry_run.repo.bot_history(), authority_world="synthetic", account_id=dry_run.repo.account_id
    )

    assert gaps == ()
    [bot] = bots
    [run] = bot.runs
    assert run.option_endings is not None
    [shown] = run.option_endings
    assert (shown.kind, shown.simulated, shown.contract.occ_symbol) == ("settled_in_cash", True, SYMBOL)
    assert shown.close_cash_usd == "$525.00" and "simulation" in shown.headline
    assert (run.orders.sent, run.orders.filled, run.transaction_count) == (1, 1, 2)
    assert (bot.orders.sent, bot.orders.filled, bot.transaction_count) == (1, 1, 2)


def test_a_run_with_no_such_ending_is_served_exactly_as_it_was_before_the_field_existed() -> None:
    """The coordinator reads each lane's answer strictly: a new key on every run would be refused by an older one."""
    run = BotHistoryRun(
        run_id="run-1", started_at_ms=1, stopped_at_ms=None, running=True, outcome=None, transaction_count=0,
        orders=BotHistoryOrders(sent=0, filled=0, cancelled=0, rejected=0),
    )

    assert "option_endings" not in run.model_dump()
    assert "option_endings" not in run.model_dump_json()


# ── Activity ─────────────────────────────────────────────────────────────


async def test_activity_serves_the_ending_on_its_row_in_every_page_and_in_the_detail_and_on_no_other_row(
    served_account: ClerkSqliteRepository,
) -> None:
    """An expiry of two calls, booked for bot-a. Activity's unfiltered page, its page filtered by origin and
    its detail all carry it. The entry's own row, in the page and in the detail, is served with no such key:
    a stock transaction's bytes are what they were."""
    repo = served_account
    holds_option(repo, "bot-a", CALL, contracts=2)
    account = _FakeReadPort()
    account.option_events.rows = [documented_option_activity("trading_api", "otm_option_expiry")]
    later(repo, 2 * 86_400_000)
    await poll(repo, account)

    rows = _history()

    [event] = [row for row in rows if row.option_event is not None]
    [entry] = [row for row in rows if row.option_event is None]
    assert event.strategy_instance_id == "bot-a" and event.subject_id == "bot:bot-a"
    shown = event.option_event
    assert (shown.kind, shown.contract.occ_symbol, shown.contracts, shown.held) == ("expired", CALL, 2, "bought")
    assert shown.ended_on_ms == et_midnight_ms(EXPIRY_DATE) and shown.recorded_at_ms == repo.clock()
    assert shown.headline == "2 contracts expired worth nothing."
    assert (shown.shares, shown.share_symbol, shown.close_price, shown.simulated) == (None, None, None, False)
    # The same ending in the page filtered by origin, which is put together by another read.
    filtered = _history(origin=event.transaction_origin)
    assert [row.option_event for row in filtered if row.transaction_id == event.transaction_id] == [shown]
    # And in the detail.
    _active, detail = sqlite_transaction_detail(account_id=ACCOUNT_ID, transaction_id=event.transaction_id)
    assert detail is not None and detail.option_event == shown
    # A row that is no option event has no such key, in the page or in the detail.
    assert "option_event" not in entry.model_dump() and "option_event" not in entry.model_dump_json()
    _active, plain = sqlite_transaction_detail(account_id=ACCOUNT_ID, transaction_id=entry.transaction_id)
    assert plain is not None
    assert "option_event" not in plain.model_dump() and '"option_event"' not in plain.model_dump_json()


async def test_an_assigned_call_is_served_with_the_shares_it_took_and_the_cash_it_paid_in(tmp_path: Path) -> None:
    """One call sold, then assigned: 100 shares are sold at the strike and $15,000.00 comes in. The shares the
    bot gave up are served below zero, never as shares gained."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=-1)
    account = _FakeReadPort()
    account.option_events.rows = [
        documented_option_activity("trading_api", "option_assignment", id="asn-1", qty="1", date="2023-07-21"),
        documented_option_activity(
            "trading_api", "option_assignment", 1, id="trd-1", qty="-100", price="150", net_amount="15000",
            date="2023-07-21",
        ),
    ]
    try:
        later(repo, 2 * 86_400_000)
        await poll(repo, account)

        [shown] = _served(repo)

        assert (shown.kind, shown.held, shown.contracts) == ("assigned", "sold", 1)
        assert (shown.shares, shown.share_symbol, shown.share_price_usd) == (-100, "AAPL", "$150.00")
        assert (shown.share_cash_cents, shown.share_cash_usd) == (1_500_000, "$15,000.00")
        assert shown.headline == "1 contract was assigned: 100 AAPL sold at $150.00 a share, $15,000.00 received."
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("held", "outside_side", "cash_cents", "cash_usd", "headline"),
    [
        pytest.param(
            1, "sell", 4_000, "$40.00",
            "1 contract was sold at $0.40 a share by an order the Clerk did not place: $40.00.", id="a bought contract sold",
        ),
        pytest.param(
            -1, "buy", -4_000, "-$40.00",
            "1 contract was bought back at $0.40 a share by an order the Clerk did not place: $40.00.",
            id="a sold contract bought back",
        ),
    ],
)
def test_a_sale_outside_is_served_with_its_price_and_its_cash_and_told_as_what_it_was(
    tmp_path: Path, held: int, outside_side: str, cash_cents: int, cash_usd: str, headline: str
) -> None:
    """An order the Clerk did not place closed the bot's contract at $0.40 a share: $40.00, in or out."""
    repo = books_at(tmp_path, date(2023, 7, 20), {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", CALL, contracts=held)
    try:
        observe_external_order(repo, order=_broker_order_fixture(
            "not-ours-1", order_id="outside-1", symbol=CALL, status="filled", side=outside_side, quantity=1,
            filled_quantity=1, filled_avg_price=0.4,
        ))
        book_option_events(repo, None)

        [shown] = _served(repo)
        with repo.write_fence() as conn:
            [recorded] = recorded_option_endings(conn)

        assert (shown.kind, shown.held) == ("sold_outside", "bought" if held > 0 else "sold")
        # No bare 0.4: the price is an exact decimal and its dollars are written out.
        assert (shown.close_price, shown.close_price_usd) == (Decimal("0.4"), "$0.40")
        assert (shown.close_cash_cents, shown.close_cash_usd) == (cash_cents, cash_usd)
        assert shown.headline == headline
        # It has no row's date: it ended on the New York date it is counted on.
        assert recorded.facts.event_date_ms is None
        assert shown.ended_on_ms == et_midnight_ms(et_date_at_ms(recorded.facts.booked_as_of_ms))
    finally:
        repo.close()


# ── a simulation's settlement, and the owner's ending ────────────────────


async def test_a_dry_runs_settlement_is_served_as_a_simulations_with_the_price_it_read_and_how_old_that_is(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """One call sold by the simulation at 5.25 a share, from SPY at 590.25 one minute before the close.

    History's words for a real ending are the broker's. Nothing but the flag
    and the sentence keeps a Dry Run's from being read as one.
    """
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "590.25", end_ms=EXPIRY_CLOSE - 60_000)
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()

    [shown] = _served(dry_run.repo)

    assert (shown.kind, shown.simulated, shown.contract.occ_symbol) == ("settled_in_cash", True, SYMBOL)
    assert (shown.close_price, shown.close_price_usd) == (Decimal("5.25"), "$5.25")
    assert (shown.close_cash_cents, shown.close_cash_usd) == (52_500, "$525.00")
    assert shown.shares is None and shown.share_symbol is None
    read = shown.settled_from
    assert read is not None
    assert (read.underlying, read.price, read.price_usd) == ("SPY", Decimal("590.25"), "$590.25")
    assert (read.bar_ended_at_ms, read.last_session_close_ms, read.before_close_ms) == (
        EXPIRY_CLOSE - 60_000, EXPIRY_CLOSE, 60_000
    )
    assert shown.ended_on_ms == et_midnight_ms(date(2026, 9, 18)) and shown.recorded_at_ms == SATURDAY_NOON
    assert shown.headline == (
        "1 contract was sold by a simulation at its exercise value, $5.25 a share: $525.00. That is the "
        "simulation's rule, not a broker's. It read SPY at $590.25, 1 minute before the close of the contract's "
        "last session."
    )


async def test_a_dry_runs_expiry_is_served_as_a_simulations_too(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    _keeps_spy(dry_run, "580.00")
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()

    [shown] = _served(dry_run.repo)

    assert (shown.kind, shown.simulated, shown.close_price, shown.close_cash_cents) == ("expired", True, None, None)
    assert shown.settled_from is not None and shown.settled_from.before_close_ms == 0
    assert shown.headline == (
        "1 contract expired worth nothing. A simulation settled it by its own rule. It read SPY at $580.00, at "
        "the close of the contract's last session."
    )


async def test_an_ending_at_the_owners_word_is_served_with_who_said_so_and_why_nothing_settled_it(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, SATURDAY_NOON)
    await dry_run.clerk.reconcile_once()
    await dry_run.clerk.end_expired_option(
        strategy_instance_id=dry_run.binding.strategy_instance_id, symbol=SYMBOL, operator="the-owner", reason=None
    )

    [shown] = _served(dry_run.repo)

    assert (shown.kind, shown.simulated, shown.ended_by_operator) == ("ended_by_owner", True, "the-owner")
    assert shown.no_settlement_reason is not None and "SPY" in shown.no_settlement_reason
    assert (shown.close_price, shown.shares, shown.settled_from) == (None, None, None)
    assert shown.ended_on_ms == et_midnight_ms(date(2026, 9, 18))
    assert shown.headline == (
        "1 contract was ended at the owner's word and counted as worth nothing. No settlement could be made. "
        "A simulation had no price to settle it from."
    )


# ── dollars, written out ─────────────────────────────────────────────────


def test_money_and_prices_are_written_out_in_dollars_to_the_cent_and_never_bare() -> None:
    assert [usd(cents) for cents in (0, 5, 4_000, 52_500, 5_850_000, -3_000_000)] == [
        "$0.00", "$0.05", "$40.00", "$525.00", "$58,500.00", "-$30,000.00"
    ]
    assert [price_usd(Decimal(text)) for text in ("585", "0.4", "5.25", "150.00", "1250.5")] == [
        "$585.00", "$0.40", "$5.25", "$150.00", "$1,250.50"
    ]
    # An average price finer than a cent keeps its digits: it is not rounded to a price nobody paid.
    assert price_usd(Decimal("0.405")) == "$0.405"
