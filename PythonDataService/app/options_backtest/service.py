"""Run an options backtest: check what is stored, fetch what is missing, replay, and save (#3030).

Formula: none. The order of the stock engine's service, for an options run:
    1. the request is validated (a setup, a first and a last date); a client mistake is a refusal, never a job
    2. check data; a session that cannot be tested fails the run at once, with the list and the reasons
    3. the download phase brings in what is missing, through the lake
    4. the engine's run gate is taken, in the worker thread, and the sessions are replayed
    5. the run is saved, whole, in one transaction
Reference: ``app/services/engine_backtest_service.py`` for the order; ``run_sessions`` for what is replayed.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_service.py.

A run ends in one of three ways and saves nothing else: complete (saved whole), failed (a code and a plain reason; the
request is never shortened to the sessions that can be tested), or cancelled. A stop is asked for at the boundary of
every phase, after every session of the check, the download and the replay, at least every few seconds while the
download waits on the vendor, and while the run waits for the gate. Each of those asks renews the job's lease, so no
stretch of a healthy run is long enough for it to read as interrupted, and Cancel is heard within one of them. A stop
that comes after the save has begun is too late: the run is complete.

The service knows nothing of Redis or of a database: it is handed the callbacks of a job (``RunHooks``) and a store
(``RunStore``). The job route wires them. ``execute_run`` ends by raising ``RunCancelled`` for a cancelled run and
``RunFailedError`` for a failed one, after the store has recorded it, so the job can end the same way; a run that
completes is returned. Recording the end is tried twice; if the store fails both times the run's id is logged and its
row stays ``running``, which reads as ``interrupted`` once the job is gone.

What a failure says. A reason is composed here from facts: what could not be read or fetched, the lake's reason code
and a short plain phrase for the code. It never carries the text the lake, the vendor or another module wrote, which
can name a date, hold a vendor's JSON or a file path; a session is named by its open in ms in the failure's items, and
no sentence names a date. For the same reason a known failure is raised without the exception that caused it chained
on, so the job's own log of the failure holds none of that text either; an unexpected error keeps its chain.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_listing import OptionListingFileError
from app.engine.run_gate import one_backtest_in_flight
from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options_backtest.data_check import (
    STORED_FILE_ERRORS,
    CheckStopped,
    DataCheck,
    SessionVerdict,
    check_data,
    lake_reason_words,
)
from app.options_backtest.fees import FeeRateNotPublished
from app.options_backtest.fetching import DownloadFailed, DownloadStopped, ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.lake_data import BARS_CANNOT_BE_PLACED, LakeRunData, UnderlyingDayUnreadable
from app.options_backtest.limits import MAX_RANGE_SESSIONS
from app.options_backtest.replay import SoldLegRulesNotReplayed, refuse_sold_leg_rules
from app.options_backtest.run import RunResult, RunStopped, SessionDataMissing, run_sessions
from app.options_backtest.saved_runs import FailureItem, RunFailure
from app.options_backtest.session_market import MalformedBars
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.grid_search.service import window_dates
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms, require_schedulable_end
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# The phases a job reports, in the order of the work. ``app/jobs/phases.py`` gives each its label.
PHASE_CHECKING_DATA = "checking_data"
PHASE_DOWNLOADING_DATA = "downloading_data"
PHASE_WAITING_FOR_ENGINE = "waiting_for_engine"
PHASE_REPLAYING = "replaying"
PHASE_SAVING = "saving"

# The codes a failed run carries, for a page to branch on.
SESSIONS_CANNOT_BE_TESTED = "SESSIONS_CANNOT_BE_TESTED"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
DATA_MISSING = "DATA_MISSING"
DATA_UNREADABLE = "DATA_UNREADABLE"
FEE_RATE_NOT_PUBLISHED = "FEE_RATE_NOT_PUBLISHED"
SOLD_LEG_RULES_NOT_REPLAYED_YET = "SOLD_LEG_RULES_NOT_REPLAYED_YET"
DATA_CATALOG_UNAVAILABLE = "DATA_CATALOG_UNAVAILABLE"
SAVE_TIMED_OUT = "SAVE_TIMED_OUT"
INTERNAL_ERROR = "INTERNAL_ERROR"

# The code of the item a DATA_UNREADABLE failure carries for the session it was on.
STORED_DATA_UNREADABLE = "stored_data_unreadable"


class RunRefused(ValueError):
    """The request is a client mistake. ``code`` is for a page to branch on, the message for the owner."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class RunCancelled(Exception):
    """The run was cancelled; the store has recorded it."""


