"""The option quotes a simulated account was handed, kept so a later fill or mark can be priced from them.

A Dry Run has no option market of its own. What it knows of a contract's
price is the quotes kept for it: an entry's as its order is sent, an exit
decision's as its close is priced, and the mark the Clerk's own keeping
states of each contract a bot holds (``held_option_quotes``). Each is
appended here, beside the account's simulated orders, with both sides'
receipt times as they arrived. A Live Shadow fills and marks the same way,
from the same kind of file in its own account's folder (ADR 0076 decision
25): where this module says a Dry Run, it holds for a Shadow too.

Two questions are asked of what is kept, and they are not the same:

- **Which is the newest quote?** By when its sides were received. A simulated
  close with no usable quote of its own, and the run-end close, are priced
  from it, and carry its age. It alone prices a close: when it has nothing
  on the side the close fills at, no older quote stands in for it.
- **What was last stated to be the newest held, and when?** By when it was
  handed over. The simulated account marks an open contract from it. A quiet
  contract's bid can stand unchanged for minutes: its receipt time grows old
  while it is still the bid, and each hand-over says so again. The row
  keeps the receipt times as they were, so a close priced from it knows
  the quote's true age.

So a hand-over is written every time, even of a quote already kept
(``keep``). A quote the Clerk hands back from its own durable facts, in case
this file was lost, says nothing of what is current: it is written as of its
own receipt (``restore``). So is what a held contract's line shows while one
of its sides has still to arrive, which the Clerk's keeping puts back: it is
the newest quote by receipt, and never a mark: the account's reading passes
over a quote with one side (``stated_mark``).

Nothing here judges a quote. Whether one is usable for a decision is
``app.options.quotes.quote_fault``'s to say, whether one may be stated as a
held contract's mark is ``held_option_quotes.mark_fault``'s, and how a fill
is priced from one is ``app.options.simulated_fill``'s.

**Pricing statements are compacted; minute observations have a separate index.** A hand-over every few seconds would otherwise
grow the file without end, and every reader would pay for all of it. So the
file is cut back as it grows to the rows a later reader can still be
answered from (``rows_still_answerable``), and what it holds is also held in
memory, read again only when the file has changed. A reader asks about now
or later, or about a session's close: the mark a contract held overnight
starts the next day from. Those answers survive every cut. New minute observations
are appended to an indexed SQLite sidecar, so a current minute never reparses the
whole history (#3368). Legacy inline minutes remain readable and retained in place.
Neither prices orders or replaces a held mark.

A fill or a baseline that used a kept quote records the quote itself, or its
figure. The reference it carries (``KeptOptionQuote.ref``) says where the
quote was kept when it was used; the row may since have been cut.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.alpaca.clerk.option_quote_minutes import OptionQuoteMinutes
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.broker.contract.errors import BrokerUnavailable
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import DELIVERY_ALLOWANCE_MS
from app.options.quotes import HeldQuote, receipt_times
from app.services.decision_clock import SOURCE_BAR_MS
from app.services.jsonl_wal import JsonlWal
from app.utils.advisory_lock import advisory_file_lock
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

KEPT_OPTION_QUOTES_FILENAME = "kept_option_quotes.jsonl"
_KEEP_LOCK = threading.RLock()

# How old a quote may be and still stand for a held contract's mark: a share bar's length and the delivery
# allowance a decision on that bar is given (ADR 0076 decision 28). The simulated account's mark and the
# exit estimate's quotes (#3100) are held to this one limit.
HELD_MARK_MAX_AGE_MS = SOURCE_BAR_MS + DELIVERY_ALLOWANCE_MS

# A file of fewer rows than this is never cut back: reading it whole costs
# nothing worth saving. Above it, the file is cut once it has doubled since
# the last cut, so the work of cutting is spread thin over the appends.
COMPACT_ABOVE_ROWS = 512

type _Side = Literal["bid", "ask"]
_SIDES: tuple[_Side, ...] = ("bid", "ask")


def _closing_side(side: Literal["buy", "sell"]) -> _Side:
    """The side of a quote a close is filled at: a sale at the bid, a purchase at the ask."""
    return "bid" if side == "sell" else "ask"


class KeptOptionQuote(BaseModel):
    """One quote as it was handed over, for one contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    # The contract's OCC symbol.
    symbol: str = Field(min_length=1)
    quote: RecordedQuote
    kept_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # A held-book observation, separate from the quotes that price simulated orders (#3368).
    minute_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)

    @property
    def received_ms(self) -> int:
        """When its newest side was received."""
        return max(receipt_times(self.quote.held))

    @property
    def ref(self) -> str:
        """What a record that used this quote cites."""
        if self.minute_ms is not None:
            return f"held-option-minute:{self.symbol}:{self.minute_ms}"
        return f"kept-option-quote:{self.symbol}:{self.seq}"


