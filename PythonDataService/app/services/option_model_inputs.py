"""The rate and the dividends an option on one underlying is priced with at one moment, each with its source.

The American pricer (``app.options.american``) takes a rate and the cash
dividends still to go ex before expiry, and has no default for either: a
stock priced as paying nothing must be one somebody looked up. This is the
one place they are looked up, for a preview now and for a bot's own entries
later (#2913).

- **The rate** is the repo's existing one: FRED's Treasury bill rates,
  interpolated to the days left to the latest expiry
  (``app.services.fred_service``). When FRED gives none, that service falls
  back to a fixed default with no recorded origin, and the source says so.
- **The dividends** are the cash dividends *declared* with an ex-date from
  today to the latest expiry. Only a declared dividend can be known. None
  declared is not none paid, and the source says that too. When the lookup
  fails, or this process has nowhere to look, the dividends are **unknown**:
  ``None``, never an empty list.

A fleet Clerk holds no Polygon key and no FRED key, by design (ADR 0076). It
asks the coordinator, which holds both and answers with what this same
function read there (``app.services.option_model_inputs_client``). When the
coordinator cannot be reached, or its answer cannot be read, the dividends
are unknown with that reason, and the rate is the fixed default, which says
so and says why.

**Each of the two is looked up apart from the other.** The rate and the
dividends are asked for at the same time, each in a thread kept for it, and
a read waits for each no longer than its bound. A vendor that hangs leaves
its own fact not known, with that reason, and takes nothing from the other:
dividends Polygon answered at once are known however long FRED takes. A
whole read at a vendor ends inside the bound, well inside the time a Clerk
waits for the coordinator.

**One lookup runs at a time for one thing asked**, and none waits behind a
lookup of something else: a slow answer about one ticker holds up nobody
asking about another. A caller that finds its own lookup already running
waits for it to its bound, or, when it said it would not wait, is told at
once that it is running.

What is learned is kept for an hour. What could not be learned, and a rate
that is only the default, is asked again half a minute after its lookup
ended. Only so many answers are kept; the oldest go first.

Both lookups block. A caller on an event loop runs a read through
``off_the_loop``, in threads kept for it: never the loop's default ones,
where the Clerk's execution-lease heartbeat runs.

The threads are started when first needed and stopped with the app
(``close_lookup_threads``). A lookup given up on while it still waited for a
thread is never begun: nobody is waiting for what it would learn.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import operator
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from app.config import fleet_settings, settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.model_input_reasons import WhyNotKnown
from app.options.resolver import DividendOn, ModelInputs
from app.services.fred_service import RATE_SOURCE_FRED, get_risk_free_rate_and_source
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_KNOWN_FOR_MS = 60 * 60_000
_ASKED_AGAIN_AFTER_MS = 30_000
# How long a read waits for a vendor before it answers that the fact is not known. Both vendors are asked at
# once, so a whole read ends inside this: well inside the 20 seconds a Clerk waits for the coordinator
# (``app.broker.fleet.option_model_inputs``).
VENDOR_LOOKUP_BOUND_S = 8.0
# The most answers kept for each of the two facts. Past it the oldest go first.
MOST_KEPT = 256
# The roles that hold a Polygon key and a FRED key of their own, and read the vendors themselves. Named one by
# one: a role added later holds neither until it says so, and asks the coordinator.
_ROLES_THAT_READ_THE_VENDORS = frozenset({"combined", "fleet_coordinator"})

class _Threads:
    """The threads kept for one kind of work: started when first needed, and stopped with the app. A process
    that looks nothing up starts none."""

    def __init__(self, most: int, named: str) -> None:
        self._most, self._named = most, named
        self._guard = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    def submit[T](self, work: Callable[..., T], /, *args: Any) -> Future[T]:
        with self._guard:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._most, thread_name_prefix=self._named)
            return self._pool.submit(work, *args)

    def close(self) -> None:
        """Stop the threads: each ends once the work already handed over is done, and none idles on. That
        work is not cancelled, since a read may be waiting on it with no bound of its own. The next thing
        handed over starts them again."""
        with self._guard:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)


# The threads the lookups and the reads run in. A lookup that hangs holds one of its own vendor's threads and
# no other's, so a slow FRED cannot keep Polygon from being asked.
_RATE_THREADS = _Threads(4, "option-rate-lookup")
_DIVIDEND_THREADS = _Threads(8, "option-dividend-lookup")
_READ_THREADS = _Threads(8, "option-model-inputs-read")

# This process's own clock, for how long a lookup took. Never a time of day.
_monotonic = time.monotonic


def off_the_loop[T](read: Callable[[], T]) -> asyncio.Future[T]:
    """Run a blocking read of the model's inputs for a caller on an event loop, in the threads kept for it."""
    return asyncio.wrap_future(
        _READ_THREADS.submit(contextvars.copy_context().run, read), loop=asyncio.get_running_loop()
    )