class RunFailedError(Exception):
    """The run failed; the store has recorded it. ``failure`` is what was recorded."""

    def __init__(self, failure: RunFailure) -> None:
        self.failure = failure
        super().__init__(failure.reason)


class SaveTimedOut(Exception):
    """Saving a replayed run took longer than the run waits. The save may still commit."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__(f"saving run {run_id} took longer than the run waits")


class StoredDayUnreadable(Exception):
    """A stored day a session needs cannot be used. ``session`` is the session the phase was on, if it is known.

    ``cause`` is the error behind it: a file that cannot be read, or bars that break the rules of a bar.
    """

    def __init__(self, session: date | None, cause: Exception) -> None:
        self.session = session
        self.cause = cause
        super().__init__("a stored day a session needs cannot be used")


class SessionsCannotBeTested(Exception):
    """Check data found sessions that cannot be tested, so the run goes no further."""

    def __init__(self, check: DataCheck) -> None:
        self.check = check
        super().__init__(f"{check.count(SessionVerdict.CANNOT_BE_TESTED)} sessions cannot be tested")


class RunStore(Protocol):
    """Where a run's end is recorded. Blocking, for a worker thread."""

    def complete(self, run_id: int, result: RunResult, *, finished_at_ms: int) -> None: ...

    def fail(self, run_id: int, failure: RunFailure, *, finished_at_ms: int) -> None: ...

    def cancel(self, run_id: int, *, finished_at_ms: int) -> None: ...


@dataclass(frozen=True)
class RunHooks:
    """What a job lets the service say and ask: its phase, its progress and log, and whether to stop."""

    on_phase: Callable[[str], None]
    on_progress: Callable[[int, int], None]
    on_log: Callable[[str], None]
    should_stop: Callable[[], bool]


def _is_new_york_midnight(ms: int) -> bool:
    """Whether ``ms`` is the instant a New York day begins. A day is 23 or 25 hours long where the clocks change, so
    this is asked of the zone and never of a multiple of 24 hours."""
    return et_midnight_ms(et_date_at_ms(ms)) == ms


def sessions_of_range(start_ms: int, end_ms: int) -> tuple[date, ...]:
    """The NYSE sessions of the half-open window ``[start_ms, end_ms)``, as Grid Search reads a window.

    ``start_ms`` is New York midnight of the first date and ``end_ms`` is New York midnight of the day after the last,
    as the wire documents them; any other instant is a ``RunRefused``, since reading it as the date it falls on would
    run a whole session that began before it. The first date is then the New York date of ``start_ms`` and the last is
    the New York date of the last millisecond before ``end_ms``; the sessions between are the canonical calendar's. A
    window with none, one that ends past the calendar's reach, or one of more than ``MAX_RANGE_SESSIONS`` sessions is
    a ``RunRefused``.
    """
    if not _is_new_york_midnight(start_ms):
        raise RunRefused(
            "OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT",
            "The range must start at midnight in New York. Send the start of its first day as start_ms.",
        )
    if not _is_new_york_midnight(end_ms):
        raise RunRefused(
            "OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT",
            "The range must end at midnight in New York. Send the start of the day after its last day as end_ms.",
        )
    if start_ms >= end_ms:
        raise RunRefused("OPTIONS_BACKTEST_RANGE_EMPTY", "The range must end after it starts.")
    try:
        require_schedulable_end(end_ms)
    except ValueError as exc:
        raise RunRefused("OPTIONS_BACKTEST_RANGE_TOO_FAR", str(exc)) from exc
    first, last = window_dates(start_ms, end_ms)
    sessions = tuple(expected_sessions(first, last))
    if not sessions:
        raise RunRefused("OPTIONS_BACKTEST_RANGE_HAS_NO_SESSION", "The range holds no trading day.")
    if len(sessions) > MAX_RANGE_SESSIONS:
        raise RunRefused(
            "OPTIONS_BACKTEST_RANGE_TOO_LONG",
            f"The range holds {len(sessions)} trading days; a run takes at most {MAX_RANGE_SESSIONS}.",
        )
    return sessions


