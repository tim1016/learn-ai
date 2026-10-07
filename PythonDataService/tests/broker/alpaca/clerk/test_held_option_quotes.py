"""The quotes a Dry Run's Clerk keeps for the option contracts its bots hold, with no runner to hand them over.

The books here are a list the test changes, the IBKR supervisor is what the
keeping reads of it, and the quotes are kept in a real file, as an account's
are.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.held_option_quotes import HELD_QUOTE_MAX_AGE_MS, KEEP_EVERY_MS, HeldOptionQuotes
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import Chain, FeedState, WatchedContract
from app.options.watch import LineClass, LineWant, plan_lines

# The resolver's own scenes, for a Tuesday morning and its contracts.
from tests.options import test_resolver as scenes

NOW = scenes._at(scenes._TUESDAY, 10, 31, 2)
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
        self.kept = KeptOptionQuotes.of_account(artifacts_root=tmp_path, account_id=account_id)
        self.followed = held.follow(account_id, books=self._books, kept=self.kept)

    def _books(self) -> list[str]:
        self.reads += 1
        if self.unreadable:
            raise RuntimeError("the books could not be read")
        return list(self.holds)

    def last_handed_over(self, contract: OptionContract, at_ms: int) -> tuple[Decimal | None, Decimal | None, int] | None:
        """The bid, the ask and the hand-over time of the quote last kept for ``contract``."""
        stated = self.kept.last_stated(contract.occ_symbol, at_or_before_ms=at_ms)
        return None if stated is None else (stated.quote.bid, stated.quote.ask, stated.kept_at_ms)


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
    "unusable",
    [
        pytest.param(lambda now: _quote("4.08", "4.12", now - HELD_QUOTE_MAX_AGE_MS - 1), id="gone quiet"),
        pytest.param(lambda now: _quote("4.08", "4.12", now - 300, data_type=QuoteDataType.DELAYED), id="delayed data"),
        pytest.param(lambda now: _quote("4.08", "4.12", now - 300, generation=GENERATION - 1), id="from before a reconnect"),
        pytest.param(lambda now: _quote("4.08", None, now - 300), id="one side only"),
        pytest.param(lambda now: _quote("4.20", "4.12", now - 300), id="crossed"),
        pytest.param(lambda now: _quote("4.08", "0", now - 300), id="nothing offered"),
    ],
)
async def test_a_quote_no_decision_could_use_is_not_handed_over_and_the_next_usable_one_is_at_once(
    held: HeldOptionQuotes, clock: _Clock, tmp_path: Path, unusable
) -> None:
    """A hand-over says "this is the newest held", and a mark is judged by the hand-over's own time. Restating a
    line that has gone quiet, or one from before a reconnect, would pass it off as a fresh mark."""
    account, ibkr = _Account(held, tmp_path), _Supervisor()
    account.holds = [CALL.occ_symbol]
    await held.read_books()
    ibkr.quotes[CALL.occ_symbol] = unusable(NOW)

    not_kept = await held.keep(ibkr)
    nothing = account.last_handed_over(CALL, NOW)
    # A second later a live quote arrives. Nothing was handed over, so nothing is waited for.
    clock.value += 1_000
    ibkr.quotes[CALL.occ_symbol] = _quote("4.08", "4.12", clock.value - 100)

    assert (not_kept, nothing) == (0, None)
    assert await held.keep(ibkr) == 1
    assert account.last_handed_over(CALL, clock.value) == (Decimal("4.08"), Decimal("4.12"), NOW + 1_000)


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
    clock.value = last_trade + KEEP_EVERY_MS
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


def test_a_quote_is_kept_every_so_often_never_every_zero_seconds() -> None:
    with pytest.raises(ValueError, match="every so often"):
        HeldOptionQuotes(keep_every_ms=0)
