"""The quotes a Dry Run's Clerk keeps for the option contracts its bots hold, with no runner to hand them over.

The books here are a list the test changes, the IBKR supervisor is what the
keeping reads of it, and the quotes are kept in a real file, as an account's
are.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import held_option_quotes
from app.broker.alpaca.clerk.held_option_quotes import FAULT_REPEAT_MS, KEEP_EVERY_MS, HeldOptionQuotes
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import Chain, FeedState, WatchedContract
from app.options.watch import LineClass, LineWant, plan_lines

# The resolver's own scenes, for a Tuesday morning and its contracts.
from tests.options import test_resolver as scenes

NOW = scenes._at(scenes._TUESDAY, 10, 31, 2)
TUESDAY_OPEN, TUESDAY_CLOSE = session_open_ms_utc(scenes._TUESDAY), session_close_ms_utc(scenes._TUESDAY)
MINUTE = 60_000
GENERATION = 3
CALL = scenes._contract("call", 585)
PUT = scenes._contract("put", 580)
QQQ_CALL = OptionContract.of(root="QQQ", expiry_date=scenes._FRIDAY, right="call", strike=500)
ACCOUNT = "sim:holds-a-call"


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Supervisor:
    """What the keeping reads of the IBKR supervisor: the option feed's state, and the quote held for each contract."""

    def __init__(self) -> None:
        self.generation = GENERATION
        self.unhealthy: str | None = None
        self.watches_options = True
        self.quotes: dict[str, HeldQuote] = {}

    def option_feed(self) -> FeedState | None:
        return FeedState(self.generation, self.unhealthy) if self.watches_options else None

    def option_chain(self, underlying: str) -> Chain:
        return Chain(
            listings=(),
            watched={
                symbol: WatchedContract(quote, None)
                for symbol, quote in self.quotes.items()
                if OptionContract.from_occ_symbol(symbol).root == underlying
            },
        )


class _Account:
    """One Dry Run account: the contracts its books say it holds, and the file its quotes are kept in."""

    def __init__(self, held: HeldOptionQuotes, tmp_path: Path, account_id: str = ACCOUNT) -> None:
        self.holds: list[str] = []
        self.reads = 0
        self.unreadable = False
        # Set: the books do not answer until it is released, as when another thread holds their lock.
        self.blocked: threading.Event | None = None
        self.kept = KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=account_id)
        self.followed = held.follow(account_id, books=self._books, kept=self.kept)

    def _books(self) -> list[str]:
        self.reads += 1
        if self.blocked is not None:
            self.blocked.wait(30)
        if self.unreadable:
            raise RuntimeError("the books could not be read")
        return list(self.holds)

    def last_handed_over(self, contract: OptionContract, at_ms: int) -> tuple[Decimal | None, Decimal | None, int] | None:
        """The bid, the ask and the hand-over time of the quote last kept for ``contract``."""
        stated = self.kept.last_stated(contract.occ_symbol, at_or_before_ms=at_ms)
        return None if stated is None else (stated.quote.bid, stated.quote.ask, stated.kept_at_ms)

    def mark(self, contract: OptionContract, at_ms: int) -> tuple[Decimal | None, Decimal | None, int] | None:
        """The bid, the ask and the hand-over time of the quote ``contract`` would be marked from, as the
        account's own reading asks for it."""
        stated = self.kept.stated_mark(contract.occ_symbol, at_or_before_ms=at_ms)
        return None if stated is None else (stated.quote.bid, stated.quote.ask, stated.kept_at_ms)

    def rows(self) -> int:
        """How many quotes the account's file holds."""
        return len(self.kept.path.read_text().splitlines()) if self.kept.path.exists() else 0


def _quote(bid: str | None, ask: str | None, received_ms: int, **changes: object) -> HeldQuote:
    held = HeldQuote(
        bid=None if bid is None else QuoteSide(Decimal(bid), received_ms),
        ask=None if ask is None else QuoteSide(Decimal(ask), received_ms),
        data_type=QuoteDataType.LIVE,
        generation=GENERATION,
    )
    return replace(held, **changes)


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW)


@pytest.fixture
def held(clock: _Clock) -> HeldOptionQuotes:
    return HeldOptionQuotes(clock=clock)


# ---- What is handed over, and when ------------------------------------------------


async def test_the_newest_quote_of_a_held_contract_is_kept_and_again_fifteen_seconds_on(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)

    first = await held.keep(ibkr)
    clock.value += KEEP_EVERY_MS - 1
    ibkr.quotes[CALL.occ_symbol] = _quote("4.30", "4.34", clock.value - 200)
    too_soon = await held.keep(ibkr)
    still_the_first = account.last_handed_over(CALL, clock.value)
    clock.value += 1
    due = await held.keep(ibkr)

    assert (first, too_soon, due) == (1, 0, 1)
    assert still_the_first == (Decimal("4.08"), Decimal("4.12"), NOW)
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.30"), Decimal("4.34"), NOW + KEEP_EVERY_MS)