def execute_run(
    *,
    run_id: int,
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    store: RunStore,
    hooks: RunHooks,
    clock: Callable[[], int] = now_ms_utc,
) -> RunResult:
    """Check, fetch, replay and save one run. Blocking: call it from a worker thread.

    Returns the result of a run that was saved. Raises ``RunCancelled`` or ``RunFailedError`` once the store has
    recorded the run as cancelled or failed; if the store cannot record it, in two tries, the run's id is logged and
    its row stays ``running``, which reads as ``interrupted`` once the job is gone. Either way the job ends as the run
    did, with the run's own words and not the store's.
    """
    try:
        result = _work(setup, sessions, hooks, clock)
        _stop_if_asked(hooks)
        hooks.on_phase(PHASE_SAVING)
        _save(store, run_id, result, clock)
        return result
    except (RunCancelled, RunStopped, DownloadStopped, CheckStopped):
        finished_at_ms = clock()
        _record_the_end(lambda: store.cancel(run_id, finished_at_ms=finished_at_ms), run_id=run_id, ended="cancelled")
        raise RunCancelled(f"run {run_id} was cancelled") from None
    except Exception as exc:
        failure = failure_of(exc)
        finished_at_ms = clock()
        _record_the_end(
            lambda: store.fail(run_id, failure, finished_at_ms=finished_at_ms), run_id=run_id, ended="failed"
        )
        # A failure whose words were composed here has said what it has to; the text of the exception behind it may
        # name a date or hold the vendor's JSON, and the job logs everything chained to what it is given.
        raise RunFailedError(failure) from (exc if failure.code == INTERNAL_ERROR else None)


def _save(store: RunStore, run_id: int, result: RunResult, clock: Callable[[], int]) -> None:
    try:
        store.complete(run_id, result, finished_at_ms=clock())
    except CallerStoppedWaitingError as exc:
        raise SaveTimedOut(run_id) from exc


def _record_the_end(record: Callable[[], None], *, run_id: int, ended: str) -> None:
    """Write how the run ended, trying twice; log the run's id if both tries fail, and let the row stay ``running``."""
    for attempt in (1, 2):
        try:
            record()
            return
        except Exception:
            logger.warning(
                "recording the run as %s failed (try %d of 2)",
                ended,
                attempt,
                extra={"action": "options_backtest_run_end_not_recorded", "run_id": run_id, "ended": ended},
                exc_info=True,
            )
    logger.error(
        "the run could not be recorded as %s; its row stays running and reads as interrupted once the job is gone",
        ended,
        extra={"action": "options_backtest_run_end_lost", "run_id": run_id, "ended": ended},
    )


def _work(
    setup: OptionsBacktestSetup, sessions: Sequence[date], hooks: RunHooks, clock: Callable[[], int]
) -> RunResult:
    now_ms = clock()
    _stop_if_asked(hooks)
    # Until a stop loss and a re-entry rule on a sold leg are replayed (#3107) such a setup is refused here, before
    # the check reads or fetches anything.
    refuse_sold_leg_rules(setup)
    hooks.on_phase(PHASE_CHECKING_DATA)
    check = check_data(
        setup,
        sessions,
        now_ms=now_ms,
        data=LakeCheckData(setup.underlying),
        on_session=hooks.on_progress,
        should_stop=hooks.should_stop,
    )
    if check.count(SessionVerdict.CANNOT_BE_TESTED):
        raise SessionsCannotBeTested(check)

    _stop_if_asked(hooks)
    hooks.on_phase(PHASE_DOWNLOADING_DATA)
    report = _naming_the_session(
        sessions,
        hooks,
        lambda progress: ensure_run_data(
            setup,
            sessions,
            now_ms=now_ms,
            on_progress=progress,
            should_stop=hooks.should_stop,
            on_log=hooks.on_log,
        ),
    )
    hooks.on_log(
        f"Brought in {report.listings_fetched} listings and {report.option_days_fetched} contract days "
        f"({report.listings_reused} and {report.option_days_reused} were already stored)."
    )

    _stop_if_asked(hooks)
    return _replay(setup, sessions, hooks)


