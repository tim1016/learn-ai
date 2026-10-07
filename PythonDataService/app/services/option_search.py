"""Where an option search runs: in a worker process of its own, never in the Clerk's (ADR 0076).

A search is ``app.options.resolver.resolve_contracts``. A leg picked by delta
prices every watched strike of its kind, about 20 ms each, and holds the
interpreter while it does. Run on the event loop, or in a thread of the same
process, it stops the loop for as long as it runs: the pricer lets go of the
interpreter lock for under a microsecond at a time, and a thread waiting for
it never gets in. That loop stamps the time each IBKR quote is received,
makes the supervisor's pass, and is where bots decide and send.

So a search is handed to another process, and the loop only awaits its
answer.

**Two lanes, one worker process each.** ``entry`` is a bot's own entry;
``preview`` is a page's. A bot's entry never waits behind a page. A lane's
worker is started the first time the lane is used or warmed, with the
``spawn`` start method: this process holds sockets, threads and a running
loop, and none of them may be copied into a child. A process that never
searches never starts one.

**What crosses to the worker is plain data**: the plan, the chain, the
underlying's observation, the feed's state, the model's inputs, the limits
and the two times. What comes back is the resolver's own answer, a
resolution or its typed refusal, rebuilt here as the resolver made it.

**A search that could not be run is not an answer.** A worker that could not
be started, died, or did not answer within the bound gives ``SearchNotRun``:
never a refusal of the plan, and never a guess. A worker that died is
replaced on the next search; one that did not answer is ended first.

**The preview lane has rules of its own** (``PreviewSearches``): one search
at a time for one page, at most one more waiting behind the one running, and
a bound on how long an ask waits. A search that outlives its ask is kept for
the page's next ask, when that ask still stands on what the search stood on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from app.options.american import AmericanIvStatus
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteFault
from app.options.resolver import (
    Chain,
    ContractsNotResolved,
    Evidence,
    FeedState,
    ModelInputs,
    Resolution,
    ResolverLimits,
    ResolverRefusal,
    UnderlyingObservation,
    resolve_contracts,
)
from app.schemas.action_plan import ActionPlan

logger = logging.getLogger(__name__)

# How long an ask on the preview lane waits for its own search before it is told to ask again.
PREVIEW_SEARCH_WAIT_S = 4.0
# How long a search that outlived its ask is kept for the page's next one.
PREVIEW_ANSWER_KEPT_S = 5.0
# A worker that has not answered a search for this long is ended and replaced. Several times the preview
# lane's wait: an iron condor on the default window takes about a second.
SEARCH_HANG_BOUND_S = 20.0
# The most searches the preview lane has in hand at once: one running and one waiting behind it.
_PREVIEW_SEARCHES_IN_HAND = 2

# What starting a worker process can raise: no semaphores or processes to be had, or an interpreter going down.
_CANNOT_START = (OSError, RuntimeError, NotImplementedError)


class SearchNotRunReason(StrEnum):
    """Why a search has no answer. None of these says anything about the plan or the market."""

    # The worker process could not be started, or ended before it answered. The next search starts another.
    WORKER_FAILED = "OPTION_SEARCH_WORKER_FAILED"
    # The worker had not answered within the bound, and was ended. The next search starts another.
    WORKER_DID_NOT_ANSWER = "OPTION_SEARCH_WORKER_DID_NOT_ANSWER"
    # The search raised in the worker, or what was asked or answered could not cross between the processes.
    FAILED = "OPTION_SEARCH_FAILED"
    # The preview lane only. A search for this page is already running: it is not run twice.
    ALREADY_RUNNING = "OPTION_SEARCH_ALREADY_RUNNING"
    # The preview lane only. One search is running and another is waiting: a third is not queued.
    LANE_BUSY = "OPTION_SEARCH_LANE_BUSY"
    # The preview lane only. The search is running and this ask has waited its time. Its answer is kept.
    NOT_DONE_IN_TIME = "OPTION_SEARCH_NOT_DONE_IN_TIME"

    @property
    def still_running(self) -> bool:
        """True when a search is running or about to, and asking again a moment later will find it."""
        return self in {self.ALREADY_RUNNING, self.LANE_BUSY, self.NOT_DONE_IN_TIME}


@dataclass(frozen=True, slots=True)
class SearchNotRun:
    """A search that gave no answer, and why, as a clause."""

    reason: SearchNotRunReason
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchAsk:
    """One search: what ``resolve_contracts`` is handed, and nothing else."""

    plan: ActionPlan
    chain: Chain
    underlying: UnderlyingObservation
    feed: FeedState
    model: ModelInputs
    limits: ResolverLimits
    decision_bar_close_ms: int
    selection_ms: int

    def as_plain_data(self) -> SearchAsk:
        """The same ask with nothing in it that cannot be sent to another process. A chain's watched contracts
        are handed about as a read-only view, and a view does not pickle."""
        return replace(self, chain=replace(self.chain, watched=dict(self.chain.watched)))


# What a search comes to: the resolver's own answer, or that it could not be run.
Searched = Resolution | ContractsNotResolved | SearchNotRun


# ---- what the worker process runs -----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Resolved:
    """A resolution as it crosses back: its read-only views as the plain mappings they are views of."""

    picked: dict[str, OptionContract]
    quotes: dict[str, HeldQuote]
    sold: frozenset[str]
    multipliers: dict[str, int]
    evidence: Evidence

    def as_the_resolver_made_it(self) -> Resolution:
        return Resolution(
            picked=MappingProxyType(self.picked),
            quotes=MappingProxyType(self.quotes),
            sold=self.sold,
            multipliers=MappingProxyType(self.multipliers),
            evidence=self.evidence,
        )


@dataclass(frozen=True, slots=True)
class _Refused:
    """The resolver's typed refusal as it crosses back. An exception with arguments of its own does not unpickle."""

    reason: ResolverRefusal
    leg_ids: tuple[str, ...]
    detail: str
    fault: QuoteFault | AmericanIvStatus | None
    contract: OptionContract | None
    evidence: Evidence

    def as_the_resolver_made_it(self) -> ContractsNotResolved:
        return ContractsNotResolved(
            self.reason, self.leg_ids, self.detail, fault=self.fault, contract=self.contract, evidence=self.evidence
        )