def close_lookup_threads() -> None:
    """Stop the threads the rate and the dividends are looked up and read in, if this process ever started any."""
    for threads in (_RATE_THREADS, _DIVIDEND_THREADS, _READ_THREADS):
        threads.close()


class DividendsUnknown(Exception):
    """The declared dividends could not be looked up. The message is the reason, in the owner's words."""

    def __init__(self, reason: str, why: WhyNotKnown = WhyNotKnown.LOOKUP_FAILED) -> None:
        super().__init__(reason)
        self.why = why


class ModelInputsBeingRead(Exception):
    """Another caller is looking these up right now, and this one chose not to wait for it."""


@dataclass(frozen=True, slots=True)
class DeclaredDividend:
    ex_date: date
    # Dollars per share.
    cash: float


# (days to the latest expiry, the New York date) -> (the rate, ``fred_service``'s word for where it came from).
RateLookup = Callable[[int, date], tuple[float, str]]
# (underlying, first ex-date, last ex-date) -> the cash dividends declared in that span. Raises
# ``DividendsUnknown`` when it cannot say; an empty answer means it looked and none is declared.
DividendLookup = Callable[[str, date, date], Sequence[DeclaredDividend]]


@dataclass(frozen=True, slots=True)
class ModelInputsRead:
    """What was learned for one underlying at one moment."""

    rate: float
    rate_source: str
    # True when FRED gave no rate and this is the fixed default, which has no recorded origin.
    rate_is_default: bool
    # None: the dividends could not be looked up. Never an assumed "none".
    dividends: tuple[DividendOn, ...] | None
    # What was looked up, where and when; or why it could not be.
    dividends_source: str
    # When the dividends were looked up: this answer may be up to an hour old.
    dividends_read_ms: int
    # The code beside each sentence, for a refusal to carry: why the rate is only the default, and why the
    # dividends are not known. None when the rate is FRED's and when the dividends are known.
    rate_default_why: WhyNotKnown | None = None
    dividends_unknown_why: WhyNotKnown | None = None

    @property
    def model(self) -> ModelInputs | None:
        """The pricing model's inputs, or None while the dividends are not known."""
        if self.dividends is None:
            return None
        return ModelInputs(
            rate=self.rate,
            rate_source=self.rate_source,
            dividends=self.dividends,
            dividends_source=self.dividends_source,
        )

    def unverified_model(self) -> ModelInputs:
        """Inputs for a caller that must hand the resolver a model while the dividends are not known, and
        shows nothing the model says. The source carries the warning into whatever evidence is kept."""
        if self.dividends is not None:
            raise ValueError("the dividends are known; use the model they give")
        return ModelInputs(
            rate=self.rate,
            rate_source=self.rate_source,
            dividends=(),
            dividends_source=f"NOT KNOWN, priced as none and not to be shown: {self.dividends_source}",
        )


def _fred_rate(days: int, today: date) -> tuple[float, str]:
    return get_risk_free_rate_and_source(dte_days=days, observation_date=today.isoformat())


_polygon: Any = None


def _from_polygon(underlying: str, first: date, last: date) -> Sequence[DeclaredDividend]:
    global _polygon
    if _polygon is None:
        from app.services.polygon_client import PolygonClientService

        _polygon = PolygonClientService()
    try:
        rows = _polygon.list_dividends(
            ticker=underlying, ex_dividend_date_gte=first.isoformat(), ex_dividend_date_lte=last.isoformat()
        )
    except Exception as exc:
        logger.warning(
            "Polygon did not answer a dividend lookup",
            extra={"action": "option_dividends_lookup_failed", "symbol": underlying, "error_type": type(exc).__name__},
        )
        raise DividendsUnknown(
            f"Polygon did not answer when asked for {underlying}'s declared dividends.", WhyNotKnown.POLYGON_DID_NOT_ANSWER
        ) from exc
    return declared_dividends_from_polygon(underlying, rows, first, last)


