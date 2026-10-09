"""The quotes a Dry Run's Clerk keeps for the option contracts its bots hold, runner or no runner (ADR 0076).

A Live Shadow's Clerk keeps them the same way, for the same reason: its
order book fills and its account reading marks from quotes kept, and it
sends nothing (ADR 0076 decision 25). Where this module says a Dry Run, it
holds for a Shadow too, but for one thing: a Shadow's runtime is not closed
when a run ends, so its books are read for as long as the Clerk is up.

A Dry Run has no option market of its own. What it knows of a contract's
price is the quotes kept for it (``kept_option_quotes``): a close is priced
from the newest, and an open contract is marked from the last one handed
over. A runner's own two decisions reach the file with their orders: the
entry's quote, stated as the newest held as its order is sent, and the
exit's, put back as its close is priced. Every hand-over after the entry's
own is made here: no runner states a mark between its two decisions.

**What is held is read from the Clerk's books**, never from a runner: a
position in a recorded contract, or an order working in one
(``reads.option_contracts_requiring_quotes``). Each Dry Run's Clerk and each Live
Shadow's is followed while it is composed.

**What is held is asked for** as the supervisor's first class of quote line
(``requests``), so a held contract keeps its line when the runner that bought
it is gone.

**What is held is marked.** A mark is the newest whole quote the contract's
line holds, restated every ``KEEP_EVERY_MS`` while the session is open
(``mark_fault``). It is kept when the feed is healthy and the quote is live
data, of the feed's current generation, both sides arrived, not crossed, and
both sides were received since today's session opened. Its age is not
tested. A calm contract's sides tick only when they change, so its bid can
stand for many minutes and is still what the contract could be sold for:
each hand-over says so again, and the next day finds its mark at the close.
The row keeps both sides' own receipt times, so whatever prices a close from
it knows the quote's true age and says so
(``natural_price_older_quote``). A quote a **decision** uses is held to the
decision-data age all the same: that test is the runner's and the Clerk's,
at the entry and at the exit, and is not this module's.

What is never a mark: a quote from before a reconnect or a data loss (the
feed's generation moves, and each side waits for a tick of its own), delayed
or frozen data, a quote with one side, a crossed one, and one a side of
which was last received before today's session. Outside the session nothing
is restated. A bid of zero is kept, because it is the truth: nothing is bid.

**A line that shows one side is put back as it stands, and is no mark.**
After a reconnect or a data loss one side of a line can tick before the
other. That quote is the line's last word, though it is not whole. Left out
of the file, a close made with no runner alive would be priced from the
whole quote kept before the loss, at a bid the line no longer shows, while a
runner's EXIT handing over the same line would leave the close unpriced. So
a live quote of the current generation that fails as a mark only for its
missing side is written as of its own receipt (``shows_one_side``,
``KeptOptionQuotes.restore``), at once and again when what it shows
changes. The newest quote alone prices a close, on both roads: with no bid
on it a sale is not priced, and with only a bid on it a sale is priced at
that bid. It is never stated as the newest held, and the account's reading
passes over a quote with one side (``KeptOptionQuotes.stated_mark``): it is
no mark, and it ends none. One from an older generation is never written.

**It outlives the Clerk's runtime.** A Dry Run's runtime is closed when its
run ends. The books give their last word as it closes, and this goes on
asking for the contract's line and keeping its quotes in the account's own
file. So a position an ended run's close could not sell is still quoted: the
pass the next opening runs, the Clerk's retry and the owner's Flatten each
find a recent quote kept, and the next session finds its mark at the close.

A contract past its last session is neither asked for nor kept: nothing
trades in it.

**The stock quotes never wait on this.** The supervisor's pass starts a
round here and goes on (``drive``); it does not wait for the books to be
read or for a file to be written. A round reads the books and keeps the
quotes off the event loop, waits for each no longer than ``STEP_BUDGET_MS``,
and goes on without what is late. One round runs at a time, and work a round
gave up waiting for is left to finish with no second one started beside it.
A fault here is logged once as it starts, again every ``FAULT_REPEAT_MS``
while it lasts and once as it ends, and never reaches the stock evidence: a
write that never returns and an account's answer that cannot be taken up
included. One account's fault does not stop the accounts after it. A process
where no Dry Run account is open and none that closed still holds a
contract starts nothing.

Nothing here opens a connection or keeps a timer of its own. The
supervisor's pass drives it, so a Clerk that reads its market status from a
shared source, and makes no pass, begins no round and keeps no mark.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.sqlite.off_loop import to_thread
from app.lean_sidecar.trading_calendar import current_trading_session_window
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.options.quotes import NOT_YET_ARRIVED, HeldQuote, QuoteDataType, QuoteFault, quote_fault
from app.utils.timestamps import Clock, now_ms_utc

if TYPE_CHECKING:
    # The watch request brings the resolver and the pricer with it. A process that holds no option
    # contract never loads them.
    from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
    from app.broker.ibkr.option_quotes import WatchRequest

logger = logging.getLogger(__name__)

# How often the newest whole quote of a held contract is handed over. A mark is good for 80 seconds
# from its hand-over, so several fall inside the last 80 seconds before a session's close.
KEEP_EVERY_MS = 15_000
# How long a round waits for one piece of its work off the event loop: the read of the books, or one
# account's file write.
STEP_BUDGET_MS = 2_000
# How often a fault that lasts is said again.
FAULT_REPEAT_MS = 5 * 60_000

# What an account's books answer: the OCC symbols of the contracts it must have quoted.
Books = Callable[[], Sequence[str]]


def mark_fault(quote: HeldQuote, *, generation: int, session_open_ms: int, now_ms: int) -> QuoteFault | None:
    """Why ``quote`` is not kept as a held contract's mark at ``now_ms``, or ``None`` when it is.

    Every test a decision's quote passes but its age (``quote_fault``): the
    feed's current generation, both sides, live data, prices, not crossed.
    In place of the age, both sides were received in the session that opened
    at ``session_open_ms``, and neither after ``now_ms``. One that was not
    fails as ``STALE``: it is yesterday's.
    """
    return quote_fault(
        quote, at_ms=now_ms, generation=generation, max_age_ms=now_ms - session_open_ms, needs_a_bid=False
    )


def shows_one_side(quote: HeldQuote, *, generation: int, session_open_ms: int, now_ms: int) -> bool:
    """Whether ``quote`` is what a held contract's line shows now with one side still to arrive, and
    nothing else against it.

    It fails as a mark only for the side that is missing: it is of the
    feed's current generation and live data, and the side it has is a
    price, an ask above zero, received in the session that opened at
    ``session_open_ms`` and not after ``now_ms``. Such a quote is no mark.
    It is the line's last word all the same, so it is put back in the
    account's file as it stands (``HeldOptionQuotes.keep``).
    """
    if mark_fault(quote, generation=generation, session_open_ms=session_open_ms, now_ms=now_ms) is not QuoteFault.ONE_SIDED:
        return False
    side = quote.ask if quote.bid is None else quote.bid
    if side is None or quote.data_type is not QuoteDataType.LIVE:
        return False
    if not side.price.is_finite() or side.price < 0 or (quote.bid is None and side.price == 0):
        return False
    return session_open_ms <= side.received_ms <= now_ms


def _sides_shown(quote: HeldQuote) -> tuple[int, Decimal | None, Decimal | None]:
    """What a line's quote says, whenever it said it: its generation and the price of each side that arrived."""
    return (
        quote.generation,
        None if quote.bid is None else quote.bid.price,
        None if quote.ask is None else quote.ask.price,
    )