@pytest.mark.parametrize(
    "no_mark",
    [
        pytest.param(lambda now: _quote("4.08", "4.12", TUESDAY_OPEN - 1), id="last ticked before today's session"),
        pytest.param(
            lambda now: replace(_quote("4.08", "4.12", now - 300), bid=QuoteSide(Decimal("4.08"), TUESDAY_OPEN - 1)),
            id="one side last ticked before today's session",
        ),
        pytest.param(lambda now: _quote("4.08", "4.12", now + 1), id="stamped after now"),
        pytest.param(lambda now: _quote("4.08", "4.12", now - 300, data_type=QuoteDataType.DELAYED), id="delayed data"),
        pytest.param(lambda now: _quote("4.08", "4.12", now - 300, data_type=QuoteDataType.FROZEN), id="frozen data"),
        pytest.param(lambda now: _quote("4.08", "4.12", now - 300, generation=GENERATION - 1), id="from before a reconnect"),
        pytest.param(lambda now: _quote("4.08", None, now - 300), id="one side only"),
        pytest.param(lambda now: _quote("4.20", "4.12", now - 300), id="crossed"),
        pytest.param(lambda now: _quote("4.08", "0", now - 300), id="nothing offered"),
    ],
)
async def test_a_quote_that_is_no_mark_is_not_handed_over_and_the_next_whole_one_is_at_once(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, no_mark
) -> None:
    """A hand-over says "this is the newest held". A quote from before a reconnect, from another day, of
    delayed data, or with a side missing is not that, however lately it was read."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = no_mark(NOW)

    not_kept = await held.keep(ibkr)
    nothing = account.mark(CALL, NOW)
    # A second later a live quote arrives. Nothing was handed over, so nothing is waited for.
    clock.value += 1_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 100)

    assert (not_kept, nothing) == (0, None)
    assert await held.keep(ibkr) == 1
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), NOW + 1_000)


async def test_a_quiet_contracts_standing_quote_is_its_mark_at_every_hand_over_with_its_own_receipt_times(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """Neither side has ticked for ten minutes: nobody has changed the bid. It is still what the call could be
    sold for, on a healthy line of the current generation, so it is stated again every fifteen seconds. Each
    row says when its sides were really received, so nothing takes the quote for a fresh one."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    last_ticked = NOW - 10 * MINUTE
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", last_ticked)

    handed_over = []
    for _ in range(4):
        handed_over.append(await held.keep(ibkr))
        stated = account.kept.last_stated(CALL.occ_symbol, at_or_before_ms=clock.value, with_side="bid")
        assert stated is not None
        assert (stated.quote.bid, stated.kept_at_ms) == (Decimal("4.08"), clock.value)
        assert (stated.quote.bid_received_ms, stated.quote.ask_received_ms) == (last_ticked, last_ticked)
        clock.value += KEEP_EVERY_MS

    assert handed_over == [1, 1, 1, 1]
    # By receipt it is one quote, ten minutes old and older by the minute.
    newest = account.kept.newest(CALL.occ_symbol, at_or_before_ms=clock.value)
    assert newest is not None and newest.received_ms == last_ticked