class KeptOptionQuotesUnavailable(BrokerUnavailable):
    """The quotes kept for a simulated account cannot be read: a whole line of the file is not a kept quote.

    Nothing can be priced or marked from them until the file is repaired. A
    line a crash left half-written is not this: it is passed over on a read
    and cut off by the next write.
    """


class _KeptFileCorrupt(RuntimeError):
    """What the file's reader raises of an unreadable line; told to callers as ``KeptOptionQuotesUnavailable``."""


def _corrupt(path: Path, detail: str) -> RuntimeError:
    return _KeptFileCorrupt(f"Kept option quotes corrupt at {path}: {detail}")


def _session_close(day: date) -> int | None:
    """That day's session close, or ``None`` for a weekend or a holiday, which has none."""
    try:
        return session_close_ms_utc(day)
    except LookupError:
        return None


def _session_closes(first_ms: int, last_ms: int) -> list[int]:
    """Every session close from the day before ``first_ms`` to the day after ``last_ms``, in order."""
    closes: list[int] = []
    day, last_day = et_date_at_ms(first_ms) - timedelta(days=1), et_date_at_ms(last_ms) + timedelta(days=1)
    while day <= last_day:
        close = _session_close(day)
        if close is not None:
            closes.append(close)
        day += timedelta(days=1)
    return closes


def rows_still_answerable(rows: Sequence[KeptOptionQuote], *, as_of_ms: int) -> list[KeptOptionQuote]:
    """The rows a reader at ``as_of_ms`` or later can still be answered from, in the order they were written.

    A reader asks one of three things, each for one contract:

    - the newest quote by receipt, as of now or later, whatever sides it
      has. Kept: the one received last by ``as_of_ms``, and every one
      received later than that, any of which may yet become the newest. An
      older one is not kept for a side the newest lacks: it prices nothing
      once a newer quote is kept (``closing_quote``);
    - the quote last handed over, as of now or later, when asked for with a
      side that had arrived. Kept: for each side, the last one handed over
      by ``as_of_ms``, and every one stamped later;
    - the quote last handed over at or before a session's close: the mark
      the next day starts from. Kept: for each side and each close, the last
      one handed over between the close before it and that close.

    ``as_of_ms`` is the writer's own clock as it cuts, and no reader asks
    about an earlier now. It is never read off the rows: a quote put back
    as of its own receipt (``restore``) can be stamped ahead of the clock,
    and a cut made as of that stamp would drop the very rows a reader
    asking about now is answered from. A clock behind the truth only keeps
    more. The last row written is always kept, so the sequence never runs
    back over a number already given out.
    """
    if not rows:
        return []
    closes = _session_closes(min(as_of_ms, *(row.kept_at_ms for row in rows)), as_of_ms)
    kept = {rows[-1].seq}
    # Preserve legacy inline observations; new minutes use the indexed sidecar (#3368).
    kept.update(row.seq for row in rows if row.minute_ms is not None)
    newest: dict[str, KeptOptionQuote] = {}
    stated: dict[tuple[str, _Side, int], KeptOptionQuote] = {}
    for row in rows:
        if row.minute_ms is not None:
            continue
        received_ms = row.received_ms
        if received_ms > as_of_ms:
            kept.add(row.seq)
        else:
            best = newest.get(row.symbol)
            if best is None or (received_ms, row.seq) > (best.received_ms, best.seq):
                newest[row.symbol] = row
        # The close this hand-over is the mark of, if no later one comes before it.
        before_close = bisect_left(closes, row.kept_at_ms)
        for side in _SIDES:
            if getattr(row.quote, side) is None:
                continue
            if row.kept_at_ms > as_of_ms:
                # Stamped ahead of the clock: it is nobody's statement about now, and may yet be the last.
                kept.add(row.seq)
                continue
            last = stated.get((row.symbol, side, before_close))
            if last is None or (row.kept_at_ms, row.seq) > (last.kept_at_ms, last.seq):
                stated[row.symbol, side, before_close] = row
    kept.update(row.seq for row in newest.values())
    kept.update(row.seq for row in stated.values())
    return [row for row in rows if row.seq in kept]