# Why a quote is not yet a mark though nothing is wrong: it has not arrived whole in this generation,
# or not since today's session opened.
_WAITING_FOR_A_TICK = NOT_YET_ARRIVED | {QuoteFault.STALE}


def _session_open_now(now_ms: int) -> int | None:
    """When the session ``now_ms`` falls in opened, its close included; ``None`` outside one.

    The close is in it so that the hand-over made as a session ends is that
    session's mark: the next day's starting equity is read at the close.
    """
    window = current_trading_session_window(now_ms)
    if window is None or not window.open_ms_utc <= now_ms <= window.close_ms_utc:
        return None
    return window.open_ms_utc


@dataclass
class _Fault:
    kind: str
    since_ms: int
    said_ms: int
    times: int = 1


class _FaultLog:
    """Says a fault that lasts once as it starts, again every so often while it lasts, and once as it ends.

    A round runs every second. A disk that is full, or books that cannot be
    read, would otherwise write the same stack every second for as long as
    it lasted.
    """

    def __init__(self, clock: Clock, *, repeat_ms: int) -> None:
        self._clock, self._repeat_ms = clock, repeat_ms
        self._lasting: dict[Hashable, _Fault] = {}

    def failed(
        self, key: Hashable, message: str, *, kind: str, extra: dict[str, Any], error: BaseException | None = None
    ) -> None:
        now, fault = self._clock(), self._lasting.get(key)
        if fault is None or fault.kind != kind:
            self._lasting[key] = _Fault(kind, now, now)
            logger.error(message, extra={**extra, "fault": kind}, exc_info=error)
            return
        fault.times += 1
        if now - fault.said_ms >= self._repeat_ms:
            fault.said_ms = now
            logger.warning(
                message, extra={**extra, "fault": kind, "still_failing_since_ms": fault.since_ms, "times": fault.times}
            )

    def cleared(self, key: Hashable, message: str, *, extra: dict[str, Any]) -> None:
        fault = self._lasting.pop(key, None)
        if fault is not None:
            logger.info(message, extra={**extra, "fault": fault.kind, "failed_since_ms": fault.since_ms, "times": fault.times})


