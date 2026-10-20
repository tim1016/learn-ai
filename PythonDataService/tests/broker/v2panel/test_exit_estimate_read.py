"""What getting out of a bot's held option position costs, as the bot page reads it (#3100).

The books are real. A real account keeps no option quotes: its quotes are the live ones in this process's own
IBKR supervisor, here a double that holds what a test says it holds. A Dry Run keeps its quotes in a file beside
its books (``kept_option_quotes``), and the estimate reads the newest of them as a simulated close does.

The spread is the PRD's received one, two structures of it: the 580 call is sold and the 585 call bought,
a hundred shares a contract, expiring 2026-09-18.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.exit_estimate import BUY_BACK_ALONE_LINE, CLOSE_UNREAD_LINE, CUTOFF_LINE, FEES_LINE
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_STUCK_REASON_CODE, ExitStuckCause
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.options.deadlines import CloseTerms
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import Chain, FeedState, WatchedContract
from app.services.broker_v2_panel import exit_estimate_read
from app.services.broker_v2_panel.exit_estimate_read import (
    exit_estimate_view,
    read_exit_estimate,
    read_exit_estimates,
    read_quotes_ahead,
)
from app.utils.et_words import et_when_words
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import accept_option_order, fill, open_option_books
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import RECEIVED_PICKS, RECEIVED_SPREAD, call
from tests.broker.alpaca.clerk.sqlite.test_multi_leg_close import opened
from tests.broker.alpaca.clerk.test_simulated_multi_leg_orders import world as _world_fixture

world = _world_fixture

C580, C585 = call(580).occ_symbol, call(585).occ_symbol
GENERATION = 4
LIVE = "Live option quotes from this Clerk's IBKR feed"
# ``NOON`` is a Tuesday's. A structure with a sold leg is closed a fixed lead before each session's own close.
TUESDAY, WEDNESDAY = date(2026, 9, 8), date(2026, 9, 9)
NOT_OVERNIGHT = "a sold option is not held overnight"
HALF_AN_HOUR_MS = 30 * 60_000
HOUR_MS = 60 * 60_000


def _closes_itself(day: date, *, now_ms: int) -> str:
    """The line that says the Clerk closes the position on ``day``'s session by its own clock."""
    when = et_when_words(session_close_ms_utc(day) - CloseTerms().sold_leg_close_lead_ms, now_ms=now_ms)
    return f"This system closes the position itself at {when}: {NOT_OVERNIGHT}."


def _missed(day: date, *, now_ms: int) -> str:
    """The line that says ``day``'s close was due and the session ended with the position still held."""
    when = et_when_words(session_close_ms_utc(day) - CloseTerms().sold_leg_close_lead_ms, now_ms=now_ms)
    return (
        f"This system's own close of the position was due at {when}: {NOT_OVERNIGHT}. "
        "That session ended with the position still held."
    )


def _quote(bid: str | None, ask: str | None, *, age_ms: int = 1_000, generation: int = GENERATION) -> HeldQuote:
    return HeldQuote(
        bid=None if bid is None else QuoteSide(D(bid), NOON - age_ms),
        ask=None if ask is None else QuoteSide(D(ask), NOON - age_ms),
        data_type=QuoteDataType.LIVE,
        generation=generation,
    )


@dataclass
class _Supervisor:
    """This process's IBKR supervisor, as the estimate asks it: a feed state, and a chain of quotes by underlying."""

    feed: FeedState | None
    quotes: dict[str, HeldQuote]

    def option_feed(self) -> FeedState | None:
        return self.feed

    def option_chain(self, underlying: str) -> Chain | None:
        watched = {symbol: WatchedContract(quote, None) for symbol, quote in self.quotes.items()}
        return Chain(listings=(), watched=watched) if underlying == "SPY" else None


def _supervisor(monkeypatch: pytest.MonkeyPatch, *, feed: FeedState | None = FeedState(GENERATION, None), **quotes: Any) -> None:
    held = {C580 if name == "short" else C585: quote for name, quote in quotes.items()}
    source = None if feed is None else _Supervisor(feed, held)
    monkeypatch.setattr(exit_estimate_read, "get_option_quotes", lambda: source)