@dataclass
class _Held:
    """What one file of kept quotes holds, as it was last read, shared by every handle on it in this process."""

    # Which file it was, its last write and its size when it was read; ``None`` when it did not exist.
    stamp: tuple[int, int, int] | None = None
    read: bool = False
    by_symbol: dict[str, list[KeptOptionQuote]] = field(default_factory=dict)
    pricing_by_symbol: dict[str, list[KeptOptionQuote]] = field(default_factory=dict)
    minute_keys: set[tuple[str, int]] = field(default_factory=set)
    minute_clock: list[int] = field(default_factory=list)
    minutes_by_ms: dict[int, list[KeptOptionQuote]] = field(default_factory=dict)
    last_seq: int = 0
    rows: int = 0
    # How many rows the last cut left. The file is cut again once it holds twice as many.
    rows_after_cut: int = 0
    # How many rows were parsed from the file since this process first read it.
    rows_parsed: int = 0

    def hold(self, rows: Sequence[KeptOptionQuote], *, stamp: tuple[int, int, int] | None) -> None:
        self.by_symbol = {}
        self.minute_keys = set()
        self.minutes_by_ms = {}
        self.pricing_by_symbol = {}
        for row in rows:
            self.by_symbol.setdefault(row.symbol, []).append(row)
            if row.minute_ms is not None:
                self.minute_keys.add((row.symbol, row.minute_ms))
                self.minutes_by_ms.setdefault(row.minute_ms, []).append(row)
            else:
                self.pricing_by_symbol.setdefault(row.symbol, []).append(row)
        self.minute_clock = sorted(self.minutes_by_ms)
        self.last_seq = rows[-1].seq if rows else 0
        self.rows = len(rows)
        self.stamp, self.read = stamp, True

    def all_rows(self) -> list[KeptOptionQuote]:
        return sorted((row for rows in self.by_symbol.values() for row in rows), key=lambda row: row.seq)


_HELD: dict[str, _Held] = {}


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """Which file is at ``path``, its last write and its size; ``None`` before its first write.

    Every append changes the last two. A cut puts a new file in the old
    one's place, and so does anything else that swaps the file: that
    changes the first, even when the new file is as long and stamped the
    same instant.
    """
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