def _replay(setup: OptionsBacktestSetup, sessions: Sequence[date], hooks: RunHooks) -> RunResult:
    """The replay under the engine's run gate, taken here in the worker thread and held for the replay alone."""

    def while_waiting() -> None:
        _stop_if_asked(hooks)

    with one_backtest_in_flight(on_wait=lambda: hooks.on_phase(PHASE_WAITING_FOR_ENGINE), while_waiting=while_waiting):
        hooks.on_phase(PHASE_REPLAYING)
        return _naming_the_session(
            sessions,
            hooks,
            lambda progress: run_sessions(
                setup,
                sessions,
                LakeRunData(setup.underlying),
                on_session=lambda done, total, _day: progress(done, total),
                should_stop=hooks.should_stop,
            ),
        )


def _naming_the_session[T](
    sessions: Sequence[date], hooks: RunHooks, phase: Callable[[Callable[[int, int], None]], T]
) -> T:
    """Run a phase that reports each session done; if it meets a stored day it cannot use, say which session it was on.

    A phase reports a session after it has finished it, so the one it was on is the one after the last reported.
    """
    finished = 0

    def progress(done: int, total: int) -> None:
        nonlocal finished
        finished = done
        hooks.on_progress(done, total)

    try:
        return phase(progress)
    except (*STORED_FILE_ERRORS, MalformedBars) as exc:
        raise StoredDayUnreadable(sessions[finished] if finished < len(sessions) else None, exc) from exc


def _stop_if_asked(hooks: RunHooks) -> None:
    if hooks.should_stop():
        raise RunCancelled("stop requested")


# ── Failures an owner can read ───────────────────────────────────────────


def failure_of(exc: Exception) -> RunFailure:
    """What a failed run records: a code, a plain reason and the list of what failed.

    A session is named by its open in the list's items and not in the reason, which says what went wrong in words.
    Nothing the exception says is copied into those words (see the module docstring); an exception this table does
    not know is an unexpected error, named by its type and logged with its traceback.
    """
    for kind, build in _BUILDERS:
        if isinstance(exc, kind):
            return build(exc)
    logger.exception("an options backtest run failed unexpectedly", extra={"action": "options_backtest_run_failed"})
    return RunFailure(
        INTERNAL_ERROR, f"The run stopped because of an unexpected error ({type(exc).__name__}). Nothing was saved."
    )


def _opened(day: date | None) -> int | None:
    return None if day is None else session_window_for_date(day).open_ms_utc


def _download_failed(exc: DownloadFailed) -> RunFailure:
    why = f"{lake_reason_words(exc.reason)} ({exc.reason})"
    return RunFailure(
        DOWNLOAD_FAILED,
        f"The data could not be fetched, so nothing was replayed. The lake could not bring in {exc.what}: {why}.",
        (FailureItem(_opened(exc.session), exc.reason, f"{exc.what}: {why}"),),
    )


def _session_data_missing(exc: SessionDataMissing) -> RunFailure:
    missing = _what_is_missing(exc)
    return RunFailure(
        DATA_MISSING,
        f"A session cannot be replayed: {missing} not in the lake. Nothing was saved.",
        (FailureItem(_opened(exc.trading_date), "not_captured", f"{missing} not captured"),),
    )


def _fee_rate_not_published(exc: FeeRateNotPublished) -> RunFailure:
    return RunFailure(
        FEE_RATE_NOT_PUBLISHED,
        f"Estimated fees cannot be priced for a session in the range: the fee model has no published rate for "
        f"{', '.join(exc.components)}. Charge a fee per contract or none, or start the run later.",
        (
            FailureItem(
                _opened(exc.trading_date), "no_published_fee_rates", f"no published rate for {', '.join(exc.components)}"
            ),
        ),
    )


def _sold_leg_rules_not_replayed(exc: SoldLegRulesNotReplayed) -> RunFailure:
    sold, ruled = ", ".join(exc.sold), ", ".join(exc.ruled)
    return RunFailure(
        SOLD_LEG_RULES_NOT_REPLAYED_YET,
        f"This setup sells ({sold}) and a leg carries a stop loss or a re-entry rule ({ruled}), and a run does not "
        "replay those on a setup that sells yet, so nothing was fetched or run. Take the stop losses and the "
        "re-entry rules off to run it: a setup that sells and has none runs, and this one can be saved as it is.",
        tuple(
            FailureItem(None, "sold_leg_rule", f"leg {leg_id} carries a stop loss or a re-entry rule")
            for leg_id in exc.ruled
        ),
    )


