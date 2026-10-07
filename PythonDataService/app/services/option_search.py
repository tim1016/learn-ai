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
never a refusal of the plan, and never a guess. An error the search itself
raised is the search's failure, and leaves its worker as it was.

**A lane holds its worker's process from the moment it is spawned, and never
has two.** A worker is up within the bound of being started, and answers a
search within the bound of being handed it. One that does neither is killed
and reaped, and only then is another started: by the search that was waiting
for it, or, when none was, by the next use of the lane. A search that comes
while a worker is still coming up waits for that worker. Closing a lane kills
and reaps its worker whatever it is doing, so nothing is left to keep this
process from ending.

**The preview lane has rules of its own** (``PreviewSearches``): one search
at a time for one page, at most one more waiting behind the one running, and
a bound on how long an ask waits. A search that outlives its ask answers
nobody: the quotes it read are by then too old to show, and the page's next
ask searches again.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from concurrent.futures import BrokenExecutor, Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from multiprocessing import connection
from multiprocessing.process import BaseProcess
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
# A worker that is not up this long after it was started, or has not answered a search this long after it
# was handed one, is ended and replaced. Several times the preview lane's wait: a worker loads in about a
# second, and an iron condor on the default window takes about as long.
SEARCH_HANG_BOUND_S = 20.0
# How long a worker that was just killed is waited for. One is gone in milliseconds; this only bounds the wait.
KILLED_WORKER_WAIT_S = 2.0
# The most searches the preview lane has in hand at once: one running and one waiting behind it.
_PREVIEW_SEARCHES_IN_HAND = 2

# What starting a worker process, or handing it a search, can raise: no semaphores or processes to be had, an
# interpreter going down, or a worker found dead (the pool's own error for that is a ``RuntimeError``).
_CANNOT_START = (OSError, RuntimeError, NotImplementedError)


# This process's own clock, for how long ago a worker was started. Never a time of day.
_monotonic = time.monotonic


class _NoAnswerInTime(Exception):
    """A worker had not answered when the lane's own bound ran out. Nothing a worker raises is this."""


class SearchNotRunReason(StrEnum):
    """Why a search has no answer. None of these says anything about the plan or the market."""

    # The worker process could not be started, or ended before it answered. The next search starts another.
    WORKER_FAILED = "OPTION_SEARCH_WORKER_FAILED"
    # The worker was not up, or had not answered, within the bound, and was ended. The next search starts another.
    WORKER_DID_NOT_ANSWER = "OPTION_SEARCH_WORKER_DID_NOT_ANSWER"
    # The search raised in the worker, or what was asked or answered could not cross between the processes.
    FAILED = "OPTION_SEARCH_FAILED"
    # The preview lane only. A search for this page is already running: it is not run twice.
    ALREADY_RUNNING = "OPTION_SEARCH_ALREADY_RUNNING"
    # The preview lane only. One search is running and another is waiting: a third is not queued.
    LANE_BUSY = "OPTION_SEARCH_LANE_BUSY"
    # The preview lane only. The search is running and this ask has waited its time. The next ask searches again.
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


# ---- a worker's own process -------------------------------------------------------


def _spawned_by(pool: Executor) -> tuple[BaseProcess, ...]:
    """The processes a pool has spawned. A ``ProcessPoolExecutor`` keeps each from the moment it starts it,
    and nothing public names them: a test pins that this record holds a lane's worker. An executor of any
    other kind has no process of its own."""
    if not isinstance(pool, ProcessPoolExecutor):
        return ()
    return tuple((getattr(pool, "_processes", None) or {}).values())


def _worker_process_of(pool: Executor) -> BaseProcess | None:
    """The one process of a pool of one, once its first call has been handed to it. None for an executor with
    no process of its own. A process pool that does not show its one process cannot be ended, and is not used."""
    if not isinstance(pool, ProcessPoolExecutor):
        return None
    spawned = _spawned_by(pool)
    if len(spawned) != 1:
        raise RuntimeError(f"a search worker's pool shows {len(spawned)} processes where it must show its one")
    return spawned[0]


def _has_gone(process: BaseProcess) -> bool:
    """Whether a process has ended. Its sentinel reads as ready from then on, whichever thread reaps it."""
    return bool(connection.wait([process.sentinel], timeout=0))


def _kill(process: BaseProcess, lane: str) -> None:
    """Kill a worker's process and reap it, whatever it is doing. Nothing else stops a search once it has
    begun, or a worker that is stuck coming up. One already dead is only reaped."""
    try:
        process.kill()
    except OSError:
        logger.exception(
            "An option search worker could not be sent its kill",
            extra={"action": "option_search_worker_kill_failed", "lane": lane, "pid": process.pid},
        )
    process.join(KILLED_WORKER_WAIT_S)
    if not _has_gone(process):
        logger.error(
            "An option search worker was killed and has not gone",
            extra={"action": "option_search_worker_not_gone", "lane": lane, "pid": process.pid},
        )