class KeptOptionQuotes:
    """The option quotes one simulated account was handed, cut back to what a later reader can use."""

    def __init__(self, *, path: Path, trusted_root: Path) -> None:
        self._minutes = OptionQuoteMinutes(path.with_suffix(".minutes.sqlite3"), trusted_root=trusted_root)
        self._wal: JsonlWal[KeptOptionQuote] = JsonlWal(
            path,
            record_model=KeptOptionQuote,
            corrupt_error=_corrupt,
            seq_of=lambda row: row.seq,
            label="kept_option_quote",
            trusted_root=trusted_root,
        )

    @classmethod
    def beside(cls, evidence_file: Path) -> KeptOptionQuotes:
        """The store that sits in the same account folder as ``evidence_file``."""
        return cls(path=evidence_file.with_name(KEPT_OPTION_QUOTES_FILENAME), trusted_root=evidence_file.parent)

    @classmethod
    def of_account(cls, *, artifacts_root: Path, account_id: str) -> KeptOptionQuotes:
        """One simulated account's store, in the folder its retained bars and simulated orders are in."""
        account_dir = resolve_contained_path(
            Path(artifacts_root), "accounts", "alpaca", safe_path_component(account_id, "account id")
        )
        return cls(path=account_dir / KEPT_OPTION_QUOTES_FILENAME, trusted_root=account_dir)

    @property
    def path(self) -> Path:
        return self._wal.path

    @property
    def rows_parsed(self) -> int:
        """How many rows this process has parsed from the file: the work its reads have cost."""
        with _KEEP_LOCK:
            return self._shared().rows_parsed

    def _shared(self) -> _Held:
        return _HELD.setdefault(str(self._wal.path), _Held())

    def _held(self) -> _Held:
        """What the file holds now, read again only if it has changed since it was last read."""
        held = self._shared()
        stamp = _stamp(self._wal.path)
        if held.read and stamp == held.stamp:
            return held
        try:
            rows = self._wal.read_all()
        except _KeptFileCorrupt as exc:
            raise KeptOptionQuotesUnavailable(
                "The option quotes kept for this simulated account cannot be read, so nothing can be priced or marked "
                "from them.",
                detail=str(exc),
            ) from exc
        held.hold(rows, stamp=stamp)
        held.rows_parsed += len(rows)
        return held

    def refresh(self) -> None:
        """Read the file now if it has changed, so that a later question finds it already read.

        For a caller about to take a lock it must not read a file under.
        """
        with _KEEP_LOCK:
            self._held()

    def keep(self, symbol: str, quote: HeldQuote, *, kept_at_ms: int) -> None:
        """Keep the quote that is the newest held for a contract at ``kept_at_ms``.

        Written every time, so that a quote unchanged since it was last kept
        is stated again as the newest held. A quote with no side says
        nothing and is not written; nor is the same statement made twice.
        """
        self.keep_all({symbol: quote}, kept_at_ms=kept_at_ms)

    def keep_all(self, quotes: Mapping[str, HeldQuote], *, kept_at_ms: int) -> None:
        """One hand-over: the newest quote held for each contract at ``kept_at_ms``, by OCC symbol.

        Written as one append with one sync, however many contracts it names.
        ``kept_at_ms`` is the caller's clock now, and the file is cut back as
        of it when it has grown.
        """
        self._append([(symbol, quote, kept_at_ms) for symbol, quote in quotes.items()], now_ms=kept_at_ms)

    def keep_minutes(self, quotes: Mapping[str, HeldQuote], *, observed_at_ms: int) -> None:
        """Keep the first held-book observation per contract and UTC minute. Later observations never replace it."""
        if not quotes:
            return
        minute = observed_at_ms // SOURCE_BAR_MS * SOURCE_BAR_MS
        with _KEEP_LOCK:
            legacy = self._held().minute_keys
            pending = {symbol: quote for symbol, quote in quotes.items() if (symbol, minute) not in legacy}
        try:
            self._minutes.keep(pending, minute_ms=minute, observed_at_ms=observed_at_ms)
        except sqlite3.Error as exc:
            raise KeptOptionQuotesUnavailable("Held option quote minutes could not be saved.", detail=str(exc)) from exc

    def minutes(self, *, from_ms: int, to_ms: int) -> list[KeptOptionQuote]:
        """Read only the requested range, preserving the first observation in either store."""
        with _KEEP_LOCK:
            held = self._held()
            clock = held.minute_clock[bisect_left(held.minute_clock, from_ms):bisect_left(held.minute_clock, to_ms)]
            legacy = [row for minute in clock for row in held.minutes_by_ms[minute] if row.kept_at_ms < to_ms]
        try:
            rows = [*legacy, *self._minutes.read(from_ms=from_ms, to_ms=to_ms)]
        except (sqlite3.Error, ValueError) as exc:
            raise KeptOptionQuotesUnavailable("Held option quote minutes could not be read.", detail=str(exc)) from exc
        found = {}
        for row in sorted(rows, key=lambda row: (row.kept_at_ms, row.seq)):
            found.setdefault((row.symbol, row.minute_ms), row)
        return list(found.values())

    def restore(self, symbol: str, quote: HeldQuote, *, now_ms: int | None = None) -> None:
        """Put back a quote the caller holds, if it is not kept: as of its own receipt, never as of now.

        Its receipt says nothing of what time it is, and may be ahead of
        the clock. So putting one back cuts the file only for a caller that
        says what time it is, having seen the quote was received by then
        (``now_ms``): the Clerk's keeping, which puts back what a line
        shows many times a day. With none, the next hand-over cuts, as of
        its own clock.
        """
        times = receipt_times(quote)
        if times:
            self._append([(symbol, quote, max(times))], now_ms=now_ms)

    def _append(
        self, statements: Sequence[tuple[str, HeldQuote, int]], *, now_ms: int | None,
    ) -> None:
        said = [(symbol, RecordedQuote.of(quote), kept_at_ms) for symbol, quote, kept_at_ms in statements if receipt_times(quote)]
        if not said:
            return
        with _KEEP_LOCK, advisory_file_lock(self._wal.path):
            # Under the file's own lock: another process may have written since this one last read.
            held = self._held()
            rows: list[KeptOptionQuote] = []
            for symbol, recorded, kept_at_ms in said:
                already = any(
                    row.quote == recorded and row.kept_at_ms == kept_at_ms
                    for row in (*held.pricing_by_symbol.get(symbol, ()), *(new for new in rows if new.symbol == symbol))
                )
                if not already:
                    rows.append(
                        KeptOptionQuote(seq=held.last_seq + len(rows) + 1, symbol=symbol, quote=recorded,
                                        kept_at_ms=kept_at_ms)
                    )
            if not rows:
                return
            self._wal.reset_seq(held.last_seq + 1)
            self._wal.append_many(rows)
            for row in rows:
                held.by_symbol.setdefault(row.symbol, []).append(row)
                held.pricing_by_symbol.setdefault(row.symbol, []).append(row)
            held.last_seq, held.rows = rows[-1].seq, held.rows + len(rows)
            held.stamp = _stamp(self._wal.path)
            if now_ms is not None and held.rows > max(COMPACT_ABOVE_ROWS, 2 * held.rows_after_cut):
                self._cut_back(held, as_of_ms=now_ms)

    def _cut_back(self, held: _Held, *, as_of_ms: int) -> None:
        """Rewrite the file to the rows a reader at ``as_of_ms`` or later can still be answered from. Under both locks."""
        still = rows_still_answerable(held.all_rows(), as_of_ms=as_of_ms)
        if len(still) < held.rows:
            self._wal.replace_all(still)
            held.hold(still, stamp=_stamp(self._wal.path))
        held.rows_after_cut = len(still)

    def _kept_for(self, symbol: str) -> list[KeptOptionQuote]:
        with _KEEP_LOCK:
            return list(self._held().pricing_by_symbol.get(symbol, ()))

    def newest(self, symbol: str, *, at_or_before_ms: int | None = None) -> KeptOptionQuote | None:
        """The quote kept for ``symbol`` whose sides were received last, whatever sides it has.

        ``at_or_before_ms`` leaves out one received later than an instant.
        """
        candidates = [
            row for row in self._kept_for(symbol) if at_or_before_ms is None or row.received_ms <= at_or_before_ms
        ]
        return max(candidates, key=lambda row: (row.received_ms, row.seq), default=None)

    def closing_quote(self, symbol: str, *, side: Literal["buy", "sell"], at_ms: int) -> KeptOptionQuote | None:
        """The quote a close of ``symbol`` made at ``at_ms`` is filled from, or ``None`` when none kept can fill it.

        The newest quote received by then decides, and it alone. A sale to
        close is filled at its bid and a purchase to close at its ask. When
        that side has not arrived on it, the close has nothing to be filled
        from: what an older quote had there says nothing of what is there
        now, so no older quote is looked for.
        """
        newest = self.newest(symbol, at_or_before_ms=at_ms)
        if newest is None or getattr(newest.quote, _closing_side(side)) is None:
            return None
        return newest

    def prices_a_close(self, symbol: str, *, side: Literal["buy", "sell"], at_ms: int) -> bool:
        """Whether a close of ``symbol`` made at ``at_ms`` has a price in what is kept.

        It has one when the quote it would be filled from (``closing_quote``)
        has something on the side it fills at: above zero. A newest quote
        with nothing bid, or with no bid at all, prices no sale, whatever an
        older quote had bid.

        The one answer both the simulation and the recovery catalog give to
        "can this contract be closed now", so an owner is never offered a
        Flatten the simulation would then refuse for want of a price.
        """
        quote = self.closing_quote(symbol, side=side, at_ms=at_ms)
        price = None if quote is None else getattr(quote.quote, _closing_side(side))
        return price is not None and price > 0

    def last_stated(
        self,
        symbol: str,
        *,
        at_or_before_ms: int,
        with_side: Literal["bid", "ask"] | None = None,
    ) -> KeptOptionQuote | None:
        """The quote most lately handed over as the newest held for ``symbol``, no later than an instant."""
        candidates = [
            row
            for row in self._kept_for(symbol)
            if row.kept_at_ms <= at_or_before_ms
            and (with_side is None or getattr(row.quote, with_side) is not None)
        ]
        return max(candidates, key=lambda row: (row.kept_at_ms, row.seq), default=None)

    def stated_mark(self, symbol: str, *, at_or_before_ms: int) -> KeptOptionQuote | None:
        """The quote a bought contract is marked from at an instant, before its freshness is asked.

        The whole quote most lately handed over as the newest held, no
        later than the instant. A quote with one side is passed over. It
        states nothing: a line's quote put back while its other side was
        still to arrive is the line's last word for a close, and it neither
        is a mark nor ends the one stated before it, which stands for as
        long as any statement does.

        A cut keeps the last statement of each side. So it keeps the last
        whole one, unless a quote with only a bid and one with only an ask
        were both put back after it. The contract then has no mark until the
        next whole quote is stated: a missing mark, never a wrong one.
        """
        whole = [
            row
            for row in self._kept_for(symbol)
            if row.kept_at_ms <= at_or_before_ms and row.quote.bid is not None and row.quote.ask is not None
        ]
        return max(whole, key=lambda row: (row.kept_at_ms, row.seq), default=None)


__all__ = [
    "COMPACT_ABOVE_ROWS",
    "HELD_MARK_MAX_AGE_MS",
    "KEPT_OPTION_QUOTES_FILENAME",
    "KeptOptionQuote",
    "KeptOptionQuotes",
    "KeptOptionQuotesUnavailable",
    "rows_still_answerable",
]