async def test_a_quiet_contract_has_its_mark_stated_as_the_session_closes(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """The call last ticked at half past three. The hand-over made as the session closes is that session's mark."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.20", "4.24", TUESDAY_CLOSE - 30 * MINUTE)
    clock.value = TUESDAY_CLOSE

    assert await held.keep(ibkr) == 1
    assert account.last_handed_over(CALL, TUESDAY_CLOSE) == (Decimal("4.20"), Decimal("4.24"), TUESDAY_CLOSE)


@pytest.mark.parametrize(
    "when",
    [
        pytest.param(TUESDAY_OPEN - 1, id="a millisecond before the open"),
        pytest.param(TUESDAY_CLOSE + 1, id="a millisecond after the close"),
        pytest.param(scenes._at(scenes._FRIDAY, 12, 0) + 24 * 3_600_000, id="a Saturday"),
    ],
)
async def test_outside_the_session_nothing_is_restated(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, when: int
) -> None:
    """A line still holds the last quote of the session after it closes, and all night. Restating it would
    pass yesterday's bid off as a mark of a market that is not open."""
    far = OptionContract.of(root="SPY", expiry_date=scenes._FRIDAY.replace(year=scenes._FRIDAY.year + 1), right="call", strike=585)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [far.occ_symbol]
    await held.read_books()
    clock.value = when
    ibkr.quotes[far.occ_symbol] = _quote("4.08", "4.12", when - 300)

    assert await held.keep(ibkr) == 0
    assert account.last_handed_over(far, when) is None
    assert [request.held for request in held.requests()] == [(far,)], "its line is still asked for"


async def test_the_quote_a_line_held_before_a_reconnect_is_no_mark_until_it_ticks_again(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """A reconnect, or a recovery from a data loss, moves the feed's generation. What the line held before it
    is never stated again, though it is seconds old; the first whole quote of the new generation is."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    before = await held.keep(ibkr)

    ibkr.generation += 1
    clock.value += KEEP_EVERY_MS
    after_the_reconnect = await held.keep(ibkr)
    clock.value += KEEP_EVERY_MS
    still_waiting = await held.keep(ibkr)
    ibkr.quotes[CALL.occ_symbol] = _quote("4.00", "4.04", clock.value - 100, generation=ibkr.generation)
    ticked_again = await held.keep(ibkr)

    assert (before, after_the_reconnect, still_waiting, ticked_again) == (1, 0, 0, 1)
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.00"), Decimal("4.04"), clock.value)
    # Nothing between the reconnect and the tick states the old quote as current.
    assert account.last_handed_over(CALL, clock.value - 1) == (Decimal("4.08"), Decimal("4.12"), NOW)


# ---- A line that shows one side: put back as it stands, and never a mark ------------


@pytest.mark.parametrize(
    ("bid", "ask", "a_sale_is_priced_at"),
    [
        pytest.param(None, "2.04", None, id="only the ask has ticked"),
        pytest.param("2.00", None, Decimal("2.00"), id="only the bid has ticked"),
    ],
)
async def test_a_line_that_shows_one_side_since_a_reconnect_is_put_back_as_it_stands_and_is_never_a_mark(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, bid: str | None, ask: str | None,
    a_sale_is_priced_at: Decimal | None,
) -> None:
    """Marked at $4.08 bid. Five seconds on, after a reconnect, one side of the line has ticked and the other
    has not. That is the line's last word, so it is in the account's file at once, as of its own receipt:
    a close is priced from it alone, and the $4.08 from before the reconnect prices nothing. It is no mark,
    then or later, and it is written once, not at every pass."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    assert await held.keep(ibkr) == 1

    ibkr.generation += 1
    clock.value += 5_000
    received = clock.value - 200
    ibkr.quotes[CALL.occ_symbol] = _quote(bid, ask, received, generation=ibkr.generation)
    stated = await held.keep(ibkr)
    newest = account.kept.newest(CALL.occ_symbol, at_or_before_ms=clock.value)
    rows_after_the_first_pass = account.rows()
    marks = []
    for _ in range(2 * KEEP_EVERY_MS // 1_000):
        marks.append(account.mark(CALL, clock.value))
        clock.value += 1_000
        stated += await held.keep(ibkr)

    assert stated == 0, "a quote with one side was stated as a mark"
    assert newest is not None
    assert (newest.quote.bid, newest.quote.ask) == (None if bid is None else Decimal(bid), None if ask is None else Decimal(ask))
    assert newest.kept_at_ms == received, "it is put back as of its own receipt, never as of now"
    # The only mark there can be is the whole quote stated before the reconnect, for as long as that stands.
    assert set(marks) <= {None, (Decimal("4.08"), Decimal("4.12"), NOW)}
    sale = account.kept.closing_quote(CALL.occ_symbol, side="sell", at_ms=clock.value)
    assert (None if sale is None else sale.quote.bid) == a_sale_is_priced_at
    assert account.kept.prices_a_close(CALL.occ_symbol, side="sell", at_ms=clock.value) is (a_sale_is_priced_at is not None)
    assert account.rows() == rows_after_the_first_pass == 2, "what the line shows was written again though it had not changed"
    # The other side ticks: the quote is whole, and is the contract's mark at once.
    ibkr.quotes[CALL.occ_symbol] = _quote("2.00", "2.04", clock.value - 100, generation=ibkr.generation)
    assert await held.keep(ibkr) == 1
    assert account.mark(CALL, clock.value) == (Decimal("2.00"), Decimal("2.04"), clock.value)


async def test_a_line_that_shows_another_price_on_its_one_side_is_put_back_again(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """The ask moves from $2.04 to $2.10 while nothing is bid yet. The file's newest quote follows the line."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote(None, "2.04", NOW - 300)
    await held.keep(ibkr)
    clock.value += 1_000
    ibkr.quotes[CALL.occ_symbol] = _quote(None, "2.10", clock.value - 100)

    assert await held.keep(ibkr) == 0
    newest = account.kept.newest(CALL.occ_symbol, at_or_before_ms=clock.value)
    assert newest is not None and (newest.quote.ask, newest.kept_at_ms) == (Decimal("2.10"), clock.value - 100)
    assert account.mark(CALL, clock.value) is None


@pytest.mark.parametrize(
    "not_the_lines_word",
    [
        pytest.param(lambda now: _quote(None, "2.04", now - 300, generation=GENERATION - 1), id="from before a reconnect"),
        pytest.param(lambda now: _quote(None, "2.04", now - 300, data_type=QuoteDataType.DELAYED), id="delayed data"),
        pytest.param(lambda now: _quote(None, "2.04", now - 300, data_type=QuoteDataType.FROZEN), id="frozen data"),
        pytest.param(lambda now: _quote(None, "2.04", TUESDAY_OPEN - 1), id="last ticked before today's session"),
        pytest.param(lambda now: _quote(None, "2.04", now + 1), id="stamped after now"),
        pytest.param(lambda now: _quote(None, "0", now - 300), id="an ask of nothing and no bid"),
        pytest.param(lambda now: _quote("-1", None, now - 300), id="a bid that is no price"),
    ],
)
async def test_a_one_sided_quote_with_anything_else_against_it_is_not_put_back(
    held: HeldOptionQuotes, tmp_path: Path, not_the_lines_word
) -> None:
    """Only what a healthy line of the current generation shows today is its last word. A one-sided quote from
    before a reconnect, of delayed data, or from another day is written nowhere, and prices nothing."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = not_the_lines_word(NOW)

    assert await held.keep(ibkr) == 0
    assert account.rows() == 0 and account.kept.newest(CALL.occ_symbol) is None


@pytest.mark.parametrize("trouble", ["the feed is not healthy", "no option quote source", "no line for the contract"])
async def test_with_no_quote_to_read_nothing_is_handed_over(
    held: HeldOptionQuotes, tmp_path: Path, trouble: str
) -> None:
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    if trouble == "the feed is not healthy":
        ibkr.unhealthy = "the IBKR market-data connection is not up"
    elif trouble == "no option quote source":
        ibkr.watches_options = False
    else:
        del ibkr.quotes[CALL.occ_symbol]

    assert await held.keep(ibkr) == 0
    assert account.last_handed_over(CALL, NOW) is None


async def test_a_bid_of_nothing_is_handed_over_because_it_is_what_the_contract_could_be_sold_for(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    """Left out, an older quote with a bid would go on pricing a sale nobody would take."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("0", "0.05", NOW - 300)

    assert await held.keep(ibkr) == 1
    assert account.last_handed_over(CALL, NOW) == (Decimal(0), Decimal("0.05"), NOW)
    assert account.kept.prices_a_close(CALL.occ_symbol, side="sell", at_ms=NOW) is False


async def test_each_account_is_handed_the_quotes_of_what_it_holds_and_no_other(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    one, other, ibkr = _Account(held, tmp_path), _Account(held, tmp_path, "sim:holds-a-put"), _Supervisor()
    one.holds, other.holds = [CALL.occ_symbol], [PUT.occ_symbol, QQQ_CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes = {
        CALL.occ_symbol: _quote("4.08", "4.12", NOW - 300),
        PUT.occ_symbol: _quote("1.10", "1.14", NOW - 300),
        QQQ_CALL.occ_symbol: _quote("2.00", "2.04", NOW - 300),
    }

    assert await held.keep(ibkr) == 3
    assert one.last_handed_over(CALL, NOW) == (Decimal("4.08"), Decimal("4.12"), NOW)
    assert one.last_handed_over(PUT, NOW) is None
    assert other.last_handed_over(PUT, NOW) == (Decimal("1.10"), Decimal("1.14"), NOW)
    assert other.last_handed_over(QQQ_CALL, NOW) == (Decimal("2.00"), Decimal("2.04"), NOW)
    assert other.last_handed_over(CALL, NOW) is None


async def test_a_quote_file_that_cannot_be_written_is_tried_again_on_the_next_round(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    writes = account.kept.keep_all

    def full_disk(*_: object, **__: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(account.kept, "keep_all", full_disk)
    failed = await held.keep(ibkr)
    monkeypatch.setattr(account.kept, "keep_all", writes)
    clock.value += 1_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 300)

    assert failed == 0
    assert await held.keep(ibkr) == 1


# ---- What is asked for ------------------------------------------------------------


async def test_held_contracts_are_asked_for_once_each_by_their_underlying(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    one, other = _Account(held, tmp_path), _Account(held, tmp_path, "sim:holds-more")
    one.holds, other.holds = [PUT.occ_symbol, CALL.occ_symbol], [CALL.occ_symbol, QQQ_CALL.occ_symbol]
    await held.read_books()

    qqq, spy = held.requests()

    assert (qqq.underlying, qqq.held, qqq.plan) == ("QQQ", (QQQ_CALL,), None)
    assert (spy.underlying, spy.held, spy.plan) == ("SPY", (CALL, PUT), None)
    assert held.held() == {
        ACCOUNT: (CALL.occ_symbol, PUT.occ_symbol), "sim:holds-more": (QQQ_CALL.occ_symbol, CALL.occ_symbol),
    }


async def test_a_held_contract_takes_a_line_before_any_bots_window_does(held: HeldOptionQuotes, tmp_path: Path) -> None:
    """One line in the budget, and a running bot's window wants it. The contract somebody holds has it."""
    account = _Account(held, tmp_path)
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    (request,) = held.requests()
    wanted_by_a_window = scenes._contract("call", 590)

    lines = plan_lines(
        [LineWant(contract, LineClass.HELD) for contract in request.held]
        + [LineWant(wanted_by_a_window, LineClass.CANDIDATE)],
        budget=1, ceiling=None, open_lines=(),
    )

    assert lines.lines == (CALL,)
    assert wanted_by_a_window.occ_symbol in lines.without


async def test_a_contract_past_its_last_session_is_neither_asked_for_nor_kept(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """Nothing trades in it any more. A quote for it would be a price of nothing."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    expires_today = scenes._contract("call", 585, scenes._TUESDAY)
    account.holds = [expires_today.occ_symbol, CALL.occ_symbol]
    await held.read_books()
    last_trade = session_close_ms_utc(scenes._TUESDAY)

    clock.value = last_trade - 1
    ibkr.quotes = {symbol: _quote("0.40", "0.44", clock.value - 300) for symbol in account.holds}
    (in_its_last_minute,) = held.requests()
    kept_in_its_last_minute = await held.keep(ibkr)
    # The next morning the other contract still trades, and the expired one is nobody's to ask for.
    clock.value = scenes._at(scenes._WEDNESDAY, 10, 0)
    ibkr.quotes = {symbol: _quote("0.40", "0.44", clock.value - 300) for symbol in account.holds}
    (after_it,) = held.requests()

    assert in_its_last_minute.held == (expires_today, CALL)
    assert kept_in_its_last_minute == 2
    assert after_it.held == (CALL,)
    assert await held.keep(ibkr) == 1
    assert account.last_handed_over(expires_today, clock.value) == (Decimal("0.40"), Decimal("0.44"), last_trade - 1)


# ---- Whose books are read, and what stands when they close ------------------------


async def test_the_books_are_read_again_on_every_round_and_a_contract_no_longer_held_is_let_go(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    account = _Account(held, tmp_path)
    await held.read_books()
    nothing_held = held.requests()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    holding = held.requests()
    account.holds = []
    await held.read_books()

    assert nothing_held == ()
    assert [request.held for request in holding] == [(CALL,)]
    assert held.requests() == ()
    assert account.reads == 3


async def test_what_a_clerk_held_as_it_closed_goes_on_being_asked_for_and_kept(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """A Dry Run's Clerk is closed when its run ends. The contract its run could not sell is still quoted, so
    the next thing to try the sale finds a recent quote kept."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    # Bought a moment ago: no round has read the books since.
    account.holds = [CALL.occ_symbol]

    account.followed.close()
    read_at_the_close = account.reads
    clock.value += 60_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.50", "4.54", clock.value - 300)
    await held.read_books()

    assert [request.held for request in held.requests()] == [(CALL,)]
    assert await held.keep(ibkr) == 1
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.50"), Decimal("4.54"), NOW + 60_000)
    assert account.kept.prices_a_close(CALL.occ_symbol, side="sell", at_ms=clock.value) is True
    assert (read_at_the_close, account.reads) == (1, 1), "a closed Clerk's books are not read again"


async def test_an_account_found_flat_as_its_clerk_closes_is_forgotten(held: HeldOptionQuotes, tmp_path: Path) -> None:
    account = _Account(held, tmp_path)
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    account.holds = []

    account.followed.close()

    assert (held.requests(), held.held()) == ((), {})


async def test_an_account_opened_again_is_read_from_its_books_again_and_is_handed_nothing_early(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path
) -> None:
    """A page that opens a stopped Dry Run's account five seconds after the last hand-over does not bring the
    next one forward: the file would grow by a row for every look."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    await held.keep(ibkr)
    account.followed.close()

    reopened = _Account(held, tmp_path)
    reopened.holds = [CALL.occ_symbol]
    clock.value += 5_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.30", "4.34", clock.value - 300)
    await held.read_books()
    five_seconds_on = await held.keep(ibkr)
    clock.value += KEEP_EVERY_MS - 5_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.30", "4.34", clock.value - 300)

    assert (account.reads, reopened.reads) == (2, 1), "the closed handle's books are not read again"
    assert five_seconds_on == 0
    assert await held.keep(ibkr) == 1
    reopened.holds = []
    await held.read_books()
    assert held.requests() == (), "and what its books now say is what stands"


async def test_books_that_cannot_be_read_leave_what_was_held_a_moment_ago(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    """A held contract keeps its line while the asking fails."""
    account = _Account(held, tmp_path)
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    account.unreadable = True

    await held.read_books()
    still_asked_for = held.requests()
    account.followed.close()

    assert [request.held for request in still_asked_for] == [(CALL,)]
    assert [request.held for request in held.requests()] == [(CALL,)], "nor does a last read that fails let it go"


async def test_a_read_that_answers_after_its_clerk_closed_does_not_overwrite_the_closes_last_word(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    """A round's read of the books is stuck behind a lock, begun while the account held nothing. The bot buys
    a call and its Clerk closes: the last word is "holds the call". Then the stuck read answers "nothing".
    It was asked before the close, so it is not taken: the call keeps its line and its mark."""
    kept = KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=ACCOUNT)
    holds: list[str] = []
    stuck, reads = threading.Event(), []

    def books() -> list[str]:
        seen = list(holds)
        reads.append(seen)
        if len(reads) == 1:
            # The round's own read, in a worker thread: it saw the books as they were, and answers late.
            stuck.wait(30)
        return seen

    followed = held.follow(ACCOUNT, books=books, kept=kept)
    try:
        held.drive(_Supervisor())
        await _eventually(lambda: len(reads) == 1)
        holds.append(CALL.occ_symbol)
        followed.close()
        last_word = held.held()
    finally:
        stuck.set()
    await held.settled()

    assert last_word == {ACCOUNT: (CALL.occ_symbol,)}
    assert held.held() == {ACCOUNT: (CALL.occ_symbol,)}, "a read asked before the close overwrote its last word"
    assert [request.held for request in held.requests()] == [(CALL,)]


def test_a_quote_is_kept_every_so_often_never_every_zero_seconds() -> None:
    with pytest.raises(ValueError, match="every so often"):
        HeldOptionQuotes(keep_every_ms=0)


async def test_a_handle_closed_after_its_account_was_opened_again_does_not_stop_the_new_opening(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    """An opening that failed part way gives its handle back late. The books now read are the later opening's."""
    stale = _Account(held, tmp_path)
    current = _Account(held, tmp_path)
    current.holds = [CALL.occ_symbol]

    stale.followed.close()
    await held.read_books()

    assert stale.reads == 0
    assert [request.held for request in held.requests()] == [(CALL,)]
    current.holds = []
    current.followed.close()
    current.followed.close()
    assert (held.requests(), current.reads) == ((), 2), "and closing twice reads the books once"


# ---- A round beside the supervisor's pass: begun, never waited for ----------------


LOGGER = "app.broker.alpaca.clerk.held_option_quotes"


def _said(caplog: pytest.LogCaptureFixture, action: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER and getattr(record, "action", None) == action]


async def _eventually(true: object, *, within_s: float = 5.0) -> None:
    """Wait until ``true()`` holds, without waiting for work that is blocked."""
    deadline = time.monotonic() + within_s
    while not true():  # type: ignore[operator]
        assert time.monotonic() < deadline, "it never happened"
        await asyncio.sleep(0.005)


async def test_with_no_dry_run_account_open_a_pass_begins_no_round(held: HeldOptionQuotes, tmp_path: Path) -> None:
    """A process whose bots trade shares pays nothing for this: no task, no thread, no read."""
    ibkr = _Supervisor()
    tasks_before = asyncio.all_tasks()

    held.drive(ibkr)
    nothing_followed = asyncio.all_tasks() == tasks_before
    account = _Account(held, tmp_path)
    account.followed.close()
    read_at_the_close = account.reads
    held.drive(ibkr)

    assert nothing_followed
    assert asyncio.all_tasks() == tasks_before, "a closed account that holds nothing begins no round either"
    assert (account.reads, held.requests()) == (read_at_the_close, ())


async def test_a_pass_begins_a_round_that_reads_the_books_and_keeps_the_marks_and_does_not_wait_for_it(
    held: HeldOptionQuotes, tmp_path: Path
) -> None:
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)

    held.drive(ibkr)
    as_the_pass_returns = (account.reads, held.requests(), account.last_handed_over(CALL, NOW))
    await held.settled()

    assert as_the_pass_returns == (0, (), None), "the pass waited for nothing"
    assert account.reads == 1
    assert [request.held for request in held.requests()] == [(CALL,)]
    assert account.last_handed_over(CALL, NOW) == (Decimal("4.08"), Decimal("4.12"), NOW)


async def test_books_that_do_not_answer_hold_up_no_pass_and_no_mark_and_are_not_read_twice_at_once(
    clock: _Clock, tmp_path: Path
) -> None:
    """One account's books are stuck behind a lock. Every pass returns at once. The round goes on without
    them after its wait, so the contract another account holds keeps its mark. No second read is begun
    beside the stuck one, which would take a worker thread a second for as long as it lasted."""
    held = HeldOptionQuotes(clock=clock, step_budget_ms=40)
    holder, stuck, ibkr = _Account(held, tmp_path), _Account(held, tmp_path, "sim:stuck-books"), _Supervisor()
    holder.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    stuck.blocked = threading.Event()
    reads_before = stuck.reads
    try:
        longest_pass_s = 0.0
        for _ in range(6):
            began = time.monotonic()
            held.drive(ibkr)
            longest_pass_s = max(longest_pass_s, time.monotonic() - began)
            await asyncio.sleep(0.03)
        await _eventually(lambda: holder.last_handed_over(CALL, clock.value) is not None)
        reads_while_stuck = stuck.reads - reads_before
    finally:
        stuck.blocked.set()
    await held.settled()
    stuck.blocked = None
    held.drive(ibkr)
    await held.settled()

    assert longest_pass_s < 0.25, "a pass waited on the books"
    assert reads_while_stuck == 1
    assert holder.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), NOW)
    assert stuck.reads - reads_before == 2, "once the stuck read answered, the next round read the books again"


async def test_a_file_that_cannot_be_written_in_time_holds_up_no_pass_and_is_not_written_twice_at_once(
    clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = HeldOptionQuotes(clock=clock, step_budget_ms=40)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    slow_disk, writes, write = threading.Event(), [], account.kept.keep_all

    def slowly(quotes: object, *, kept_at_ms: int) -> None:
        writes.append(kept_at_ms)
        slow_disk.wait(30)
        write(quotes, kept_at_ms=kept_at_ms)  # type: ignore[arg-type]

    monkeypatch.setattr(account.kept, "keep_all", slowly)
    try:
        longest_pass_s = 0.0
        for _ in range(6):
            began = time.monotonic()
            held.drive(ibkr)
            longest_pass_s = max(longest_pass_s, time.monotonic() - began)
            await asyncio.sleep(0.03)
        begun_while_slow = len(writes)
    finally:
        slow_disk.set()
    await held.settled()

    assert longest_pass_s < 0.25, "a pass waited on the file"
    assert begun_while_slow == 1
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), NOW)
    # The late write is noted as the hand-over it was: the next is due fifteen seconds after it, not at once.
    assert await held.keep(ibkr) == 0


async def test_a_round_that_fails_reaches_no_pass_is_logged_once_and_the_next_pass_begins_another(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The supervisor's own read fails under the keeping. No pass hears of it. It is logged with its stack
    once, not every second, and the keeping is back the moment the fault is."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    feed = ibkr.option_feed

    def broken() -> None:
        raise LookupError("the option lines are part way through a change")

    ibkr.option_feed = broken  # type: ignore[method-assign]
    for _ in range(30):
        held.drive(ibkr)
        await held.settled()
        clock.value += 1_000
    failures = _said(caplog, "held_option_quotes_round_failed")
    ibkr.option_feed = feed  # type: ignore[method-assign]
    held.drive(ibkr)
    await held.settled()

    assert [(record.levelno, record.exc_info is not None) for record in failures] == [(logging.ERROR, True)]
    assert len(_said(caplog, "held_option_quotes_round_again")) == 1
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), clock.value)


async def test_a_round_that_cannot_even_begin_fails_no_pass(
    held: HeldOptionQuotes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]

    def cannot_begin(_source: object) -> None:
        raise RuntimeError("no round can be made")

    monkeypatch.setattr(held, "_one_round", cannot_begin)

    held.drive(ibkr)
    held.drive(ibkr)

    assert len(_said(caplog, "held_option_quotes_round_failed")) == 1


def _never_retrieved(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """What asyncio says of a task that ended with an error nobody took."""
    gc.collect()
    return [record for record in caplog.records if record.name == "asyncio" and "never retrieved" in record.getMessage()]


async def test_an_answer_that_cannot_be_taken_up_is_one_accounts_fault_and_the_accounts_after_it_are_still_read(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The first account's books answer a symbol that is no option contract. The second account's call is
    asked for and marked all the same, round after round. The fault is in this module's log once, with its
    stack and the account's name, and nothing is left for asyncio to report."""
    caplog.set_level(logging.INFO)
    broken, holder, ibkr = _Account(held, tmp_path, "sim:answers-nonsense"), _Account(held, tmp_path), _Supervisor()
    broken.holds, holder.holds = ["NOT A CONTRACT"], [CALL.occ_symbol]
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)

    for _ in range(4):
        held.drive(ibkr)
        await held.settled()
        clock.value += 1_000
    said = _said(caplog, "held_option_books_answer_not_taken")

    assert held.held() == {ACCOUNT: (CALL.occ_symbol,)}
    assert holder.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), NOW)
    assert [(record.levelno, record.exc_info is not None, record.account_id) for record in said] == [
        (logging.ERROR, True, "sim:answers-nonsense"),
    ]
    assert _never_retrieved(caplog) == []
    # Its books answer sense again: it is read, and the fault is said to be over.
    broken.holds = [PUT.occ_symbol]
    held.drive(ibkr)
    await held.settled()
    assert held.held()["sim:answers-nonsense"] == (PUT.occ_symbol,)
    assert len(_said(caplog, "held_option_books_read_again")) == 1