# ---- a real account: live quotes, while the bot's run asks for them ------------------------------------------


async def test_a_real_accounts_estimate_is_priced_from_the_live_quotes_and_names_the_close_it_owes(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two structures: the sold 580 call is bought back at its ask, 5.35 × 200 shares = $1,070.00; the bought 585
    call is sold at its bid, 2.20 × 200 shares = $440.00. The whole close pays $630.00."""
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35"), long=_quote("2.20", "2.24"))

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None
    assert (estimate.net_close.state, estimate.net_close.amount_cents, estimate.net_close.source) == (
        "known", 63_000, LIVE,
    )
    assert (estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents) == ("known", 107_000)
    assert [(quality.symbol, quality.contracts, quality.age_ms) for quality in estimate.quotes] == [
        (C580, -2, 1_000), (C585, 2, 1_000),
    ]
    # The entry is held with a sold leg, so a close is owed before this session's close.
    assert estimate.conditions == (
        FEES_LINE, BUY_BACK_ALONE_LINE, _closes_itself(TUESDAY, now_ms=NOON), CUTOFF_LINE,
    )


async def test_a_real_account_with_no_live_quote_states_no_price_says_the_run_is_not_asking_and_is_served_no_amount(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real accounts keep no option quotes. With no supervisor in this process, nothing is estimated from a mark,
    a fill or a share of the underlying, and the reason is stated. The page is served no amount, never zero."""
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, feed=None)

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)
    assert estimate is not None, "an estimate is read for the bot's held option position"
    view = exit_estimate_view(estimate)

    for name, figure in (("net_close", estimate.net_close), ("gross_buy_back", estimate.gross_buy_back)):
        assert (figure.state, figure.amount_cents, figure.terms) == ("unknown", None, ()), f"{name} states no price"
        assert "this estimate reads this Clerk's live IBKR quotes" in figure.source, f"{name} says it reads the Clerk's live quotes"
        assert "the bot's run is not asking for any" in figure.source, f"{name} says the run is not asking"
    assert {quality.state for quality in estimate.quotes} == {"unknown"}, "every contract's quote is unknown"
    served_net, served_buy_back = view.net_close, view.gross_buy_back
    assert (served_net.state, served_net.amount_usd, served_net.terms, served_net.as_of_ms) == (
        "unknown", None, (), None,
    ), "the served net close has no amount and is never zero"
    assert (served_buy_back.state, served_buy_back.amount_usd, served_buy_back.terms, served_buy_back.as_of_ms) == (
        "unknown", None, (), None,
    ), "the served buy-back has no amount and is never zero"
    assert [(leg.state, leg.age_ms, leg.missing, leg.gap_usd) for leg in view.quotes] == [
        ("unknown", None, ("bid", "ask"), None)
    ] * 2, "each served contract is unknown with no age, no bid or ask and no gap"


