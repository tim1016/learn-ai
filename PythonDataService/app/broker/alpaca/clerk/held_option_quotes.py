"""The quotes a Dry Run's Clerk keeps for the option contracts its bots hold, runner or no runner (ADR 0076).

A Dry Run has no option market of its own. What it knows of a contract's
price is the quotes kept for it (``kept_option_quotes``): a close is priced
from the newest, and an open contract is marked from the last one handed
over. A runner hands over the quotes of its own two decisions, the entry's
and the exit's. Everything between and after them is this module's.

**What is held is read from the Clerk's books**, never from a runner: a
position in a recorded contract, or an order working in one
(``reads.option_contracts_to_watch``). Each Dry Run's Clerk is followed while
it is composed, and its books are read on every pass of the IBKR supervisor.

**What is held is asked for** as the supervisor's first class of quote line
(``requests``), so a held contract keeps its line when the runner that bought
it is gone.

**What is held is kept**: every ``KEEP_EVERY_MS`` the newest quote the
supervisor holds for it is appended to its account's kept quotes, when that
quote is one a decision could use (``app.options.quotes.quote_fault``, with
the decision-data age limit, on a healthy feed). A hand-over says "this is
the newest held", and a mark is judged by the hand-over's own time, so a
quote that is delayed, from before a reconnect, one-sided or gone quiet is
not handed over: restating it would pass a dead line off as a fresh mark. A
bid of zero is handed over, because it is the truth: nothing is bid.

**It outlives the Clerk's runtime.** A Dry Run's runtime is closed when its
run ends. The books give their last word as it closes, and this goes on
asking for the contract's line and keeping its quotes in the account's own
file. So a position an ended run's close could not sell is still quoted: the
pass the next opening runs, the Clerk's retry and the owner's Flatten each
find a recent quote kept, and the next session finds its mark at the close.

A contract past its last session is neither asked for nor kept: nothing
trades in it.

Nothing here opens a connection or keeps a timer. The supervisor's own pass
drives it (``app.broker.alpaca.market_liveness``), and a fault here is logged
and never takes the stock quotes down with it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.sqlite.off_loop import to_thread
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.options.quotes import HeldQuote, quote_fault
from app.services.market_liveness import QUOTE_MAX_AGE_MS
from app.utils.timestamps import Clock, now_ms_utc

if TYPE_CHECKING:
    # The watch request brings the resolver and the pricer with it. A process that holds no option
    # contract never loads them.
    from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
    from app.broker.ibkr.option_quotes import WatchRequest
    from app.options.resolver import FeedState

logger = logging.getLogger(__name__)

# How often the newest usable quote of a held contract is handed over. A mark is good for 80 seconds
# from its hand-over, so several fall inside the last 80 seconds before a session's close.
KEEP_EVERY_MS = 15_000
# The oldest either side of a quote may be when it is handed over: what the Clerk holds an entry's
# own quote to.
HELD_QUOTE_MAX_AGE_MS = QUOTE_MAX_AGE_MS

# What an account's books answer: the OCC symbols of the contracts it must have quoted.
Books = Callable[[], Sequence[str]]


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


class HeldOptionQuotes:
    """Every option contract this process's Dry Run Clerks hold: asked for, and its quotes kept."""

    def __init__(
        self,
        *,
        clock: Clock = now_ms_utc,
        keep_every_ms: int = KEEP_EVERY_MS,
        max_quote_age_ms: int = HELD_QUOTE_MAX_AGE_MS,
    ) -> None:
        if keep_every_ms <= 0:
            raise ValueError(f"a held contract's quote is kept every so often; got {keep_every_ms} ms")
        self._clock, self._keep_every_ms, self._max_quote_age_ms = clock, keep_every_ms, max_quote_age_ms
        self._accounts: dict[str, _Account] = {}
        # Why each held contract's quote was not handed over on the last round, so a reason is said once.
        self._not_kept: dict[tuple[str, str], str] = {}

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
        """Read every followed Clerk's books again, off the event loop.

        A read that fails leaves what that account held a moment ago: a held
        contract keeps its line while the asking fails.
        """
        open_books = [(account, account.books) for account in self._accounts.values() if account.books is not None]
        if not open_books:
            return

        def read() -> list[tuple[_Account, Books, Sequence[str] | None]]:
            answers: list[tuple[_Account, Books, Sequence[str] | None]] = []
            for account, books in open_books:
                try:
                    answers.append((account, books, books()))
                except Exception:
                    # A Clerk that closed while its books were read has given its last word already.
                    if account.books is books:
                        logger.exception(
                            "Could not read a Clerk's held option contracts",
                            extra={"action": "held_option_books_read_failed", "account_id": account.account_id},
                        )
                    answers.append((account, books, None))
            return answers

        for account, books, symbols in await to_thread(read):
            # A Clerk that closed, or was opened again, while its books were read gave a later word.
            if symbols is not None and account.books is books:
                self._hold(account, symbols)

    def _hold(self, account: _Account, symbols: Sequence[str]) -> None:
        account.held = tuple(OptionContract.from_occ_symbol(symbol) for symbol in sorted(set(symbols)))
        still = {contract.occ_symbol for contract in account.held}
        for symbol in [symbol for symbol in account.kept_at_ms if symbol not in still]:
            del account.kept_at_ms[symbol]
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

    # ---- what is kept -----------------------------------------------------------

    async def keep(self, source: IbkrMarketStatusSource) -> int:
        """Hand each account the newest usable quote of every contract it holds whose last hand-over is due.

        Returns how many quotes were handed over. The file is written off the
        event loop.
        """
        now_ms = self._clock()
        still_trading = self._still_trading(now_ms)
        if not still_trading:
            return 0
        feed = source.option_feed()
        due: dict[str, tuple[_Account, dict[str, HeldQuote]]] = {}
        for underlying, held in still_trading.items():
            chain = None if feed is None else source.option_chain(underlying)
            for account, contract in held:
                symbol = contract.occ_symbol
                last = account.kept_at_ms.get(symbol)
                if last is not None and now_ms - last < self._keep_every_ms:
                    continue
                watched = None if chain is None else chain.watched.get(symbol)
                usable = self._usable(feed, None if watched is None else watched.quote, now_ms)
                if isinstance(usable, HeldQuote):
                    due.setdefault(account.account_id, (account, {}))[1][symbol] = usable
                else:
                    self._say_once(account, symbol, usable)
        handed_over = 0
        for account, quotes in due.values():
            try:
                await to_thread(partial(account.kept.keep_all, quotes, kept_at_ms=now_ms))
            except Exception:
                # Tried again on the next round. A mark that lapses meanwhile says so itself.
                logger.exception(
                    "Could not keep the quotes of held option contracts",
                    extra={"action": "held_option_quotes_not_written", "account_id": account.account_id},
                )
                continue
            for symbol in quotes:
                account.kept_at_ms[symbol] = now_ms
                if self._not_kept.pop((account.account_id, symbol), None) is not None:
                    logger.info(
                        "A held option contract's quote is being kept again",
                        extra={"action": "held_option_quote_kept_again", "account_id": account.account_id, "contract": symbol},
                    )
            handed_over += len(quotes)
        return handed_over

    def _usable(self, feed: FeedState | None, quote: HeldQuote | None, now_ms: int) -> HeldQuote | str:
        """``quote`` when it is one a decision could use at ``now_ms``, or why it is not handed over."""
        if feed is None:
            return "this process has no IBKR quote source for options"
        if feed.unhealthy is not None:
            return f"the quote feed is not healthy: {feed.unhealthy}"
        if quote is None:
            return "no quote is held for it"
        fault = quote_fault(
            quote, at_ms=now_ms, generation=feed.generation, max_age_ms=self._max_quote_age_ms, needs_a_bid=False
        )
        return quote if fault is None else f"its quote fails as {fault}"

    def _say_once(self, account: _Account, symbol: str, why: str) -> None:
        key = (account.account_id, symbol)
        if self._not_kept.get(key) == why:
            return
        self._not_kept[key] = why
        logger.warning(
            "A held option contract has no usable quote to keep; its mark lapses until one arrives",
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
    "HELD_QUOTE_MAX_AGE_MS",
    "KEEP_EVERY_MS",
    "FollowedBooks",
    "HeldOptionQuotes",
    "get_held_option_quotes",
    "reset_held_option_quotes_for_testing",
]
