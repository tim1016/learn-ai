"""The download phase of a run: bring into the lake everything the run can need (#3030).

Formula: none. For each session that needs data, in date order: the underlying's raw minute bars, the session's
  listing of option contracts, and then the minute bars of each contract ``session_needs`` names that is not stored.
Reference: the order of ``run_sessions`` (a session whose window does not fit reads nothing; any other reads the
  underlying's day, the listing and the needed contracts); and the lake's own capture functions, which are the only way
  anything is fetched here.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_fetching.py, test_fetching_liveness.py.

Everything is fetched through the lake's functions and nothing else, so its rules decide what may be fetched and how a
failure reads: ``materialize_engine_run`` for the underlying, ``ensure_option_listing`` for a listing and
``ensure_option_day_bars`` for a contract's day. A typed failure from any of them for something a session needs stops
the phase with ``DownloadFailed``, naming the session and the lake's reason code (never its words, which can hold a
date or a vendor's JSON): a run never goes on over data that was only partly fetched. The stock pipeline's error for
the underlying is a sentence with no code, so its code is asked of the catalog, where the lake wrote the reason of each
day it could not fetch; when no day of the chunk has one the failure is ``UNDERLYING_NOT_FETCHED``. The listing of a
session is read from the lake once and handed to every contract capture of that session, so it is not parsed again for
each contract. The lake is asked once which of a session's contract days are captured, and only the rest are captured;
a captured day whose file does not read is captured again, as the check promised. The lake fetches such a day again
whenever its own parser refuses the file (the reuse gate runs that parser). A day that parser accepts and this
feature's reader refuses, a bar closing on another New York date, is held as good: the capture reuses it with no
request, and the replay is where it ends the run; the check refuses such a session first and promises no fetch for it
(``data_check``'s table of stored days).

Order. The underlying's bars are asked for in chunks of ``UNDERLYING_CHUNK_SESSIONS`` sessions, each chunk just before
the first session in it, so the progress counts sessions from the start; then, for each session, its listing and its
contracts. A chunk holds only sessions the run needs, and the stock pipeline is asked for the bars of every session from
the first date of a request to its last, so a chunk is asked for in one call for each stretch of sessions that follow one
another in the calendar and are all needed: a session the run skips (its window does not fit) is in no request, and a
failure on it cannot fail the run. The stock pipeline has no gate on a session that is not over: asked for one, it
stores the minutes so far as the whole day. Check data refuses such a session first, but this phase is a function of its
own, so before a chunk is asked for, the lake's own rule (``option_lake.session_refusal``) is asked of each session in
it, and a refused one is a ``DownloadFailed`` with the lake's reason and the pipeline is not called for the chunk.

Limits on the vendor. At most ``MAX_VENDOR_REQUESTS_IN_FLIGHT`` option requests are in flight in the whole process:
every capture here holds ``option_lake.vendor_request_gate``, which every run and the past chain's download share, so
two runs at once do not double it. A day another request is fetching right now is not a failure: the capture waits and
asks again for up to ``OTHER_REQUEST_WAIT_SECONDS``, and only then is it ``another_request_still_fetching``.

Saying so. While a capture waits for a day another request is fetching nothing else moves: the phase, the progress and
the log stay as they were, and a healthy run would look hung for as long as six minutes. The captures run on the
background loop and do not call the job's log hook, so each batch keeps a count of the captures that are waiting on
another request (``_Batch``): a capture is counted from its first ``lease_timeout`` to the moment it leaves the wait,
however it leaves, and a batch notes when a capture of it has failed. The job's own thread reads the count once a
slice and writes ``WAITING_FOR_ANOTHER_REQUEST`` when it goes from none to some, and ``THE_OTHER_REQUEST_FINISHED``
when it goes back to none, so a wait is said once and its end once. Neither line is ever untrue: the count is of the
captures waiting now, so a wait that began and ended between two looks is not announced afterwards, and the second line
is written only when no capture of the batch has failed and the phase was not told to stop, so a run that is failing or
stopping never says it carries on.

Liveness. The job's lease is renewed only by an emit or a stop poll, so no wait here is longer than
``STOP_POLL_SECONDS`` without asking ``should_stop``. A yes while contract requests are in flight lets those finish
(a request cut off half way could leave a file with no receipt), starts no other, and ends the phase when they have, or
after ``STOP_GRACE_SECONDS`` at the latest; a capture still running then is cancelled as a last resort, and the row it
had claimed stays ``fetching`` until its lease ends (``LEASE_TTL_MS``), after which the next ask for that day takes it
over. The same holds when a wait runs out: the batch is told to stop before the run is failed, and so it is when the
job's thread leaves a wait by an exception, so nothing keeps asking the vendor for a run that has ended. The
underlying's calls to the lake have no way to be cut short: a stop abandons the one in progress, and the thread that runs
it goes on in the background through the rest of its sessions, up to ``UNDERLYING_CHUNK_SESSIONS`` stock requests, whose
days are stored for a rerun to reuse; the stretches of the chunk after it are not asked for.

The functions of the lake that fetch options are coroutines whose catalog pool belongs to one event loop; a worker
thread has none. The phase hands each coroutine to the process's background loop, opens that loop's pool first, and
waits for it in short slices.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import Future
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Any

from app.data_lake import catalog_client
from app.data_lake.option_bars import captured_option_days, ensure_option_day_bars
from app.data_lake.option_lake import LEASE_TTL_MS, OptionCaptureResult, session_refusal, vendor_request_gate
from app.data_lake.option_listing import OptionListing, ensure_option_listing
from app.data_lake.run_materialization import LakeMaterializationError, materialize_engine_run
from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.data_check import STORED_FILE_ERRORS, DataKind, DataThing
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.lake_data import LakeRunData, captured_listing_of
from app.options_backtest.needs import session_needs
from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.background_loop import CallerStoppedWaitingError, background_loop

logger = logging.getLogger(__name__)

# Sessions of the underlying asked for in one call. That call waits at most ten minutes for a window and reports
# nothing until it returns, so a long range is asked for in chunks: each has its own wait, log lines and a stop check.
UNDERLYING_CHUNK_SESSIONS = 25
# How long a wait goes by before the phase asks whether to stop. The job's lease lasts five minutes and a stop poll
# renews it, so this is far inside it.
STOP_POLL_SECONDS = 5.0
# How long requests already in flight are given to finish once the phase has been told to stop.
STOP_GRACE_SECONDS = 60.0
# How long a capture waits for another request that is fetching the same day: one lease of the lake, after which the
# row can be taken over, and a minute over.
OTHER_REQUEST_WAIT_SECONDS = LEASE_TTL_MS / 1000 + 60.0
# How often it asks again meanwhile. An ask is a claim attempt on the catalog, not a vendor request.
OTHER_REQUEST_POLL_SECONDS = 2.0
# How long the phase waits for one session's listing, for one session's contracts, for the underlying's chunk (the lake
# gives up on it at 630 seconds), and for a question of the catalog.
_LISTING_TIMEOUT_SECONDS = 600.0
_CONTRACTS_TIMEOUT_SECONDS = 1800.0
_UNDERLYING_TIMEOUT_SECONDS = 700.0
_CATALOG_QUESTION_TIMEOUT_SECONDS = 120.0

# The two lines the job's log gets about a wait for another request: no date, no contract, no count that could go stale.
WAITING_FOR_ANOTHER_REQUEST = "Waiting for data another request is already fetching."
THE_OTHER_REQUEST_FINISHED = "The other request has finished; carrying on."

# The reasons this phase gives beside the lake's own. ``data_check.LAKE_REASON_WORDS`` has plain words for each.
UNDERLYING_NOT_FETCHED = "underlying_bars_not_materialized"
NOT_IN_THE_LAKE = "not_in_the_lake"
STILL_BEING_FETCHED = "another_request_still_fetching"
TOOK_TOO_LONG = "took_too_long"


class DownloadFailed(Exception):
    """The lake could not bring in something a session needs. Nothing after it was fetched.

    ``session`` is ``None`` for the underlying's bars, which are asked for in chunks. ``reason`` is the lake's own
    reason code or one of this module's; ``detail`` is the lake's text, kept for a developer and never put in a
    sentence an owner reads (it can hold a date, a vendor's JSON or a file path).
    """

    def __init__(self, *, session: date | None, what: str, reason: str, detail: str) -> None:
        self.session = session
        self.what = what
        self.reason = reason
        self.detail = detail
        super().__init__(f"{what} could not be fetched ({reason})")


class DownloadStopped(Exception):
    """The phase was asked to stop. ``sessions_done`` sessions had been fully brought in."""

    def __init__(self, sessions_done: int) -> None:
        self.sessions_done = sessions_done
        super().__init__(f"the download was stopped after {sessions_done} sessions")


@dataclass(frozen=True)
class DownloadReport:
    """What the phase did, for the log."""

    sessions_prepared: int
    listings_fetched: int
    listings_reused: int
    option_days_fetched: int
    option_days_reused: int


@dataclass
class _Counts:
    sessions: int = 0
    listings_fetched: int = 0
    listings_reused: int = 0
    days_fetched: int = 0
    days_reused: int = 0

    def report(self) -> DownloadReport:
        return DownloadReport(
            self.sessions, self.listings_fetched, self.listings_reused, self.days_fetched, self.days_reused
        )


def _ignore_progress(_done: int, _total: int) -> None:
    return None


def _ignore_log(_message: str) -> None:
    return None


def _wait_for(future: Future[Any], seconds: float) -> None:
    wait_for_futures([future], timeout=seconds)


@dataclass(frozen=True)
class Pace:
    """How the phase reads the time and waits for a future, so a test can hand it a clock it controls.

    ``wait_for(future, seconds)`` returns once the future is done or the seconds have passed.
    """

    now: Callable[[], float] = time.monotonic
    wait_for: Callable[[Future[Any], float], None] = _wait_for


REAL_PACE = Pace()


def ensure_run_data(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    *,
    now_ms: int,
    on_progress: Callable[[int, int], None] = _ignore_progress,
    should_stop: Callable[[], bool],
    on_log: Callable[[str], None] = _ignore_log,
    data: LakeRunData | None = None,
    pace: Pace = REAL_PACE,
) -> DownloadReport:
    """Bring into the lake what a run of ``setup`` over ``sessions`` (in date order) can read.

    ``on_progress(done, total)`` is called after each session, in sessions. ``should_stop()`` is asked before each
    session and at least every ``STOP_POLL_SECONDS`` while the phase waits; a yes raises ``DownloadStopped``.
    ``now_ms`` is the instant the lake's rules judge a session by. Raises ``DownloadFailed`` for a typed failure of
    the lake or a wait that ran out; a catalog outage, a file that stays unreadable after it was fetched, and anything
    unexpected propagate as they are. Blocking: call it from a worker thread.
    """
    return _Download(
        setup,
        now_ms=now_ms,
        reader=data or LakeRunData(setup.underlying),
        on_log=on_log,
        should_stop=should_stop,
        pace=pace,
    ).run(sessions, on_progress)


class _Batch:
    """What a piece of work on the background loop shares with the thread waiting for it.

    A stop the thread sets and the coroutine reads, how many of its captures are waiting for another request, and
    whether any capture of it has failed. The count and the note are changed on the loop and read on the thread, so
    they are held under a lock.
    """

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._waiting = 0
        self._failed = False

    def begins_to_wait(self) -> None:
        with self._lock:
            self._waiting += 1

    def ends_the_wait(self) -> None:
        with self._lock:
            self._waiting -= 1

    def is_waiting(self) -> bool:
        with self._lock:
            return self._waiting > 0

    def fails(self) -> None:
        """A capture of this batch ended in a failure, or in an error."""
        with self._lock:
            self._failed = True

    def is_carrying_on(self) -> bool:
        """Whether a wait that has ended did so because the other request finished and the batch goes on."""
        with self._lock:
            return not self._failed and not self.stop.is_set()


class _Download:
    """One run of the phase: what it reads with, what it counts, and how it waits."""

    def __init__(
        self,
        setup: OptionsBacktestSetup,
        *,
        now_ms: int,
        reader: LakeRunData,
        on_log: Callable[[str], None],
        should_stop: Callable[[], bool],
        pace: Pace,
    ) -> None:
        self._setup = setup
        self._now_ms = now_ms
        self._reader = reader
        self._log = on_log
        self._should_stop = should_stop
        self._pace = pace
        self._counts = _Counts()
        self._done = 0

    def run(self, sessions: Sequence[date], on_progress: Callable[[int, int], None]) -> DownloadReport:
        needing = [day for day in sessions if self._needs_data(day)]
        chunks = {
            needing[first]: needing[first : first + UNDERLYING_CHUNK_SESSIONS]
            for first in range(0, len(needing), UNDERLYING_CHUNK_SESSIONS)
        }
        needing_data = set(needing)
        for done, day in enumerate(sessions):
            self._done = done
            if self._should_stop():
                raise DownloadStopped(done)
            if day in chunks:
                self._bring_in_the_underlying(chunks[day], already=needing.index(day), of=len(needing))
            if day in needing_data:
                self._bring_in_the_session(day, position=(done + 1, len(sessions)))
                self._counts.sessions += 1
            on_progress(done + 1, len(sessions))
        return self._counts.report()

    def _needs_data(self, day: date) -> bool:
        """A session whose window does not fit reads nothing, so it fetches nothing."""
        window = session_window_for_date(day)
        return self._setup.window_fits(session_open_ms=window.open_ms_utc, session_close_ms=window.close_ms_utc)

    # ── the underlying ───────────────────────────────────────────────────

    def _bring_in_the_underlying(self, chunk: Sequence[date], *, already: int, of: int) -> None:
        underlying = self._setup.underlying
        what = f"the minute bars of {underlying}"
        for day in chunk:
            refusal = session_refusal(underlying, day, now_ms=self._now_ms)
            if refusal is not None:
                raise DownloadFailed(session=day, what=what, reason=refusal.reason, detail=refusal.detail)
        self._log(f"Fetching {underlying} minute bars for sessions {already + 1} to {already + len(chunk)} of {of}.")
        for stretch in _stretches(chunk):
            future = _in_a_thread(
                partial(
                    materialize_engine_run,
                    symbol=underlying,
                    start=stretch[0],
                    end=stretch[-1],
                    resolution="minute",
                    price_adjustment_mode="raw",
                ),
                name="options-backtest-underlying",
            )
            try:
                self._wait(future, batch=None, give_up_after=_UNDERLYING_TIMEOUT_SECONDS, session=None, what=what)
            except LakeMaterializationError as exc:
                reason = self._reason_the_lake_recorded_for_the_underlying(stretch) or UNDERLYING_NOT_FETCHED
                raise DownloadFailed(session=None, what=what, reason=reason, detail=str(exc)) from exc
            except CallerStoppedWaitingError as exc:
                raise DownloadFailed(session=None, what=what, reason=TOOK_TOO_LONG, detail=str(exc)) from exc
        self._log(f"{underlying} minute bars are in the lake for {already + len(chunk)} of {of} sessions.")

    def _reason_the_lake_recorded_for_the_underlying(self, chunk: Sequence[date]) -> str | None:
        """The reason code the lake wrote in the catalog for the first day of ``chunk`` whose stock fetch failed.

        The lake's error for a chunk says only that it could not serve it, in a sentence; the reason for each day it
        could not is in the catalog's row for that day (``provider_no_data``, ``provider_api_error``, ...), and that is
        asked, never read out of the sentence. ``None`` when no day of the chunk has a failed row (the error came from
        elsewhere: a file missing on disk, a calendar the lake cannot reach) or when the catalog cannot be asked now.
        """
        reader = LakeCheckData(self._setup.underlying)
        for day in chunk:
            try:
                recorded = reader.recorded_failure(DataThing(DataKind.UNDERLYING_BARS), day)
            except (catalog_client.CatalogUnavailableError, CallerStoppedWaitingError) as exc:
                logger.warning(
                    "the lake's reason for the underlying's failed fetch could not be read; the failure goes without it",
                    extra={"action": "options_backtest_underlying_reason_unread", "error_type": type(exc).__name__},
                )
                return None
            if recorded is not None:
                return recorded.reason
        return None

    # ── one session ──────────────────────────────────────────────────────

    def _bring_in_the_session(self, day: date, *, position: tuple[int, int]) -> None:
        underlying = self._setup.underlying
        listing_what = f"the {underlying} option listing"
        listed = self._on_the_loop(
            lambda batch: _capture_listing(underlying, day, self._now_ms, batch),
            give_up_after=_LISTING_TIMEOUT_SECONDS,
            session=day,
            what=listing_what,
        )
        if listed is None:
            raise DownloadStopped(self._done)
        self._require(listed, session=day, what=listing_what)
        if listed.reused:
            self._counts.listings_reused += 1
        else:
            self._counts.listings_fetched += 1

        bars = self._reader.underlying_day(day)
        if bars is None:
            raise DownloadFailed(
                session=day,
                what=f"the minute bars of {underlying}",
                reason=NOT_IN_THE_LAKE,
                detail="they are still not in the lake after they were fetched",
            )
        stored = self._reader.stored_listing(day)
        if stored is None:
            raise DownloadFailed(
                session=day,
                what=listing_what,
                reason=NOT_IN_THE_LAKE,
                detail="it is still not in the lake after it was fetched",
            )
        window = session_window_for_date(day)
        needs = session_needs(
            self._setup,
            trading_date=day,
            session_open_ms=window.open_ms_utc,
            session_close_ms=window.close_ms_utc,
            underlying=tuple(bars.value),
            listing=captured_listing_of(stored).value,
        )
        contracts = sorted(needs.contracts, key=lambda each: each.occ_symbol)
        if not contracts:
            return
        to_capture = self._not_stored(contracts, day)
        self._counts.days_reused += len(contracts) - len(to_capture)
        self._log(
            f"Session {position[0]} of {position[1]}: bringing in {len(to_capture)} of {len(contracts)} contract days."
        )
        if to_capture:
            self._capture_contracts(to_capture, day, stored)

    def _not_stored(self, contracts: Sequence[OptionContract], day: date) -> list[OptionContract]:
        """The contracts whose day is not stored: the lake is asked once, and a captured day must also read."""
        symbols = [contract.occ_symbol for contract in contracts]
        captured = self._on_the_loop(
            lambda _batch: _which_are_captured(symbols, day),
            give_up_after=_CATALOG_QUESTION_TIMEOUT_SECONDS,
            session=day,
            what="the list of stored contract days",
        )
        return [
            contract for contract in contracts if contract.occ_symbol not in captured or not self._reads(contract, day)
        ]

    def _reads(self, contract: OptionContract, day: date) -> bool:
        """Whether a day the catalog names as captured still reads; one that does not is captured again."""
        try:
            return self._reader.option_day(contract, day) is not None
        except STORED_FILE_ERRORS:
            return False

    def _capture_contracts(self, contracts: Sequence[OptionContract], day: date, stored: OptionListing) -> None:
        underlying = self._setup.underlying
        outcomes = self._on_the_loop(
            lambda batch: _capture_contract_days(contracts, day, underlying, stored, self._now_ms, batch),
            give_up_after=_CONTRACTS_TIMEOUT_SECONDS,
            session=day,
            what=f"the bars of {len(contracts)} contracts",
        )
        for outcome in outcomes:
            if outcome.error is not None:
                raise outcome.error
        for outcome in outcomes:
            if outcome.result is None:
                continue
            self._require(outcome.result, session=day, what=f"the bars of {outcome.contract.occ_symbol}")
            if outcome.result.reused:
                self._counts.days_reused += 1
            else:
                self._counts.days_fetched += 1

    @staticmethod
    def _require(result: OptionCaptureResult, *, session: date, what: str) -> None:
        failure = result.failure
        if failure is None:
            return
        # Another request still holding the row when the wait for it ran out is the only way the lake's lease
        # timeout reaches here.
        reason = STILL_BEING_FETCHED if failure.reason == "lease_timeout" else failure.reason
        raise DownloadFailed(session=session, what=what, reason=reason, detail=failure.detail)

    # ── waiting ──────────────────────────────────────────────────────────

    def _on_the_loop[T](
        self,
        make: Callable[[_Batch], Coroutine[Any, Any, T]],
        *,
        give_up_after: float,
        session: date | None,
        what: str,
    ) -> T:
        """Run a coroutine on the process's background loop and wait for it, as :meth:`_wait` does.

        ``make`` is handed the batch: the event the coroutine reads to know it has been told to stop, and the count of
        its captures that are waiting for another request.
        """
        batch = _Batch()
        future = asyncio.run_coroutine_threadsafe(make(batch), background_loop())
        return self._wait(future, batch=batch, give_up_after=give_up_after, session=session, what=what)

    def _say_if_waiting_changed(self, batch: _Batch, said_waiting: bool) -> bool:
        """Write the line for a wait that has begun or ended since the last look; return whether one has been said
        and not yet closed.

        The count is of the captures that are waiting now, so a wait that began and ended between two looks is never
        announced after the fact, and the end of a wait is not announced as carrying on once a capture has failed or
        the batch has been told to stop.
        """
        waiting = batch.is_waiting()
        if waiting and not said_waiting:
            self._log(WAITING_FOR_ANOTHER_REQUEST)
            return True
        if said_waiting and not waiting:
            if batch.is_carrying_on():
                self._log(THE_OTHER_REQUEST_FINISHED)
            return False
        return said_waiting

    def _wait[T](
        self,
        future: Future[T],
        *,
        batch: _Batch | None,
        give_up_after: float,
        session: date | None,
        what: str,
    ) -> T:
        """The result of ``future``, waiting in slices of ``STOP_POLL_SECONDS`` and asking after each whether to stop.

        A yes, or ``give_up_after`` seconds gone, sets the batch's stop (the coroutine starts no more requests and ends
        once those in flight have), and the wait goes on for ``STOP_GRACE_SECONDS`` more, after which the future is
        cancelled as a last resort. A work with no batch (the underlying's chunk) is abandoned at once. A yes then
        raises ``DownloadStopped``; a wait that ran out raises ``DownloadFailed``. Whatever the future raised on its
        own propagates. After each slice the log is told if the batch has begun to wait for another request or has
        stopped waiting. If the thread leaves the wait by any other exception (a log hook or a stop poll that fails),
        the batch is told to stop first, so the captures begin no request after it.
        """
        deadline = self._pace.now() + give_up_after
        grace = STOP_GRACE_SECONDS if batch is not None else 0.0
        told_at: float | None = None
        stopped = False
        said_waiting = False
        try:
            while True:
                self._pace.wait_for(future, STOP_POLL_SECONDS)
                if batch is not None:
                    said_waiting = self._say_if_waiting_changed(batch, said_waiting)
                if future.done():
                    break
                now = self._pace.now()
                if told_at is None:
                    stopped = self._should_stop()
                    if not stopped and now < deadline:
                        continue
                    told_at = now
                    if batch is not None:
                        batch.stop.set()
                if now - told_at >= grace:
                    break
            if told_at is None:
                return future.result()
            if not future.done():
                future.cancel()
            if stopped:
                raise DownloadStopped(self._done)
            raise DownloadFailed(
                session=session, what=what, reason=TOOK_TOO_LONG, detail=f"not finished after {give_up_after} seconds"
            )
        except BaseException:
            # Nothing waits for this batch any more, so nothing of it may start a request: the stop is a flag the
            # coroutines read, and setting it again after the paths above set it is harmless.
            if batch is not None:
                batch.stop.set()
            raise


def _stretches(needed: Sequence[date]) -> list[list[date]]:
    """``needed`` (sessions in date order) cut at every session of the calendar that falls between two of them and is
    not among them.

    The stock pipeline is asked for the bars of every session from a first date to a last, so a request for a stretch
    that holds a session the run does not need would fetch that session too, and a failure on it would fail a run that
    could have run. Each stretch is sessions that follow one another in the calendar, all of them needed.
    """
    wanted = set(needed)
    stretches: list[list[date]] = []
    current: list[date] = []
    for session in expected_sessions(needed[0], needed[-1]):
        if session in wanted:
            current.append(session)
        elif current:
            stretches.append(current)
            current = []
    if current:
        stretches.append(current)
    return stretches


def _in_a_thread[T](work: Callable[[], T], *, name: str) -> Future[T]:
    """``work`` on a thread of its own, as a future the caller can wait on in slices."""
    future: Future[T] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(work())
        except BaseException as exc:  # carried to the thread waiting on the future
            future.set_exception(exc)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future


# ── What runs on the background loop ─────────────────────────────────────


@dataclass(frozen=True)
class _ContractOutcome:
    """One contract's capture: the lake's answer, or what it raised; neither when the batch had stopped first."""

    contract: OptionContract
    result: OptionCaptureResult | None = None
    error: Exception | None = None


async def _capture_listing(
    underlying: str, day: date, now_ms: int, batch: _Batch
) -> OptionCaptureResult | None:
    """The listing's capture; ``None`` when the phase was told to stop before it was asked."""
    await catalog_client.init_pool()
    return await _ask_until_nobody_else_is_fetching(
        lambda: ensure_option_listing(underlying, day, now_ms=now_ms), batch=batch
    )


async def _which_are_captured(symbols: Sequence[str], day: date) -> frozenset[str]:
    await catalog_client.init_pool()
    return await captured_option_days(symbols, day)


async def _capture_contract_days(
    contracts: Sequence[OptionContract],
    day: date,
    underlying: str,
    stored: OptionListing,
    now_ms: int,
    batch: _Batch,
) -> list[_ContractOutcome]:
    """Capture each contract's day through the process's request gate; none starts once one failed or stop is set."""
    await catalog_client.init_pool()
    broken = False

    async def capture(contract: OptionContract) -> _ContractOutcome:
        nonlocal broken
        try:
            result = await _ask_until_nobody_else_is_fetching(
                lambda: ensure_option_day_bars(contract, day, listed_under=underlying, listing=stored, now_ms=now_ms),
                batch=batch,
                halt=lambda: broken,
            )
        except Exception as exc:  # carried to the thread that raises it; the others stop starting
            broken = True
            return _ContractOutcome(contract, error=exc)
        broken = broken or (result is not None and result.failure is not None)
        return _ContractOutcome(contract, result)

    return list(await asyncio.gather(*(capture(contract) for contract in contracts)))


async def _ask_until_nobody_else_is_fetching(
    ask: Callable[[], Coroutine[Any, Any, OptionCaptureResult]],
    *,
    batch: _Batch,
    halt: Callable[[], bool] = lambda: False,
) -> OptionCaptureResult | None:
    """``ask()`` under the process's request gate; ``None`` when it was not asked because the phase had to stop.

    The lake answers ``lease_timeout`` while another worker holds the row it wants. That is not a failure yet: the
    gate is let go, the capture waits ``OTHER_REQUEST_POLL_SECONDS`` and asks again, until
    ``OTHER_REQUEST_WAIT_SECONDS`` have passed, when the last ``lease_timeout`` is returned. The batch counts the
    capture as waiting from the first ``lease_timeout`` until it leaves the wait, whichever way it leaves, and notes
    when a capture's answer is a failure, so the job's thread can tell a wait that ended well from one that did not.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OTHER_REQUEST_WAIT_SECONDS
    waiting = False
    try:
        while True:
            if batch.stop.is_set() or halt():
                return None
            async with vendor_request_gate():
                if batch.stop.is_set() or halt():
                    return None
                result = await ask()
            failure = result.failure
            if failure is None:
                return result
            if failure.reason != "lease_timeout" or loop.time() >= deadline:
                batch.fails()
                return result
            if not waiting:
                batch.begins_to_wait()
                waiting = True
            await asyncio.sleep(OTHER_REQUEST_POLL_SECONDS)
    except Exception:
        # Noted before the capture leaves the wait, so the thread never sees a wait that ended with nothing wrong.
        batch.fails()
        raise
    finally:
        if waiting:
            batch.ends_the_wait()