_BARS_BREAK_THE_RULES = (
    "Stored bars the run needs break the rules of a bar (a minute twice or out of order, a bar not on a whole minute, "
    "a price of zero or less, or bars of another day), so it cannot use them."
)


def _bars_break_the_rules(_exc: MalformedBars) -> RunFailure:
    return RunFailure(DATA_UNREADABLE, f"{_BARS_BREAK_THE_RULES} Nothing was saved.")


def _stored_file_cannot_be_read(exc: Exception) -> RunFailure:
    return RunFailure(
        DATA_UNREADABLE, f"A stored file the run needs cannot be read ({type(exc).__name__}). Nothing was saved."
    )


def _stored_day_cannot_be_used(exc: StoredDayUnreadable) -> RunFailure:
    """The failure of the error behind it, with the session it was on named by its open."""
    failure = failure_of(exc.cause)
    return RunFailure(
        failure.code, failure.reason, (FailureItem(_opened(exc.session), STORED_DATA_UNREADABLE, failure.reason),)
    )


def _catalog_unavailable(_exc: CatalogUnavailableError) -> RunFailure:
    return RunFailure(
        DATA_CATALOG_UNAVAILABLE, "The data catalog could not be reached, so the run stopped. Nothing was saved."
    )


def _lake_took_too_long(_exc: CallerStoppedWaitingError) -> RunFailure:
    return RunFailure(
        DOWNLOAD_FAILED,
        "The lake took longer to answer than the run waits. What it fetched is kept; run again to go on.",
    )


def _save_timed_out(_exc: SaveTimedOut) -> RunFailure:
    return RunFailure(
        SAVE_TIMED_OUT,
        "The run was replayed, but saving it took longer than the run waits, so it may or may not have been saved. "
        "Look in the history before running it again.",
    )


# The first entry an exception is an instance of builds its failure. Each takes the exception as its own type.
_BUILDERS: tuple[tuple[type[Exception] | tuple[type[Exception], ...], Callable[[Any], RunFailure]], ...] = (
    (SessionsCannotBeTested, lambda exc: _cannot_be_tested(exc.check)),
    (SoldLegRulesNotReplayed, _sold_leg_rules_not_replayed),
    (DownloadFailed, _download_failed),
    (SessionDataMissing, _session_data_missing),
    (FeeRateNotPublished, _fee_rate_not_published),
    (StoredDayUnreadable, _stored_day_cannot_be_used),
    ((MalformedBars, *BARS_CANNOT_BE_PLACED), _bars_break_the_rules),
    (
        (LakeAdmissionError, OptionBarsFileError, OptionListingFileError, UnderlyingDayUnreadable),
        _stored_file_cannot_be_read,
    ),
    (CatalogUnavailableError, _catalog_unavailable),
    (SaveTimedOut, _save_timed_out),
    (CallerStoppedWaitingError, _lake_took_too_long),
)


def _what_is_missing(exc: SessionDataMissing) -> str:
    parts = []
    if exc.underlying:
        parts.append("the underlying's minute bars")
    if exc.listing:
        parts.append("the contract listing")
    if exc.contracts:
        parts.append(f"the bars of {', '.join(contract.occ_symbol for contract in exc.contracts)}")
    return " and ".join(parts) + (" are" if len(parts) > 1 or (exc.contracts and len(exc.contracts) > 1) else " is")


def _cannot_be_tested(check: DataCheck) -> RunFailure:
    refused = [session for session in check.sessions if session.verdict is SessionVerdict.CANNOT_BE_TESTED]
    items = tuple(
        FailureItem(session.session_open_ms, problem.code.value, problem.message)
        for session in refused
        for problem in session.problems
    )
    return RunFailure(
        SESSIONS_CANNOT_BE_TESTED,
        f"{len(refused)} of {len(check.sessions)} sessions cannot be tested, so nothing was fetched or run. "
        f"Each is listed with its reason; the first reads: {refused[0].problems[0].message}",
        items,
    )