@dataclass(frozen=True, slots=True)
class _NotAQuestion:
    """What was asked could not describe a market: the caller's mistake, which the resolver raises as ``ValueError``."""

    detail: str


def search(asked: SearchAsk) -> _Resolved | _Refused | _NotAQuestion:
    """Run one search. This is what a lane's worker process runs, and the one place outside the resolver's
    own package that calls it."""
    try:
        resolution = resolve_contracts(
            asked.plan,
            chain=asked.chain,
            underlying=asked.underlying,
            feed=asked.feed,
            model=asked.model,
            limits=asked.limits,
            decision_bar_close_ms=asked.decision_bar_close_ms,
            selection_ms=asked.selection_ms,
        )
    except ContractsNotResolved as refused:
        return _Refused(
            refused.reason, refused.leg_ids, str(refused), refused.fault, refused.contract, refused.evidence
        )
    except ValueError as mistake:
        return _NotAQuestion(str(mistake))
    return _Resolved(
        dict(resolution.picked),
        dict(resolution.quotes),
        resolution.sold,
        dict(resolution.multipliers),
        resolution.evidence,
    )


def _worker_starts() -> None:
    """Runs once in a worker. Loading this module there has loaded the resolver, the calendar and the pricer."""
    # A terminal's interrupt goes to the whole process group. The Clerk ends its workers itself.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _worker_is_up() -> int:
    """The worker's own process id: the answer to the lane's first call, which starts it."""
    return os.getpid()