async def _answer_within[T](answer: Future[T], seconds: float) -> T:
    """A worker's answer, waited for no longer than ``seconds``: past that, ``_NoAnswerInTime``. Whatever the
    worker's own call raised is raised as it was, a ``TimeoutError`` of its own included."""
    if answer.done():
        return answer.result()
    bound = asyncio.timeout(max(seconds, 0.0))
    try:
        async with bound:
            return await asyncio.wrap_future(answer)
    except TimeoutError:
        if bound.expired():
            raise _NoAnswerInTime from None
        raise


# ---- one lane -------------------------------------------------------------------


class OptionSearchLane:
    """One worker process, and one search in it at a time, in the order asked.

    The lane holds its worker's process from the moment it is spawned, and
    never has two: a worker is killed and reaped before another is started.
    """

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
        # The worker's own process, held from the moment it is spawned. None for an executor that has none.
        self._process: BaseProcess | None = None
        # When the worker was started, on this process's own clock. It is up within the bound of this, or ended.
        self._started_at = 0.0
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
        of the ask that will need it: a page's when its window is first asked for, a bot's when it starts.
        A worker started earlier that never came up is ended first."""
        self._end_a_worker_that_never_came_up()
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
        """Stop the lane. Its worker is killed and reaped whatever it is doing: coming up, searching, hung, or
        dead already. A search in hand is not waited for. Closing a lane that is closed does nothing."""
        for task in tuple(self._in_hand):
            task.cancel()
        self._end_worker()

    # ---- the worker -----------------------------------------------------------

    def _start(self) -> None:
        """Spawn the lane's worker. The lane has none when this is called."""
        pool = self._make()
        try:
            # The first call starts the process and loads the resolver in it.
            up = pool.submit(_worker_is_up)
            process = _worker_process_of(pool)
        except BaseException:
            # Nothing half started is left behind: not a process the pool spawned before it failed.
            for spawned in _spawned_by(pool):
                _kill(spawned, self.name)
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        self._pool, self._process, self._up, self._worker_pid = pool, process, up, None
        self._started_at = _monotonic()

    def _end_a_worker_that_never_came_up(self) -> None:
        """A worker started longer ago than the bound that has still not said it is up, with no search waiting
        for it: one started ahead of its first search. It is ended, so that the lane can start another."""
        if self._up is None or self._up.done() or _monotonic() - self._started_at < self._hang_bound_s:
            return
        logger.error(
            "An option search worker never came up, and was ended",
            extra={"action": "option_search_worker_never_came_up", "lane": self.name, "bound_s": self._hang_bound_s},
        )
        self._end_worker()

    async def _worker(self) -> Executor:
        """The lane's worker, started and loaded. It is up within the bound of being started, whoever started
        it and however long ago: past that this raises ``_NoAnswerInTime``."""
        self._end_a_worker_that_never_came_up()
        if self._pool is None:
            self._start()
        assert self._pool is not None and self._up is not None
        if self._worker_pid is None:
            left = self._started_at + self._hang_bound_s - _monotonic()
            self._worker_pid = await _answer_within(self._up, left)
        return self._pool

    def _end_worker(self) -> None:
        """End the lane's worker, whatever it is doing: its process is killed and reaped before this returns,
        and its pool let go of. With the process dead the pool's own thread ends by itself. The next search
        starts another worker."""
        pool, process = self._pool, self._process
        self._pool = self._process = self._up = self._worker_pid = None
        if process is not None:
            _kill(process, self.name)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _its_turn(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._turn_loop is not loop:
            self._turn_loop, self._turn = loop, asyncio.Lock()
        assert self._turn is not None
        return self._turn

    async def _hand_over(self, ask: SearchAsk) -> Future[_Resolved | _Refused | _NotAQuestion] | SearchNotRun:
        """Hand a search to the worker, starting one when there is none. A worker found gone before anything
        was handed to it, as one killed while it was idle, is replaced once: this search did not break it."""
        plain = ask.as_plain_data()
        for last_try in (False, True):
            had_a_worker = self._pool is not None
            try:
                worker = await self._worker()
                return worker.submit(search, plain)
            except _NoAnswerInTime:
                self._end_worker()
                return self._not_run(
                    SearchNotRunReason.WORKER_DID_NOT_ANSWER,
                    f"its worker process was not up {self._hang_bound_s:g} seconds after it was started, and was ended",
                )
            except _CANNOT_START:
                self._end_worker()
                if last_try or not had_a_worker:
                    return self._not_run(
                        SearchNotRunReason.WORKER_FAILED, "its worker process could not be started", failed=True
                    )
        raise AssertionError("a search is handed over or said not to have been")

    async def _in_turn(self, ask: SearchAsk) -> Searched:
        async with self._its_turn():
            handed = await self._hand_over(ask)
            if isinstance(handed, SearchNotRun):
                return handed
            try:
                found = await _answer_within(handed, self._hang_bound_s)
            except _NoAnswerInTime:
                self._end_worker()
                return self._not_run(
                    SearchNotRunReason.WORKER_DID_NOT_ANSWER,
                    f"its worker process had not answered after {self._hang_bound_s:g} seconds, and was ended",
                )
            except BrokenExecutor:
                # The worker ended under the search, and took its pool with it.
                self._end_worker()
                return self._not_run(
                    SearchNotRunReason.WORKER_FAILED, "its worker process ended before it answered", failed=True
                )
            except Exception:
                # Not the resolver's refusal and not a caller's mistake: those come back as answers. A fault in
                # the search itself, whatever it raised, or something that would not cross to the worker or
                # back. The worker is as it was, and takes the next search.
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


@dataclass(slots=True)
class _Flight:
    """One page's search, running or waiting its turn."""

    # Set once its ask has stopped waiting for it.
    outlived_its_ask: bool = False


class PreviewSearches:
    """A page's searches, on a lane of their own. ``key`` names the page.

    - One search at a time for one page. A second ask while it runs is told
      it is running, and no second search is begun.
    - One search running and one waiting, for every page together. A third
      is told the lane is busy.
    - An ask waits ``wait_s`` for its own search. Past that it is told to ask
      again. Nothing stops a search once it has begun, so it runs to its end,
      and its answer is shown to nobody: the page's next ask searches again,
      on quotes read then.

    A late answer is not kept for the next ask. It stands on quotes read
    before the search began, and the page is told to ask again in a second:
    its next ask comes when those quotes are older than a pick may stand on
    (the age limit the resolver is handed), so a kept answer could only ever
    be one the resolver itself would call stale. A test of the preview holds
    the wait, the second and that limit to this.
    """

    def __init__(self, lane: OptionSearchLane, *, wait_s: float = PREVIEW_SEARCH_WAIT_S) -> None:
        self.lane = lane
        self._wait_s = wait_s
        self._flying: dict[str, _Flight] = {}

    def warm(self) -> None:
        self.lane.warm()

    async def search(self, key: str, ask: SearchAsk) -> Searched:
        """This ask's own search: the resolver's answer, or that the search has none for this ask. A caller's
        mistake raises ``ValueError``."""
        if key in self._flying:
            return SearchNotRun(SearchNotRunReason.ALREADY_RUNNING, "a search for this preview is already running")
        if self.lane.in_hand >= _PREVIEW_SEARCHES_IN_HAND:
            return SearchNotRun(
                SearchNotRunReason.LANE_BUSY, "other previews' searches are running and waiting ahead of this one"
            )
        flight = self._flying[key] = _Flight()
        task = self.lane.start(ask)
        task.add_done_callback(lambda done: self._landed(key, flight, done))
        try:
            return await asyncio.wait_for(asyncio.shield(task), self._wait_s)
        except TimeoutError:
            if task.done():
                # It finished as the wait ran out.
                return task.result()
            flight.outlived_its_ask = True
            return SearchNotRun(
                SearchNotRunReason.NOT_DONE_IN_TIME, f"the search had not finished after {self._wait_s:g} seconds"
            )
        except asyncio.CancelledError:
            # The page hung up.
            flight.outlived_its_ask = True
            raise

    def close(self) -> None:
        self.lane.close()
        self._flying.clear()

    def _landed(self, key: str, flight: _Flight, done: asyncio.Task[Searched]) -> None:
        if self._flying.get(key) is flight:
            del self._flying[key]
        if done.cancelled() or not flight.outlived_its_ask:
            return
        # Nobody is waiting for it: what it raised would otherwise be seen by nobody.
        failed = done.exception()
        if failed is not None:
            logger.error(
                "A preview's search failed after its ask had stopped waiting",
                extra={"action": "option_search_failed_unwatched", "lane": self.lane.name},
                exc_info=failed,
            )


# ---- this process's lanes -------------------------------------------------------


class OptionSearches:
    """This process's two lanes."""

    def __init__(self, *, executor: Callable[[], Executor] | None = None) -> None:
        executor = executor or a_worker_process
        self.entry = OptionSearchLane("entry", executor=executor)
        self.preview = PreviewSearches(OptionSearchLane("preview", executor=executor))

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
    "PREVIEW_SEARCH_WAIT_S",
    "SEARCH_HANG_BOUND_S",
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