@dataclass
class _Account:
    """One Dry Run account: what its books last said it holds, and where its quotes are kept."""

    account_id: str
    kept: KeptOptionQuotes
    # None once the account's Clerk has closed: what it held then stands until it is opened again.
    books: Books | None
    held: tuple[OptionContract, ...] = ()
    # When each held contract's quote was last handed over, by OCC symbol.
    kept_at_ms: dict[str, int] = field(default_factory=dict)
    # What was last put back of a held contract whose line showed one side, by OCC symbol. Gone once
    # the contract is marked again.
    put_back: dict[str, tuple[int, Decimal | None, Decimal | None]] = field(default_factory=dict)
    # The write of this account's file under way, if one is.
    writing: asyncio.Task[int] | None = None


@dataclass
class _Writes:
    """What one round writes to one account's file."""

    account: _Account
    # The marks: each stated as the newest held, as of the round's own clock.
    marks: dict[str, HeldQuote] = field(default_factory=dict)
    # What a line shows with one side still to arrive: put back as of its own receipt, and no mark.
    one_sided: dict[str, HeldQuote] = field(default_factory=dict)


class FollowedBooks:
    """One Clerk's books while they are followed. Closed before the Clerk's repository is."""

    def __init__(self, held: HeldOptionQuotes, account: _Account, books: Books) -> None:
        self._held, self._account, self._books = held, account, books

    def close(self) -> None:
        """Read the books a last time, and stop reading them. What they said then stands.

        Closing twice, or after the account was opened again, does nothing:
        the books now followed are a later opening's.
        """
        self._held._stop_following(self._account, self._books)


def _under_way(task: asyncio.Task[Any] | None) -> bool:
    """Whether ``task`` is still running on the event loop this is asked from. Work left on a loop that has
    gone is nobody's any more."""
    return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()