def a_worker_process() -> ProcessPoolExecutor:
    """One worker process. Spawned, never forked: this process holds sockets, threads and a running loop."""
    return ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn"), initializer=_worker_starts
    )


# ---- one lane -------------------------------------------------------------------


class OptionSearchLane:
    """One worker process, and one search in it at a time, in the order asked."""

    def __init__(
        self,
        name: str,
        *,
        executor: Callable[[], Executor] = a_worker_process,
        hang_bound_s: float = SEARCH_HANG_BOUND_S,
    ) -> None:
        self.name = name
        self._make = executor
        self._hang_bound_s = hang_bound_s
        self._pool: Executor | None = None
        # The worker's answer to its first call: its process id.
        self._up: Future[int] | None = None
        self._worker_pid: int | None = None
        self._turn: asyncio.Lock | None = None
        self._turn_loop: asyncio.AbstractEventLoop | None = None
        self._in_hand: set[asyncio.Task[Searched]] = set()

    @property
    def in_hand(self) -> int:
        """The searches running or waiting their turn."""
        return len(self._in_hand)

    @property
    def worker_pid(self) -> int | None:
        """The worker's process id, once it has answered that it is up."""
        return self._worker_pid

    def warm(self) -> None:
        """Start the worker now, so that the first search does not wait for it to load. Call it off the path
        of the ask that will need it: a page's when its window is first asked for, a bot's when it starts."""
        if self._pool is not None:
            return
        try:
            self._start()
        except _CANNOT_START:
            # The search that needs the worker tries again, and says so if it cannot be had.
            logger.exception(
                "An option search worker could not be started ahead of its first search",
                extra={"action": "option_search_worker_not_warmed", "lane": self.name},
            )

    def start(self, ask: SearchAsk) -> asyncio.Task[Searched]:
        """Begin a search and return it. It runs to its answer whoever stops waiting for it."""
        task = asyncio.get_running_loop().create_task(self._in_turn(ask), name=f"option-search-{self.name}")
        self._in_hand.add(task)
        task.add_done_callback(self._in_hand.discard)
        return task

    async def search(self, ask: SearchAsk) -> Searched:
        """One search's answer, or that it could not be run. A caller's mistake raises ``ValueError``."""
        return await asyncio.shield(self.start(ask))

    async def resolve(
        self,
        plan: ActionPlan,
        *,
        chain: Chain,
        underlying: UnderlyingObservation,
        feed: FeedState,
        model: ModelInputs,
        limits: ResolverLimits,
        decision_bar_close_ms: int,
        selection_ms: int,
    ) -> Resolution | SearchNotRun:
        """``resolve_contracts``, in this lane's worker process.

        Takes what it takes, returns what it returns and raises what it
        raises: ``ContractsNotResolved`` for a refusal, ``ValueError`` for a
        caller's mistake. A search that could not be run is returned as
        ``SearchNotRun``, which is neither.
        """
        found = await self.search(
            SearchAsk(
                plan=plan,
                chain=chain,
                underlying=underlying,
                feed=feed,
                model=model,
                limits=limits,
                decision_bar_close_ms=decision_bar_close_ms,
                selection_ms=selection_ms,
            )
        )
        if isinstance(found, ContractsNotResolved):
            raise found
        return found

    def close(self) -> None:
        """Stop the lane. A search in hand is not waited for."""
        searching = bool(self._in_hand)
        for task in tuple(self._in_hand):
            task.cancel()
        if searching:
            self._end_worker()
        else:
            self._drop_worker()

    # ---- the worker -----------------------------------------------------------

    def _start(self) -> None:
        pool = self._make()
        self._pool, self._worker_pid = pool, None
        # The first call starts the process and loads the resolver in it.
        self._up = pool.submit(_worker_is_up)

    async def _worker(self) -> Executor:
        """The lane's worker, started and loaded."""
        if self._pool is None:
            self._start()
        assert self._pool is not None and self._up is not None
        if self._worker_pid is None:
            self._worker_pid = await asyncio.wait_for(asyncio.wrap_future(self._up), self._hang_bound_s)
        return self._pool

    def _drop_worker(self) -> None:
        """Let go of the worker. The next search starts another."""
        pool, self._pool, self._up, self._worker_pid = self._pool, None, None, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _end_worker(self) -> None:
        """End a worker that is still searching: nothing else stops a search once it has begun."""
        pid = self._worker_pid
        self._drop_worker()
        if pid is not None and pid != os.getpid():
            # It may have gone by itself a moment ago.
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))

    def _its_turn(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._turn_loop is not loop:
            self._turn_loop, self._turn = loop, asyncio.Lock()
        assert self._turn is not None
        return self._turn

    async def _in_turn(self, ask: SearchAsk) -> Searched:
        async with self._its_turn():
            try:
                worker = await self._worker()
                handed = worker.submit(search, ask.as_plain_data())
            except TimeoutError:
                self._end_worker()
                return self._not_run(SearchNotRunReason.WORKER_DID_NOT_ANSWER, "its worker process did not start in time")
            except _CANNOT_START:
                self._drop_worker()
                return self._not_run(SearchNotRunReason.WORKER_FAILED, "its worker process could not be started", failed=True)
            try:
                found = await asyncio.wait_for(asyncio.wrap_future(handed), self._hang_bound_s)
            except TimeoutError:
                self._end_worker()
                return self._not_run(
                    SearchNotRunReason.WORKER_DID_NOT_ANSWER,
                    f"its worker process had not answered after {self._hang_bound_s:g} seconds, and was ended",
                )
            except _CANNOT_START:
                # A worker that dies takes its pool with it.
                self._drop_worker()
                return self._not_run(
                    SearchNotRunReason.WORKER_FAILED, "its worker process ended before it answered", failed=True
                )
            except Exception:
                # Not the resolver's refusal and not a caller's mistake: those come back as answers. A fault in
                # the search itself, or something that would not cross to the worker or back.
                return self._not_run(
                    SearchNotRunReason.FAILED, "it failed in its worker process, or could not be handed to it", failed=True
                )
        if isinstance(found, _NotAQuestion):
            raise ValueError(found.detail)
        return found.as_the_resolver_made_it()

    def _not_run(self, reason: SearchNotRunReason, detail: str, *, failed: bool = False) -> SearchNotRun:
        logger.error(
            "An option search could not be run",
            extra={"action": "option_search_not_run", "lane": self.name, "reason": str(reason), "detail": detail},
            exc_info=failed,
        )
        return SearchNotRun(reason, detail)


# ---- the preview lane's rules ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Found[T]:
    """A search's answer, and what the ask that began it stood on. That may be an earlier ask of the same page."""

    standing_on: T
    outcome: Resolution | ContractsNotResolved


@dataclass(slots=True)
class _Flight[T]:
    standing_on: T
    # Set once its ask has stopped waiting: the answer is then kept for the page's next ask.
    outlived_its_ask: bool = False


@dataclass(frozen=True, slots=True)
class _Kept[T]:
    found: Found[T]
    landed_at: float


class PreviewSearches[T]:
    """A page's searches, on a lane of their own.

    ``key`` names the page. ``standing_on`` is whatever the caller's answer
    is built from beside the search's own: it is handed back with the
    answer, and two asks stand on the same thing when theirs compare equal.

    - One search at a time for one page. A second ask while it runs is told
      it is running, and no second search is begun.
    - One search running and one waiting, for every page together. A third
      is told the lane is busy.
    - An ask waits ``wait_s`` for its own search. Past that it is told to ask
      again, and the search's answer is kept ``kept_for_s`` for the page's
      next ask: handed over when that ask stands on the same thing, and
      dropped when it does not.
    """

    def __init__(
        self,
        lane: OptionSearchLane,
        *,
        wait_s: float = PREVIEW_SEARCH_WAIT_S,
        kept_for_s: float = PREVIEW_ANSWER_KEPT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lane = lane
        self._wait_s, self._kept_for_s, self._clock = wait_s, kept_for_s, clock
        self._flying: dict[str, _Flight[T]] = {}
        self._kept: dict[str, _Kept[T]] = {}

    def warm(self) -> None:
        self.lane.warm()

    async def search(self, key: str, ask: SearchAsk, *, standing_on: T) -> Found[T] | SearchNotRun:
        now = self._clock()
        for stale in [page for page, kept in self._kept.items() if now - kept.landed_at > self._kept_for_s]:
            del self._kept[stale]
        kept = self._kept.pop(key, None)
        if kept is not None and kept.found.standing_on == standing_on:
            return kept.found
        if key in self._flying:
            return SearchNotRun(SearchNotRunReason.ALREADY_RUNNING, "a search for this preview is already running")
        if self.lane.in_hand >= _PREVIEW_SEARCHES_IN_HAND:
            return SearchNotRun(
                SearchNotRunReason.LANE_BUSY, "other previews' searches are running and waiting ahead of this one"
            )
        flight = self._flying[key] = _Flight(standing_on)
        task = self.lane.start(ask)
        task.add_done_callback(lambda done: self._landed(key, flight, done))
        try:
            outcome = await asyncio.wait_for(asyncio.shield(task), self._wait_s)
        except TimeoutError:
            if not task.done():
                flight.outlived_its_ask = True
                return SearchNotRun(
                    SearchNotRunReason.NOT_DONE_IN_TIME, f"the search had not finished after {self._wait_s:g} seconds"
                )
            # It finished as the wait ran out.
            outcome = task.result()
        except asyncio.CancelledError:
            # The page hung up. Its next ask may still be answered by this search.
            flight.outlived_its_ask = True
            raise
        if isinstance(outcome, SearchNotRun):
            return outcome
        return Found(standing_on, outcome)

    def close(self) -> None:
        self.lane.close()
        self._flying.clear()
        self._kept.clear()

    def _landed(self, key: str, flight: _Flight[T], done: asyncio.Task[Searched]) -> None:
        if self._flying.get(key) is flight:
            del self._flying[key]
        if done.cancelled() or not flight.outlived_its_ask:
            return
        failed = done.exception()
        if failed is not None:
            logger.error(
                "A preview's search failed after its ask had stopped waiting",
                extra={"action": "option_search_failed_unwatched", "lane": self.lane.name},
                exc_info=failed,
            )
            return
        outcome = done.result()
        if not isinstance(outcome, SearchNotRun):
            self._kept[key] = _Kept(Found(flight.standing_on, outcome), self._clock())


# ---- this process's lanes -------------------------------------------------------


class OptionSearches:
    """This process's two lanes."""

    def __init__(self, *, executor: Callable[[], Executor] = a_worker_process) -> None:
        self.entry = OptionSearchLane("entry", executor=executor)
        self.preview: PreviewSearches[object] = PreviewSearches(OptionSearchLane("preview", executor=executor))

    def close(self) -> None:
        self.entry.close()
        self.preview.close()


_searches: OptionSearches | None = None


def get_option_searches() -> OptionSearches:
    """Where this process's option searches run. Asking for it starts no worker; a search or a warm-up does."""
    global _searches
    if _searches is None:
        _searches = OptionSearches()
    return _searches


def close_option_searches() -> None:
    """Stop this process's search workers, if it ever started any."""
    global _searches
    searches, _searches = _searches, None
    if searches is not None:
        searches.close()


__all__ = [
    "PREVIEW_ANSWER_KEPT_S",
    "PREVIEW_SEARCH_WAIT_S",
    "SEARCH_HANG_BOUND_S",
    "Found",
    "OptionSearchLane",
    "OptionSearches",
    "PreviewSearches",
    "SearchAsk",
    "SearchNotRun",
    "SearchNotRunReason",
    "Searched",
    "a_worker_process",
    "close_option_searches",
    "get_option_searches",
    "search",
]