async def test_a_read_of_the_books_that_breaks_is_logged_here_and_the_marks_are_still_kept(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The threads the books are read in cannot take the read. What the account held a moment ago stands and
    is marked. The fault is logged by this module, once, and asyncio is left nothing to report."""
    caplog.set_level(logging.INFO)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    off_the_loop = held_option_quotes.to_thread

    async def no_thread_for_the_books(work: object) -> object:
        if getattr(work, "__name__", "") == "read":
            raise RuntimeError("cannot schedule new futures after shutdown")
        return await off_the_loop(work)  # type: ignore[arg-type]

    monkeypatch.setattr(held_option_quotes, "to_thread", no_thread_for_the_books)
    for _ in range(3):
        held.drive(ibkr)
        await held.settled()
        clock.value += KEEP_EVERY_MS
    broke = _said(caplog, "held_option_books_reading_broke")
    monkeypatch.setattr(held_option_quotes, "to_thread", off_the_loop)
    held.drive(ibkr)
    await held.settled()

    assert [(record.levelno, record.exc_info is not None) for record in broke] == [(logging.ERROR, True)]
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), clock.value)
    assert len(_said(caplog, "held_option_books_reading_again")) == 1
    assert _never_retrieved(caplog) == []


async def test_a_write_that_never_returns_is_said_again_every_five_minutes_and_once_when_it_returns(
    clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The account's file hangs under a write for twelve minutes of passes. One write is begun and no second
    one beside it. The log names the account as the write is found stuck, again at five minutes and at ten,
    and says once that its quotes are kept again: not once at the start and then nothing."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    held = HeldOptionQuotes(clock=clock, step_budget_ms=20)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    stuck_disk, begun, write = threading.Event(), [], account.kept.keep_all

    def never_returns(quotes: object, *, kept_at_ms: int) -> None:
        begun.append(kept_at_ms)
        stuck_disk.wait(30)
        write(quotes, kept_at_ms=kept_at_ms)  # type: ignore[arg-type]

    monkeypatch.setattr(account.kept, "keep_all", never_returns)
    try:
        assert await held.keep(ibkr) == 0
        for _ in range(12 * 60):
            clock.value += 1_000
            ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 300)
            assert await held.keep(ibkr) == 0
        while_stuck = _said(caplog, "held_option_quotes_write_stuck")
        begun_while_stuck = len(begun)
    finally:
        stuck_disk.set()
    await held.settled()

    assert begun_while_stuck == 1
    assert [(record.levelno, record.account_id) for record in while_stuck] == [
        (logging.ERROR, ACCOUNT), (logging.WARNING, ACCOUNT), (logging.WARNING, ACCOUNT),
    ]
    assert [record.still_failing_since_ms for record in while_stuck[1:]] == [NOW + 1_000, NOW + 1_000]
    assert len(_said(caplog, "held_option_quotes_written_again")) == 1


# ---- Stopping, as the supervisor that drives it closes ------------------------------


@pytest.mark.parametrize("stuck_at", ["the read of the books", "a write of the file"])
async def test_stopping_ends_the_round_under_way_at_once_though_a_thread_is_stuck_and_what_was_held_stands(
    clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stuck_at: str
) -> None:
    """The supervisor closes while a round waits on a lock nothing can break. Stopping does not wait for the
    thread: the round's work on the loop is ended, nothing of it is left under way, and what the books last
    said the account holds is still what is asked for when a supervisor drives the keeping again."""
    held = HeldOptionQuotes(clock=clock, step_budget_ms=60_000)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)
    stuck, write = threading.Event(), account.kept.keep_all

    def a_write_that_is_stuck(quotes: object, *, kept_at_ms: int) -> None:
        stuck.wait(30)
        write(quotes, kept_at_ms=kept_at_ms)  # type: ignore[arg-type]

    if stuck_at == "the read of the books":
        account.blocked = stuck
    else:
        monkeypatch.setattr(account.kept, "keep_all", a_write_that_is_stuck)
    try:
        held.drive(ibkr)
        await asyncio.sleep(0.05)
        began = time.monotonic()
        await held.stop()
        took_s = time.monotonic() - began
        # Nothing is left under way on the loop: this returns at once, with the thread still stuck.
        await asyncio.wait_for(held.settled(), timeout=1)
        still_stuck = not stuck.is_set()
    finally:
        stuck.set()
    account.blocked = None

    assert still_stuck and took_s < 1.0, "stopping waited for a thread nothing can stop"
    assert [request.held for request in held.requests()] == [(CALL,)]
    # A supervisor that opens again drives the same keeping, and it works.
    monkeypatch.setattr(account.kept, "keep_all", write)
    clock.value += KEEP_EVERY_MS
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 300)
    held.drive(ibkr)
    await held.settled()
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), clock.value)


async def test_stopping_with_nothing_under_way_does_nothing(held: HeldOptionQuotes, tmp_path: Path) -> None:
    account = _Account(held, tmp_path)
    account.holds = [CALL.occ_symbol]
    await held.read_books()

    await held.stop()

    assert held.held() == {ACCOUNT: (CALL.occ_symbol,)}


# ---- What is logged of a fault that lasts ------------------------------------------


async def test_a_file_that_cannot_be_written_is_logged_once_with_its_stack_then_seldom_and_once_when_it_can(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full disk for six minutes is tried again every second. It is one stack in the log, one reminder
    after five minutes with no stack, and one line when the quotes are kept again: not 360 stacks."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    writes = account.kept.keep_all

    def full_disk(*_: object, **__: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(account.kept, "keep_all", full_disk)
    for _ in range(FAULT_REPEAT_MS // 1_000 + 60):
        ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 300)
        assert await held.keep(ibkr) == 0
        clock.value += 1_000
    while_it_lasted = _said(caplog, "held_option_quotes_not_written")
    monkeypatch.setattr(account.kept, "keep_all", writes)
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 300)
    kept_again = await held.keep(ibkr)

    assert [(record.levelno, record.exc_info is not None) for record in while_it_lasted] == [
        (logging.ERROR, True), (logging.WARNING, False),
    ]
    assert kept_again == 1
    assert len(_said(caplog, "held_option_quotes_written_again")) == 1


async def test_books_that_cannot_be_read_are_logged_once_not_at_every_pass(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    account = _Account(held, tmp_path)
    account.holds = [CALL.occ_symbol]
    account.unreadable = True

    for _ in range(40):
        await held.read_books()
        clock.value += 1_000
    while_it_lasted = _said(caplog, "held_option_books_read_failed")
    account.unreadable = False
    await held.read_books()

    assert [(record.levelno, record.exc_info is not None) for record in while_it_lasted] == [(logging.ERROR, True)]
    assert len(_said(caplog, "held_option_books_read_again")) == 1
    assert [request.held for request in held.requests()] == [(CALL,)]


async def test_a_feed_that_is_down_is_said_once_for_the_feed_not_at_every_pass_for_every_contract(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol, PUT.occ_symbol]
    await held.read_books()
    ibkr.unhealthy = "the IBKR market-data connection is not up"

    for _ in range(40):
        assert await held.keep(ibkr) == 0
        clock.value += 1_000
    while_down = [record for record in caplog.records if record.name == LOGGER]
    ibkr.unhealthy = None
    ibkr.quotes = {symbol: _quote("4.08", "4.12", clock.value - 300) for symbol in account.holds}
    assert await held.keep(ibkr) == 2

    assert [(record.levelno, record.action) for record in while_down] == [(logging.WARNING, "held_option_quotes_no_feed")]
    assert len(_said(caplog, "held_option_quotes_feed_back")) == 1


async def test_a_quiet_contract_is_marked_every_fifteen_seconds_and_logs_nothing(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Its quote stands for an hour. Nothing lapses and returns, so nothing is said of it."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", NOW - 300)

    handed_over = 0
    for _ in range(3_600):
        handed_over += await held.keep(ibkr)
        clock.value += 1_000

    assert handed_over == 3_600_000 // KEEP_EVERY_MS
    assert [record for record in caplog.records if record.name == LOGGER] == []


async def test_a_contract_waiting_for_its_first_ticks_of_the_day_is_said_once_and_is_no_warning(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    clock.value = TUESDAY_OPEN
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", TUESDAY_OPEN - 18 * 3_600_000)

    for _ in range(120):
        assert await held.keep(ibkr) == 0
        clock.value += 1_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.10", "4.14", clock.value - 200)
    assert await held.keep(ibkr) == 1

    waiting = _said(caplog, "held_option_quote_not_kept")
    assert [record.levelno for record in waiting] == [logging.INFO]
    assert len(_said(caplog, "held_option_quote_kept")) == 1