class HeldOptionQuotes:
    """Every option contract this process's Dry Run Clerks hold: asked for, and its quotes kept."""

    def __init__(
        self,
        *,
        clock: Clock = now_ms_utc,
        keep_every_ms: int = KEEP_EVERY_MS,
        step_budget_ms: int = STEP_BUDGET_MS,
        fault_repeat_ms: int = FAULT_REPEAT_MS,
    ) -> None:
        if keep_every_ms <= 0:
            raise ValueError(f"a held contract's quote is kept every so often; got {keep_every_ms} ms")
        self._clock, self._keep_every_ms, self._step_budget_ms = clock, keep_every_ms, step_budget_ms
        self._accounts: dict[str, _Account] = {}
        # Why each held contract's quote was not handed over on the last round, so a reason is said once.
        self._not_kept: dict[tuple[str, str], str] = {}
        # Why nothing at all could be read of the feed on the last round, said once.
        self._feed_trouble: str | None = None
        self._faults = _FaultLog(clock, repeat_ms=fault_repeat_ms)
        self._round: asyncio.Task[None] | None = None
        self._reading: asyncio.Task[None] | None = None

    # ---- whose books are read ---------------------------------------------------

    def follow(self, account_id: str, *, books: Books, kept: KeptOptionQuotes) -> FollowedBooks:
        """Read ``account_id``'s books from now on, until the handle returned is closed.

        An account followed before keeps when each of its quotes was last
        handed over, so reopening it hands nothing over early.
        """
        account = self._accounts.get(account_id)
        if account is None:
            account = self._accounts[account_id] = _Account(account_id, kept, books)
        else:
            account.books, account.kept = books, kept
        return FollowedBooks(self, account, books)

    def _stop_following(self, account: _Account, books: Books) -> None:
        if account.books is not books:
            return
        account.books = None
        try:
            self._hold(account, books())
        except Exception:
            # What the books said a moment ago stands: a held contract keeps its line.
            logger.exception(
                "Could not read a closing Clerk's held option contracts a last time",
                extra={"action": "held_option_books_last_read_failed", "account_id": account.account_id},
            )

    async def read_books(self) -> None:
        """Read every followed Clerk's books again, off the event loop, and wait for the answer.

        A read that fails leaves what that account held a moment ago: a held
        contract keeps its line while the asking fails.
        """
        reading = self._read_the_books()
        if reading is not None:
            await reading

    def _read_the_books(self) -> asyncio.Task[None] | None:
        """The read of every open Clerk's books under way, begun now if none is. None with no books open."""
        if _under_way(self._reading):
            return self._reading
        open_books = [(account, account.books) for account in self._accounts.values() if account.books is not None]
        if not open_books:
            return None
        self._reading = asyncio.get_running_loop().create_task(self._read(open_books))
        # Heard whoever waits for it, and when nobody does: a round goes on without a read that is late.
        self._reading.add_done_callback(self._read_ended)
        return self._reading

    def _read_ended(self, read: asyncio.Task[None]) -> None:
        """Say how a read of the books ended when it broke. What each account held a moment ago stands."""
        if read.cancelled():
            return
        failed, key = read.exception(), ("reading", None)
        if failed is None:
            self._faults.cleared(
                key, "The Clerks' held option contracts are being read again",
                extra={"action": "held_option_books_reading_again"},
            )
            return
        self._faults.failed(
            key, "The read of the Clerks' held option contracts broke; what each held a moment ago stands",
            kind=type(failed).__name__, error=failed, extra={"action": "held_option_books_reading_broke"},
        )

    async def _read(self, open_books: list[tuple[_Account, Books]]) -> None:
        def read() -> list[tuple[_Account, Books, Sequence[str] | BaseException]]:
            answers: list[tuple[_Account, Books, Sequence[str] | BaseException]] = []
            for account, books in open_books:
                try:
                    answers.append((account, books, books()))
                except Exception as failed:
                    answers.append((account, books, failed))
            return answers

        for account, books, answer in await to_thread(read):
            # A Clerk that closed, or was opened again, while its books were read gave a later word.
            if account.books is not books:
                continue
            key = ("books", account.account_id)
            if isinstance(answer, BaseException):
                self._faults.failed(
                    key, "Could not read a Clerk's held option contracts; what it held a moment ago stands",
                    kind=type(answer).__name__, error=answer,
                    extra={"action": "held_option_books_read_failed", "account_id": account.account_id},
                )
                continue
            try:
                self._hold(account, answer)
            except Exception as failed:
                # An answer that cannot be taken up is this account's fault alone: the accounts after it
                # are still taken up, and what this one held a moment ago stands.
                self._faults.failed(
                    key, "Could not take up what a Clerk's books say it holds; what it held a moment ago stands",
                    kind=type(failed).__name__, error=failed,
                    extra={"action": "held_option_books_answer_not_taken", "account_id": account.account_id},
                )
                continue
            self._faults.cleared(
                key, "A Clerk's held option contracts are being read again",
                extra={"action": "held_option_books_read_again", "account_id": account.account_id},
            )

    def _hold(self, account: _Account, symbols: Sequence[str]) -> None:
        account.held = tuple(OptionContract.from_occ_symbol(symbol) for symbol in sorted(set(symbols)))
        still = {contract.occ_symbol for contract in account.held}
        for symbol in [symbol for symbol in account.kept_at_ms if symbol not in still]:
            del account.kept_at_ms[symbol]
        for symbol in [symbol for symbol in account.put_back if symbol not in still]:
            del account.put_back[symbol]
        for key in [key for key in self._not_kept if key[0] == account.account_id and key[1] not in still]:
            del self._not_kept[key]

    # ---- what is asked for ------------------------------------------------------

    def held(self) -> Mapping[str, tuple[str, ...]]:
        """What each account holds, as its books last said, by account id."""
        return {
            account.account_id: tuple(contract.occ_symbol for contract in account.held)
            for account in self._accounts.values()
            if account.held
        }

    def _still_trading(self, now_ms: int) -> dict[str, list[tuple[_Account, OptionContract]]]:
        """Every held contract whose last session has not ended, by its underlying's ticker."""
        by_underlying: dict[str, list[tuple[_Account, OptionContract]]] = {}
        for account in self._accounts.values():
            for contract in account.held:
                if not past_last_session(contract.expiry_date, now_ms):
                    by_underlying.setdefault(contract.root, []).append((account, contract))
        return by_underlying

    def requests(self) -> tuple[WatchRequest, ...]:
        """The held contracts as the supervisor is asked for them: one request for each underlying.

        A held contract's line is kept whatever else gives way
        (``app.options.watch.plan_lines``).
        """
        if not any(account.held for account in self._accounts.values()):
            return ()
        from app.broker.ibkr.option_quotes import WatchRequest
        from app.options.watch import LineClass

        return tuple(
            WatchRequest(
                underlying=underlying,
                line_class=LineClass.CANDIDATE,
                held=tuple(sorted({contract for _, contract in held}, key=lambda contract: contract.occ_symbol)),
            )
            for underlying, held in sorted(self._still_trading(self._clock()).items())
        )

    # ---- a round, beside the supervisor's pass -----------------------------------

    def drive(self, source: IbkrMarketStatusSource) -> None:
        """The supervisor has made a pass: begin a round, unless one is under way. Returns at once.

        Nothing waits for the round, and nothing it does can fail the
        caller. With no Dry Run account open and nothing held there is no
        round to begin.
        """
        if _under_way(self._round) or not any(
            account.books is not None or account.held for account in self._accounts.values()
        ):
            return
        try:
            self._round = asyncio.get_running_loop().create_task(self._one_round(source))
        except Exception as failed:
            self._round_failed(failed)

    async def _one_round(self, source: IbkrMarketStatusSource) -> None:
        """Read the books, then keep the quotes of what they hold. Each is waited for no longer than the
        step budget."""
        try:
            reading = self._read_the_books()
            if reading is not None:
                await self._within_budget({reading}, key=("books", None), what="the read of the Clerks' books")
            await self.keep(source)
        except asyncio.CancelledError:
            raise
        except Exception as failed:
            self._round_failed(failed)
        else:
            self._faults.cleared(
                ("round", None), "The keeping of held option quotes is running again",
                extra={"action": "held_option_quotes_round_again"},
            )

    def _round_failed(self, failed: BaseException) -> None:
        self._faults.failed(
            ("round", None), "A round of the keeping of held option quotes failed; the next pass begins another",
            kind=type(failed).__name__, error=failed, extra={"action": "held_option_quotes_round_failed"},
        )

    async def _within_budget(self, work: set[asyncio.Task[Any]], *, key: Hashable, what: str) -> set[asyncio.Task[Any]]:
        """Wait for ``work`` no longer than the step budget, and return what finished. What did not is left
        to finish: it is on a worker thread, which nothing can stop."""
        done, late = await asyncio.wait(work, timeout=self._step_budget_ms / 1000)
        if late:
            self._faults.failed(
                key, f"A round of the keeping of held option quotes went on without {what}, which is late",
                kind="late", extra={"action": "held_option_quotes_step_late", "budget_ms": self._step_budget_ms},
            )
        else:
            self._faults.cleared(
                key, f"A round of the keeping of held option quotes has {what} in time again",
                extra={"action": "held_option_quotes_step_in_time"},
            )
        return done

    async def settled(self) -> None:
        """Wait until the round under way, and any work an earlier round went on without, has ended.

        For a caller that must see what the last pass led to. The
        supervisor's own pass never asks.
        """
        while True:
            under_way = [
                task
                for task in (self._round, self._reading, *(account.writing for account in self._accounts.values()))
                if _under_way(task)
            ]
            if not under_way:
                return
            await asyncio.wait(under_way)

    async def stop(self) -> None:
        """End the round under way, as the supervisor that drives it closes. What the books last said stands."""
        under_way = [
            task
            for task in (self._round, self._reading, *(account.writing for account in self._accounts.values()))
            if _under_way(task)
        ]
        for task in under_way:
            task.cancel()
        if under_way:
            await asyncio.gather(*under_way, return_exceptions=True)

    # ---- what is kept -----------------------------------------------------------

    async def keep(self, source: IbkrMarketStatusSource) -> int:
        """Hand each account the mark of every contract it holds whose last hand-over is due, and put back
        what a held contract's line shows while it shows one side only.

        Returns how many marks were handed over while this waited. Each
        account's file is written off the event loop, and a write still
        under way from an earlier round is not started again beside itself.

        A line with one side still to arrive is no mark, and is never
        stated as one. It is put back as of its own receipt, at once and
        whenever what it shows changes, so that the newest quote the file
        holds is the line's last word with no runner alive as it is when a
        runner's EXIT hands the same line over. The newest quote alone
        prices a close: one with no bid leaves a sale unpriced, whatever an
        older quote had bid.
        """
        now_ms = self._clock()
        still_trading = self._still_trading(now_ms)
        if not still_trading:
            return 0
        session_open_ms = _session_open_now(now_ms)
        if session_open_ms is None:
            # No session is open: nothing is restated, and a quote from the last one is no mark of the next.
            return 0
        feed = source.option_feed()
        trouble = (
            "this process has no IBKR quote source for options" if feed is None
            else None if feed.unhealthy is None
            else f"the quote feed is not healthy: {feed.unhealthy}"
        )
        self._say_feed_trouble(trouble)
        if feed is None or trouble is not None:
            return 0
        due: dict[str, _Writes] = {}
        for underlying, held in still_trading.items():
            chain = source.option_chain(underlying)
            for account, contract in held:
                symbol = contract.occ_symbol
                watched = None if chain is None else chain.watched.get(symbol)
                quote = None if watched is None else watched.quote
                if (
                    quote is not None
                    and shows_one_side(quote, generation=feed.generation, session_open_ms=session_open_ms, now_ms=now_ms)
                    and account.put_back.get(symbol) != _sides_shown(quote)
                ):
                    # Not held to the marks' cadence: the line may have lost a side a second after its mark.
                    due.setdefault(account.account_id, _Writes(account)).one_sided[symbol] = quote
                last = account.kept_at_ms.get(symbol)
                if last is not None and now_ms - last < self._keep_every_ms:
                    continue
                fault = None if quote is None else mark_fault(
                    quote, generation=feed.generation, session_open_ms=session_open_ms, now_ms=now_ms
                )
                if quote is not None and fault is None:
                    due.setdefault(account.account_id, _Writes(account)).marks[symbol] = quote
                else:
                    self._say_once(account, symbol, fault)
        writes: set[asyncio.Task[int]] = set()
        for wanted in due.values():
            account = wanted.account
            if _under_way(account.writing):
                # Its file is still being written from an earlier round: the disk, or its lock, is slow. No
                # second write is begun beside it, and it is said again for as long as it lasts.
                self._faults.failed(
                    ("write", account.account_id),
                    "A write of kept option quotes has not returned; none is begun beside it, and marks lapse meanwhile",
                    kind="late", extra={"action": "held_option_quotes_write_stuck", "account_id": account.account_id},
                )
                continue
            account.writing = asyncio.get_running_loop().create_task(self._write(wanted, now_ms))
            writes.add(account.writing)
        if not writes:
            return 0
        written = await self._within_budget(writes, key=("writes", None), what="a write of kept quotes")
        return sum(write.result() for write in written)

    async def _write(self, wanted: _Writes, now_ms: int) -> int:
        """Append one account's hand-over to its file, and note when each quote was handed over. Returns
        how many marks it stated."""
        account, marks, one_sided = wanted.account, wanted.marks, wanted.one_sided
        key = ("write", account.account_id)

        def write() -> None:
            if marks:
                account.kept.keep_all(marks, kept_at_ms=now_ms)
            for symbol, quote in one_sided.items():
                # As of its own receipt, never as of now: it says what the line shows, and is no mark.
                account.kept.restore(symbol, quote, now_ms=now_ms)

        try:
            await to_thread(write)
        except Exception as failed:
            # Tried again on the next round. A mark that lapses meanwhile says so itself.
            self._faults.failed(
                key, "Could not keep the quotes of held option contracts; tried again on the next round",
                kind=type(failed).__name__, error=failed,
                extra={"action": "held_option_quotes_not_written", "account_id": account.account_id},
            )
            return 0
        self._faults.cleared(
            key, "The quotes of held option contracts are being kept again",
            extra={"action": "held_option_quotes_written_again", "account_id": account.account_id},
        )
        for symbol, quote in one_sided.items():
            account.put_back[symbol] = _sides_shown(quote)
        for symbol in marks:
            # Never back: a late write of an earlier round must not undo a later one's note.
            account.kept_at_ms[symbol] = max(now_ms, account.kept_at_ms.get(symbol, now_ms))
            # Whole again: the next time its line shows one side, that is put back, whatever was before.
            account.put_back.pop(symbol, None)
            if self._not_kept.pop((account.account_id, symbol), None) is not None:
                logger.info(
                    "A held option contract's quote is being kept",
                    extra={"action": "held_option_quote_kept", "account_id": account.account_id, "contract": symbol},
                )
        return len(marks)

    def _say_feed_trouble(self, trouble: str | None) -> None:
        """Said once for the feed, not once for each contract, and once more when it is over."""
        if trouble == self._feed_trouble:
            return
        self._feed_trouble = trouble
        if trouble is None:
            logger.info("Held option contracts can be marked again", extra={"action": "held_option_quotes_feed_back"})
        else:
            logger.warning(
                "No held option contract can be marked; each mark lapses until this is over",
                extra={"action": "held_option_quotes_no_feed", "why": trouble},
            )

    def _say_once(self, account: _Account, symbol: str, fault: QuoteFault | None) -> None:
        """Say why a held contract has no mark, once for each reason. A contract only waiting for its first
        ticks of the session, or since a reconnect, has nothing wrong with it and is not a warning."""
        waiting = fault is None or fault in _WAITING_FOR_A_TICK
        why = (
            "no quote has arrived on its line" if fault is None
            else "a side has not ticked since today's session opened" if fault is QuoteFault.STALE
            else f"its quote fails as {fault.value}"
        )
        key = (account.account_id, symbol)
        if self._not_kept.get(key) == why:
            return
        self._not_kept[key] = why
        logger.log(
            logging.INFO if waiting else logging.WARNING,
            "A held option contract has no quote to keep as its mark yet",
            extra={"action": "held_option_quote_not_kept", "account_id": account.account_id, "contract": symbol, "why": why},
        )


_held = HeldOptionQuotes()


def get_held_option_quotes() -> HeldOptionQuotes:
    """What this process's Dry Run Clerks hold in option contracts. The process's IBKR supervisor reads it."""
    return _held


def reset_held_option_quotes_for_testing() -> None:
    """Forget every account followed (test helper)."""
    global _held
    _held = HeldOptionQuotes()


__all__ = [
    "FAULT_REPEAT_MS",
    "KEEP_EVERY_MS",
    "STEP_BUDGET_MS",
    "FollowedBooks",
    "HeldOptionQuotes",
    "get_held_option_quotes",
    "mark_fault",
    "reset_held_option_quotes_for_testing",
    "shows_one_side",
]