def declared_dividends_from_polygon(
    underlying: str, rows: Sequence[Mapping[str, Any]], first: date, last: date
) -> tuple[DeclaredDividend, ...]:
    """Polygon's dividend rows as declared cash dividends with an ex-date from ``first`` to ``last``.

    Every kind of cash distribution counts: each takes its amount off the
    price on its ex-date. A row that cannot be read makes the whole answer
    unknown: leaving it out would be pricing as if it were not there.
    """
    declared = []
    for row in rows:
        try:
            ex_date = date.fromisoformat(str(row["ex_dividend_date"]))
            cash = float(row["cash_amount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DividendsUnknown(
                f"Polygon listed a dividend for {underlying} with no ex-date or amount this system could read.",
                WhyNotKnown.POLYGON_ANSWER_UNREADABLE,
            ) from exc
        currency = row.get("currency")
        if not (math.isfinite(cash) and cash > 0) or (currency is not None and str(currency).upper() != "USD"):
            raise DividendsUnknown(
                f"Polygon listed a dividend for {underlying} on {ex_date} that is not an amount in dollars above zero.",
                WhyNotKnown.POLYGON_ANSWER_UNREADABLE,
            )
        if first <= ex_date <= last:
            declared.append(DeclaredDividend(ex_date, cash))
    return tuple(sorted(declared, key=lambda dividend: dividend.ex_date))


def _nowhere_to_look(underlying: str, _first: date, _last: date) -> Sequence[DeclaredDividend]:
    raise DividendsUnknown(
        f"No Polygon key is set to look up {underlying}'s declared dividends with.",
        WhyNotKnown.NO_POLYGON_KEY,
    )


def declared_dividend_lookup() -> DividendLookup:
    """Where this process looks dividends up at a vendor: Polygon when it holds a key of its own, and otherwise
    nowhere. A Clerk holds none, and asks the coordinator instead."""
    if fleet_settings.ROLE in _ROLES_THAT_READ_THE_VENDORS and settings.POLYGON_API_KEY:
        return _from_polygon
    return _nowhere_to_look


@dataclass(frozen=True, slots=True)
class RateLearned:
    rate: float
    told: str
    # None: the rate is FRED's. Otherwise it is the fixed default, and this is why.
    why_default: WhyNotKnown | None


@dataclass(frozen=True, slots=True)
class DividendsLearned:
    # Each at the open of its ex-date's session. None: they could not be looked up.
    declared: tuple[DividendOn, ...] | None
    told: str
    read_ms: int
    why_unknown: WhyNotKnown | None


def the_default(because: str, why: WhyNotKnown) -> RateLearned:
    """The fixed default rate, standing in for one that is not known, and saying so."""
    told = f"The fixed default of {DEFAULT_RISK_FREE_RATE:.1%}, because {because}. The default has no recorded origin."
    return RateLearned(DEFAULT_RISK_FREE_RATE, told, why)


def _rate_from(lookup: RateLookup) -> Callable[[int, date], RateLearned]:
    def learn(days: int, today: date) -> RateLearned:
        rate, source = lookup(days, today)
        if source != RATE_SOURCE_FRED:
            return the_default("FRED gave no rate", WhyNotKnown.FRED_GAVE_NO_RATE)
        return RateLearned(rate, f"FRED Treasury bill rates, interpolated to {days} days, as published by {today}.", None)

    return learn


def _dividends_from(lookup: DividendLookup) -> Callable[[str, date, date, int], DividendsLearned]:
    def learn(underlying: str, first: date, last: date, at_ms: int) -> DividendsLearned:
        try:
            declared = tuple(lookup(underlying, first, last))
        except DividendsUnknown as unknown:
            return DividendsLearned(None, str(unknown), at_ms, unknown.why)
        span = f"an ex-date from {first} to {last}"
        told = (
            f"{len(declared)} cash dividend{'' if len(declared) == 1 else 's'} declared for {underlying} with "
            f"{span}, read {_when(at_ms)}."
            if declared
            else f"No cash dividend is declared for {underlying} with {span}, read {_when(at_ms)}. "
            "None declared is not none paid: one declared after that is not counted."
        )
        going_ex = tuple(
            DividendOn(ex_ms=session_open_ms_utc(dividend.ex_date), cash=dividend.cash) for dividend in declared
        )
        return DividendsLearned(going_ex, told, at_ms, None)

    return learn


@dataclass(frozen=True, slots=True)
class _Sources:
    """Where one read learns each fact, and how long it waits for each."""

    rate: Callable[[int, date], RateLearned]
    dividends: Callable[[str, date, date, int], DividendsLearned]
    # None: the source ends itself in bounded time. The coordinator's client has timeouts of its own.
    rate_bound_s: float | None
    dividends_bound_s: float | None


def _sources(
    underlying: str, at_ms: int, latest_expiry: date, wait: bool, rate: RateLookup | None, dividends: DividendLookup | None
) -> _Sources:
    """Where this read learns each fact: from a lookup handed in; else from the vendors, in a process that holds
    their keys; else from the coordinator."""
    if fleet_settings.ROLE in _ROLES_THAT_READ_THE_VENDORS:
        return _Sources(
            _rate_from(rate or _fred_rate),
            _dividends_from(dividends or declared_dividend_lookup()),
            VENDOR_LOOKUP_BOUND_S,
            VENDOR_LOOKUP_BOUND_S,
        )
    # A process that reads the vendors itself never loads what a Clerk asks the coordinator with.
    from app.services.option_model_inputs_client import AskedOfTheCoordinator

    asked = AskedOfTheCoordinator(underlying, at_ms, latest_expiry, wait)
    return _Sources(
        _rate_from(rate) if rate else asked.rate,
        _dividends_from(dividends) if dividends else asked.dividends,
        VENDOR_LOOKUP_BOUND_S if rate else None,
        VENDOR_LOOKUP_BOUND_S if dividends else None,
    )


# ---- what is kept, and what is being looked up ---------------------------------------


@dataclass(frozen=True, slots=True)
class _Kept[T]:
    value: T
    until_ms: int


@dataclass(slots=True)
class _Running[T]:
    """One lookup in a thread of its own."""

    answer: Future[T]
    # The moment it was asked about, and when it began on this process's own clock.
    asked_at_ms: int
    began: float
    # How long a read waits for it, counted from when it began. None: it ends itself.
    bound_s: float | None
    # Its place in the line for a thread: what is cancelled when it is given up on before it got one.
    queued: Future[None] | None = None


class _Shelf[K, T]:
    """One of the two facts: what is kept of it and what is being looked up now, each by what was asked.

    A lookup runs in a thread from ``threads``, one at a time for one thing
    asked. A read waits for it no longer than its bound. Past the bound the
    read answers ``too_slow``, and that is kept as a failure is: the lookup
    is over as far as any reader goes. Should it still learn the fact, that
    is kept in its place.
    """

    def __init__(
        self,
        threads: _Threads,
        *,
        known: Callable[[T], bool],
        kept_until_ms: Callable[[T, int], int],
        too_slow: Callable[[K, int, float], T],
    ) -> None:
        self._threads, self._known, self._kept_until_ms, self._too_slow = threads, known, kept_until_ms, too_slow
        self._guard = threading.Lock()
        self._kept: dict[K, _Kept[T]] = {}
        self._running: dict[K, _Running[T]] = {}

    def kept(self, key: K, at_ms: int) -> T | None:
        with self._guard:
            kept = self._kept.get(key)
        return None if kept is None or at_ms >= kept.until_ms else kept.value

    def look_up(self, key: K, at_ms: int, lookup: Callable[[], T], bound_s: float | None) -> tuple[_Running[T], bool]:
        """Begin looking ``key`` up, or join the lookup already running for it. Returns the lookup, and whether
        it was begun here."""
        with self._guard:
            running = self._running.get(key)
            if running is not None:
                return running, False
            running = self._running[key] = _Running(Future(), at_ms, _monotonic(), bound_s)
        try:
            running.queued = self._threads.submit(self._run, key, lookup, running)
        except BaseException:
            # Nothing is left looking like a lookup under way that no thread will ever run.
            with self._guard:
                if self._running.get(key) is running:
                    del self._running[key]
            raise
        return running, True

    def wait_for(self, key: K, at_ms: int, running: _Running[T]) -> T:
        """What the lookup learned, or that it was too slow. Raises what it raised."""
        timeout = None if running.bound_s is None else max(running.began + running.bound_s - _monotonic(), 0.0)
        try:
            return running.answer.result(timeout=timeout)
        except TimeoutError:
            if running.answer.done():
                return running.answer.result()
        assert running.bound_s is not None
        gave_up = self._too_slow(key, at_ms, running.bound_s)
        with self._guard:
            if self._running.get(key) is running:
                del self._running[key]
                self._keep(key, gave_up, self._ended_ms(running))
        if running.queued is not None:
            # Still waiting for a thread: it is never begun, and its vendor is not asked for an answer nobody
            # waits for. One already asking its vendor is not stopped by this, and what it learns is kept.
            running.queued.cancel()
        return gave_up

    def clear(self) -> None:
        with self._guard:
            self._kept.clear()
            self._running.clear()

    def __len__(self) -> int:
        return len(self._kept)

    @staticmethod
    def _ended_ms(running: _Running[T]) -> int:
        """When a lookup ended, as a moment: the moment it was asked about, and how long it has run since."""
        return running.asked_at_ms + round((_monotonic() - running.began) * 1000)

    def _run(self, key: K, lookup: Callable[[], T], running: _Running[T]) -> None:
        try:
            learned = lookup()
        except Exception as failed:
            # Whoever waits is told, and nothing is kept: a lookup the coordinator is still making, or a fault.
            with self._guard:
                if self._running.get(key) is running:
                    del self._running[key]
            running.answer.set_exception(failed)
            return
        with self._guard:
            still_waited_for = self._running.get(key) is running
            if still_waited_for:
                del self._running[key]
            # Given up on at its bound, it may still have learned the fact. That it failed after all is not news.
            if still_waited_for or self._known(learned):
                self._keep(key, learned, self._ended_ms(running))
        running.answer.set_result(learned)

    def _keep(self, key: K, learned: T, ended_ms: int) -> None:
        self._kept.pop(key, None)
        self._kept[key] = _Kept(learned, self._kept_until_ms(learned, ended_ms))
        for other in [other for other, kept in self._kept.items() if kept.until_ms <= ended_ms]:
            del self._kept[other]
        for oldest in list(self._kept)[: max(len(self._kept) - MOST_KEPT, 0)]:
            del self._kept[oldest]


def _rate_too_slow(_key: tuple[int, date], _at_ms: int, bound_s: float) -> RateLearned:
    return the_default(f"the rate's lookup had not answered after {bound_s:g} seconds", WhyNotKnown.LOOKUP_TOO_SLOW)


def _dividends_too_slow(key: tuple[str, date, date], at_ms: int, bound_s: float) -> DividendsLearned:
    return DividendsLearned(
        None,
        f"{key[0]}'s declared dividends are not known: their lookup had not answered after {bound_s:g} seconds.",
        at_ms,
        WhyNotKnown.LOOKUP_TOO_SLOW,
    )


# (days to the latest expiry, the New York date) -> the rate.
_rates: _Shelf[tuple[int, date], RateLearned] = _Shelf(
    _RATE_THREADS,
    known=lambda learned: learned.why_default is None,
    kept_until_ms=lambda learned, ended_ms: ended_ms + (_KNOWN_FOR_MS if learned.why_default is None else _ASKED_AGAIN_AFTER_MS),
    too_slow=_rate_too_slow,
)
# (underlying, first ex-date, last ex-date) -> the dividends declared in that span. The hour runs from when
# the vendor was read: what the coordinator had already kept a while is kept on a Clerk only for the rest of it.
_dividends: _Shelf[tuple[str, date, date], DividendsLearned] = _Shelf(
    _DIVIDEND_THREADS,
    known=lambda learned: learned.declared is not None,
    kept_until_ms=lambda learned, ended_ms: (
        ended_ms + _ASKED_AGAIN_AFTER_MS if learned.declared is None else learned.read_ms + _KNOWN_FOR_MS
    ),
    too_slow=_dividends_too_slow,
)


def clear_cache() -> None:
    """Forget every lookup kept (test helper)."""
    _rates.clear()
    _dividends.clear()


def _when(at_ms: int) -> str:
    return f"{datetime.fromtimestamp(at_ms / 1000, tz=UTC).astimezone(_NY):%Y-%m-%d %H:%M:%S} New York time"


def _a_plain_whole_number(at_ms: int) -> int:
    """``at_ms`` as a plain ``int``. A moment read off a bar or an array is often a numpy integer, which is the
    same moment and which a strict reader of the wire refuses."""
    if isinstance(at_ms, bool):
        raise TypeError("at_ms is a moment in whole milliseconds, not a flag")
    return operator.index(at_ms)


def read_option_model_inputs(
    underlying: str,
    *,
    at_ms: int,
    latest_expiry: date,
    rate: RateLookup | None = None,
    dividends: DividendLookup | None = None,
    wait: bool = True,
) -> ModelInputsRead:
    """The rate and the declared dividends for pricing options on ``underlying`` at ``at_ms``.

    ``latest_expiry`` is the last expiry date anything priced can have. The
    rate is the one for the days left to it, and the dividends are the ones
    with an ex-date from today to it. A dividend whose ex-date's session has
    already opened is in the stock's price, and is left out.

    What is already kept is answered at once. What is not is looked up, the
    rate and the dividends at the same time, and each is waited for no longer
    than its bound: one that is too slow is answered as not known, with that
    reason, beside whatever the other learned. A caller that finds a lookup
    of what it asks already running waits for that one, or with ``wait``
    false raises ``ModelInputsBeingRead`` at once: a page that asks every
    second must not stack a waiting thread on each ask. On a Clerk the lookup
    is one ask of the coordinator, which is told whether this caller waits
    and holds to it the same way.
    """
    at_ms = _a_plain_whole_number(at_ms)
    symbol = underlying.upper()
    today = et_date_at_ms(at_ms)
    days = max((latest_expiry - today).days, 1)
    rate_asked, dividends_asked = (days, today), (symbol, today, latest_expiry)
    nothing_to_look_up = latest_expiry < today
    learned_rate = _rates.kept(rate_asked, at_ms)
    learned_dividends = None if nothing_to_look_up else _dividends.kept(dividends_asked, at_ms)
    if learned_rate is None or not (nothing_to_look_up or learned_dividends is not None):
        sources = _sources(symbol, at_ms, latest_expiry, wait, rate, dividends)
        # Both are asked for before either is waited for: a slow one does not hold the other up.
        looking_rate = looking_dividends = None
        joined = False
        if learned_rate is None:
            looking_rate, begun = _rates.look_up(rate_asked, at_ms, partial(sources.rate, days, today), sources.rate_bound_s)
            joined |= not begun
        if learned_dividends is None and not nothing_to_look_up:
            looking_dividends, begun = _dividends.look_up(
                dividends_asked,
                at_ms,
                partial(sources.dividends, symbol, today, latest_expiry, at_ms),
                sources.dividends_bound_s,
            )
            joined |= not begun
        if joined and not wait:
            raise ModelInputsBeingRead(f"the rate and the dividends for {symbol} are being looked up")
        # Each is waited for to its own end, whatever the other came to: nothing this read began is still
        # under way when it returns.
        still_looking: ModelInputsBeingRead | None = None
        if looking_rate is not None:
            try:
                learned_rate = _rates.wait_for(rate_asked, at_ms, looking_rate)
            except ModelInputsBeingRead as elsewhere:
                still_looking = elsewhere
        if looking_dividends is not None:
            try:
                learned_dividends = _dividends.wait_for(dividends_asked, at_ms, looking_dividends)
            except ModelInputsBeingRead as elsewhere:
                still_looking = elsewhere
        if still_looking is not None:
            raise still_looking
    assert learned_rate is not None
    going_ex = learned_dividends or DividendsLearned(
        (), f"Nothing was looked up: {latest_expiry} is already past.", at_ms, None
    )
    still_to_go_ex = None
    if going_ex.declared is not None:
        still_to_go_ex = tuple(dividend for dividend in going_ex.declared if dividend.ex_ms > at_ms)
    return ModelInputsRead(
        rate=learned_rate.rate,
        rate_source=learned_rate.told,
        rate_is_default=learned_rate.why_default is not None,
        dividends=still_to_go_ex,
        dividends_source=going_ex.told,
        dividends_read_ms=going_ex.read_ms,
        rate_default_why=learned_rate.why_default,
        dividends_unknown_why=going_ex.why_unknown,
    )