async def test_an_unhealthy_feed_is_why_a_real_accounts_figures_are_unknown(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(
        monkeypatch, feed=FeedState(GENERATION, "the IBKR market-data connection is not up"),
        short=_quote("5.30", "5.35"), long=_quote("2.20", "2.24"),
    )

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None and estimate.net_close.state == "unknown"
    assert "the live quote feed is not healthy: the IBKR market-data connection is not up" in estimate.net_close.source


async def test_a_contract_the_run_holds_no_line_for_is_unknown_and_the_other_is_still_priced(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the bought leg has a line: the net close needs the sold leg's ask and is unknown, and so is the
    buy-back. The bought leg's own quote is still said."""
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, long=_quote("2.20", "2.24"))

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None
    assert (estimate.net_close.state, estimate.gross_buy_back.state) == ("unknown", "unknown")
    assert "none is held for this contract" in estimate.gross_buy_back.source
    assert {quality.symbol: quality.state for quality in estimate.quotes} == {C580: "unknown", C585: "known"}


async def test_a_quote_from_before_the_feeds_last_reconnect_is_no_quote(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35", generation=GENERATION - 1), long=_quote("2.20", "2.24"))

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None and estimate.gross_buy_back.state == "unknown"


async def test_a_live_quote_older_than_a_marks_limit_is_a_stale_figure_not_an_unknown_one(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35", age_ms=80_001), long=_quote("2.20", "2.24"))

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None
    assert (estimate.net_close.state, estimate.net_close.amount_cents) == ("stale", 63_000)
    assert (estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents) == ("stale", 107_000)


async def test_the_page_is_served_the_figures_each_contract_and_the_words_as_python_authored_them(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35"), long=_quote("2.20", None))
    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)
    assert estimate is not None

    view = exit_estimate_view(estimate)

    assert (view.net_close.state, view.net_close.amount_usd) == ("known", "630.00")
    assert [(term.label, term.amount_usd) for term in view.net_close.terms] == [
        (f"Buy back 2 {C580} at the ask", "1070.00"), (f"Sell 2 {C585} at the bid", "-440.00"),
    ]
    assert (view.gross_buy_back.state, view.gross_buy_back.amount_usd) == ("known", "1070.00")
    assert [
        (leg.contract.occ_symbol, leg.contract.underlying, leg.contract.right, leg.contract.strike, leg.contracts,
         leg.state, leg.age_ms, leg.missing, leg.gap_usd)
        for leg in view.quotes
    ] == [
        (C580, "SPY", "call", D("580"), -2, "known", 1_000, (), "0.05"),
        (C585, "SPY", "call", D("585"), 2, "known", 1_000, ("ask",), None),
    ]
    assert view.conditions == estimate.conditions


async def test_the_accounts_picture_reads_each_holding_bots_estimate_from_the_holdings_it_was_handed(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3101: the same estimate the bot's page reads, for each bot that holds a contract and no other."""
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35"), long=_quote("2.20", "2.24"))
    held = {(C580, 100): -2, (C585, 100): 2}

    estimates = read_exit_estimates(books, {"bot-a": held, "flat-bot": {}, "closed-out": {(C585, 100): 0}}, now_ms=NOON)

    assert list(estimates) == ["bot-a"]
    assert estimates["bot-a"] == read_exit_estimate(books, "bot-a", now_ms=NOON)


# ---- what the close owes -------------------------------------------------------------------------------------
#
# Real books throughout: the Clerk's own read of what is held says what is owed and what was missed, and the
# estimate says what that read says. Times come from the trading calendar, never from a session literal.


async def _held_spread_read_at(world, monkeypatch: pytest.MonkeyPatch, now_ms: int) -> Any:
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35"), long=_quote("2.20", "2.24"))
    estimate = read_exit_estimate(books, "bot-a", now_ms=now_ms)
    assert estimate is not None
    return estimate


async def test_a_close_the_session_ended_on_is_still_told_with_its_time_after_the_session_that_missed_it(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An hour after Tuesday's close the spread is still held: the close due before it was missed, and
    nothing is owed in a session that has ended. The missed close is the one line the page has."""
    after_the_close = session_close_ms_utc(TUESDAY) + HOUR_MS

    estimate = await _held_spread_read_at(world, monkeypatch, after_the_close)

    assert estimate.conditions == (
        FEES_LINE, BUY_BACK_ALONE_LINE, _missed(TUESDAY, now_ms=after_the_close), CUTOFF_LINE,
    )


@pytest.mark.parametrize("offset_ms", [-HALF_AN_HOUR_MS, HALF_AN_HOUR_MS], ids=["before the open", "after the open"])
async def test_the_next_sessions_close_is_owed_beside_the_one_missed_and_neither_hides_the_other(
    world, monkeypatch: pytest.MonkeyPatch, offset_ms: int,
) -> None:
    """The morning after, Wednesday's close is owed and Tuesday's was missed. The Clerk's own read reports
    the missed one first, and the estimate states both."""
    now = session_open_ms_utc(WEDNESDAY) + offset_ms

    estimate = await _held_spread_read_at(world, monkeypatch, now)

    assert estimate.conditions == (
        FEES_LINE, BUY_BACK_ALONE_LINE, _missed(TUESDAY, now_ms=now), _closes_itself(WEDNESDAY, now_ms=now),
        CUTOFF_LINE,
    )


async def test_a_close_the_clerk_has_stopped_making_by_itself_is_said_to_be_the_owners(
    world, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its EXIT_STUCK episode is open on the books: the Clerk sends no sale of its own for this bot."""
    books, _ = world
    await opened(world, RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35"), long=_quote("2.20", "2.24"))
    raise_uncertainty(
        books, strategy_instance_id="bot-a", reason_code=EXIT_STUCK_REASON_CODE,
        headline="A stuck EXIT exhausted automatic re-drives",
        explanation="2 contracts remain attributed after 3 automatic EXIT re-drives.",
        operator_impact="New exposure stays on hold for this strategy and automatic re-drives stopped.",
        next_step="Run Reconcile now, then execute the presented safe flatten.",
        cause_facts=ExitStuckCause(
            symbol=C580, attributed_qty=2.0, redrive_count=3, first_observed_at_ms=NOON,
        ).to_mapping(),
        severity="error",
    )

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None
    when = et_when_words(session_close_ms_utc(TUESDAY) - CloseTerms().sold_leg_close_lead_ms, now_ms=NOON)
    assert estimate.conditions == (
        FEES_LINE,
        BUY_BACK_ALONE_LINE,
        f"A close of the position is due at {when}: {NOT_OVERNIGHT}. This system has stopped exiting this bot "
        "by itself, so the close is yours to make.",
        CUTOFF_LINE,
    )


async def test_a_position_held_with_no_readable_entry_says_its_close_could_not_be_read_and_logs_why(
    books: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A sold contract on the books that no entry of the bot opened: the Clerk's read of what is owed cannot
    tell, and the estimate says so. It is never taken for a close not owed."""
    _hold(books, C580, -1)
    _supervisor(monkeypatch, short=_quote("5.30", "5.35"))

    with caplog.at_level(logging.ERROR):
        estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None
    assert estimate.conditions == (FEES_LINE, BUY_BACK_ALONE_LINE, CLOSE_UNREAD_LINE, CUTOFF_LINE)
    assert any(getattr(record, "action", "") == "scheduled_close_holding_unreadable" for record in caplog.records)


async def test_a_bought_position_that_owes_no_close_before_its_expiry_has_none_named(
    books: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bought call expiring a week and more later: nothing is owed today, and none is said to be."""
    _hold(books, C585, 1)
    _supervisor(monkeypatch, long=_quote("2.20", "2.24"))

    estimate = read_exit_estimate(books, "bot-a", now_ms=NOON)

    assert estimate is not None and estimate.conditions == (FEES_LINE, CUTOFF_LINE)


# ---- what nothing holds, and what the books cannot count ------------------------------------------------------


@pytest.fixture
def books(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    repo = open_option_books(tmp_path, bots={"bot-a": "SPY"})
    try:
        yield repo
    finally:
        repo.close()


def _hold(repo: ClerkSqliteRepository, symbol: str, contracts: int, *, decision: str = "d") -> None:
    side = "buy" if contracts > 0 else "sell"
    accepted = accept_option_order(
        repo, terms=ContractTerms(symbol, 100), side=side, contracts=abs(contracts), decision_id=decision,
        strategy_instance_id="bot-a",
    )
    fill(repo, accepted, execution_id=f"x-{decision}", symbol=symbol, side=side.upper(), quantity=abs(contracts), price=1.0)


def test_a_bot_holding_no_option_contract_has_no_estimate(books: ClerkSqliteRepository) -> None:
    assert read_exit_estimate(books, "bot-a", now_ms=NOON) is None


def test_holdings_the_books_cannot_count_in_whole_contracts_are_refused_and_not_rounded(
    books: ClerkSqliteRepository,
) -> None:
    _hold(books, C585, 1)
    with books.write_fence() as conn:
        conn.execute("UPDATE positions SET attributed_qty = 1.5 WHERE strategy_instance_id = 'bot-a'")

    with pytest.raises(BudgetUnavailable, match="cannot be counted"):
        read_exit_estimate(books, "bot-a", now_ms=NOON)


# ---- a Dry Run: the quotes it kept ---------------------------------------------------------------------------


@pytest.fixture
def dry_run(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    repo = open_option_books(tmp_path, bots={"bot-a": "SPY"}, account_id="sim:exit-estimate")
    try:
        yield repo
    finally:
        repo.close()


def _kept(repo: ClerkSqliteRepository) -> KeptOptionQuotes:
    return KeptOptionQuotes.beside(repo.db_path)


def test_a_dry_runs_estimate_is_priced_from_the_newest_quote_kept_as_a_simulated_close_is(
    dry_run: ClerkSqliteRepository,
) -> None:
    """One bought 585 call, three contracts: sold at the bid of the quote received last, 2.30 × 300 = $690.00. An
    older quote is passed over, and so is one received after the instant asked about."""
    _hold(dry_run, C585, 3)
    kept = _kept(dry_run)
    kept.keep(C585, _quote("2.10", "2.14", age_ms=30_000), kept_at_ms=NOON - 30_000)
    kept.keep(C585, _quote("2.30", "2.34", age_ms=5_000), kept_at_ms=NOON - 5_000)
    kept.keep(C585, _quote("9.00", "9.04", age_ms=-5_000), kept_at_ms=NOON + 5_000)
    read_quotes_ahead(dry_run)

    estimate = read_exit_estimate(dry_run, "bot-a", now_ms=NOON)

    assert estimate is not None
    assert (estimate.net_close.state, estimate.net_close.amount_cents, estimate.net_close.source) == (
        "known", -69_000, "Quotes kept for this Dry Run",
    )
    assert [(term.label, term.amount_cents) for term in estimate.net_close.terms] == [
        (f"Sell 3 {C585} at the bid", -69_000),
    ]
    assert (estimate.gross_buy_back.state, estimate.gross_buy_back.amount_cents) == ("known", 0)
    assert [quality.age_ms for quality in estimate.quotes] == [5_000]


def test_a_dry_run_with_no_quote_kept_yet_says_none_has_been(dry_run: ClerkSqliteRepository) -> None:
    _hold(dry_run, C585, 1)

    estimate = read_exit_estimate(dry_run, "bot-a", now_ms=NOON)

    assert estimate is not None
    assert (estimate.net_close.state, estimate.net_close.amount_cents) == ("unknown", None)
    assert f"{C585}: no quote has been kept for it yet" in estimate.net_close.source
    assert "Quotes kept for this Dry Run" in estimate.net_close.source


def test_quotes_kept_that_cannot_be_read_make_the_figures_unknown_and_never_fail_the_page(
    dry_run: ClerkSqliteRepository, caplog: pytest.LogCaptureFixture,
) -> None:
    _hold(dry_run, C585, 1)
    kept = _kept(dry_run)
    # A whole line that is not a kept quote: the file is damaged, not half-written.
    kept.path.parent.mkdir(parents=True, exist_ok=True)
    kept.path.write_text('{"not": "a kept quote"}\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        read_quotes_ahead(dry_run)
        estimate = read_exit_estimate(dry_run, "bot-a", now_ms=NOON)

    assert estimate is not None and estimate.net_close.state == "unknown"
    assert "the option quotes kept for this Dry Run cannot be read" in estimate.net_close.source
    assert any(getattr(record, "action", "") == "kept_option_quotes_unreadable" for record in caplog.records)


def test_a_real_account_reads_nothing_ahead(books: ClerkSqliteRepository) -> None:
    """It has no kept quotes: no file is looked for beside its books."""
    read_quotes_ahead(books)

    assert not _kept(books).path.exists()
