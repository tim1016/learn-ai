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
every phase, between sessions in the download and in the replay, and while the run waits for the gate. A stop that
comes after the save has begun is too late: the run is complete.

The service knows nothing of Redis or of a database: it is handed the callbacks of a job (``RunHooks``) and a store
(``RunStore``). The job route wires them. ``execute_run`` ends by raising ``RunCancelled`` for a cancelled run and
``RunFailedError`` for a failed one, after the store has recorded it, so the job can end the same way; a run that
completes is returned.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_listing import OptionListingFileError
from app.engine.run_gate import one_backtest_in_flight
from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options_backtest.data_check import SESSION_DATA_NOT_FINAL_WORDS, DataCheck, SessionVerdict, check_data
from app.options_backtest.fees import FeeRateNotPublished
from app.options_backtest.fetching import DownloadFailed, DownloadStopped, ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.lake_data import LakeRunData, UnderlyingDayUnreadable
from app.options_backtest.run import RunResult, RunStopped, SessionDataMissing, run_sessions
from app.options_backtest.saved_runs import FailureItem, RunFailure
from app.options_backtest.session_market import MalformedBars
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.grid_search.service import window_dates
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.session_anchors import require_schedulable_end
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# The most sessions one request may name: about five years of trading days. Nothing older than the options plan can be
# tested anyway; this only keeps a mistyped range from asking the calendar for a century.
MAX_RANGE_SESSIONS = 1_300

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
DATA_CATALOG_UNAVAILABLE = "DATA_CATALOG_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"


# What the lake says when it refuses to take a session at all. Its own sentences name the day and the rule it judged by;
# a run's words name neither (the session is in the failure's items, by its open, and the rule is the lake's to state).
_LAKE_REFUSAL_WORDS = {
    "session_not_over": SESSION_DATA_NOT_FINAL_WORDS,
    "not_a_session": "The exchange was closed on that day.",
    "outside_data_plan": "The data plan does not reach that session.",
}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _words_of(exc: DownloadFailed) -> str:
    """The lake's reason in words with no date in them: a refusal in this module's words, any other detail scrubbed."""
    return _LAKE_REFUSAL_WORDS.get(exc.reason) or _ISO_DATE.sub("that day", exc.detail)


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


def sessions_of_range(start_ms: int, end_ms: int) -> tuple[date, ...]:
    """The NYSE sessions of the half-open window ``[start_ms, end_ms)``, as Grid Search reads a window.

    The first date is the New York date of ``start_ms`` and the last is the New York date of the last millisecond
    before ``end_ms``; the sessions between are the canonical calendar's. A window with none, one that ends past the
    calendar's reach, or one of more than ``MAX_RANGE_SESSIONS`` sessions is a ``RunRefused``.
    """
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
    recorded the run as cancelled or failed.
    """
    try:
        result = _work(setup, sessions, hooks, clock)
        _stop_if_asked(hooks)
        hooks.on_phase(PHASE_SAVING)
        store.complete(run_id, result, finished_at_ms=clock())
        return result
    except (RunCancelled, RunStopped, DownloadStopped):
        store.cancel(run_id, finished_at_ms=clock())
        raise RunCancelled(f"run {run_id} was cancelled") from None
    except Exception as exc:
        failure = failure_of(exc)
        store.fail(run_id, failure, finished_at_ms=clock())
        raise RunFailedError(failure) from exc


def _work(
    setup: OptionsBacktestSetup, sessions: Sequence[date], hooks: RunHooks, clock: Callable[[], int]
) -> RunResult:
    now_ms = clock()
    _stop_if_asked(hooks)
    hooks.on_phase(PHASE_CHECKING_DATA)
    check = check_data(setup, sessions, now_ms=now_ms, data=LakeCheckData(setup.underlying))
    if check.count(SessionVerdict.CANNOT_BE_TESTED):
        raise SessionsCannotBeTested(check)

    _stop_if_asked(hooks)
    hooks.on_phase(PHASE_DOWNLOADING_DATA)
    report = ensure_run_data(
        setup,
        sessions,
        now_ms=now_ms,
        on_progress=hooks.on_progress,
        should_stop=hooks.should_stop,
        on_log=hooks.on_log,
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
        return run_sessions(
            setup,
            sessions,
            LakeRunData(setup.underlying),
            on_session=lambda done, total, _day: hooks.on_progress(done, total),
            should_stop=hooks.should_stop,
        )


def _stop_if_asked(hooks: RunHooks) -> None:
    if hooks.should_stop():
        raise RunCancelled("stop requested")


# ── Failures an owner can read ───────────────────────────────────────────


def failure_of(exc: Exception) -> RunFailure:
    """What a failed run records: a code, a plain reason and the list of what failed.

    A session is named by its open in the list's items and not in the reason, which says what went wrong in words.
    """
    if isinstance(exc, SessionsCannotBeTested):
        return _cannot_be_tested(exc.check)
    if isinstance(exc, DownloadFailed):
        opened = None if exc.session is None else session_window_for_date(exc.session).open_ms_utc
        return RunFailure(
            DOWNLOAD_FAILED,
            f"The data could not be fetched, so nothing was replayed: {exc.what} ({exc.reason}): {_words_of(exc)}",
            (FailureItem(opened, exc.reason, f"{exc.what}: {_words_of(exc)}"),),
        )
    if isinstance(exc, SessionDataMissing):
        opened = session_window_for_date(exc.trading_date).open_ms_utc
        missing = _what_is_missing(exc)
        return RunFailure(
            DATA_MISSING,
            f"A session cannot be replayed: {missing} not in the lake. Nothing was saved.",
            (FailureItem(opened, "not_captured", f"{missing} not captured"),),
        )
    if isinstance(exc, FeeRateNotPublished):
        opened = session_window_for_date(exc.trading_date).open_ms_utc
        return RunFailure(
            FEE_RATE_NOT_PUBLISHED,
            f"Estimated fees cannot be priced for a session in the range: the fee model has no published rate for "
            f"{', '.join(exc.components)}. Charge a fee per contract or none, or start the run later.",
            (FailureItem(opened, "no_published_fee_rates", f"no published rate for {', '.join(exc.components)}"),),
        )
    if isinstance(
        exc, LakeAdmissionError | OptionBarsFileError | OptionListingFileError | UnderlyingDayUnreadable | MalformedBars
    ):
        return RunFailure(
            DATA_UNREADABLE,
            f"A stored file the run needs cannot be read ({type(exc).__name__}). Nothing was saved.",
        )
    if isinstance(exc, CatalogUnavailableError):
        return RunFailure(
            DATA_CATALOG_UNAVAILABLE, "The data catalog could not be reached, so the run stopped. Nothing was saved."
        )
    if isinstance(exc, CallerStoppedWaitingError):
        return RunFailure(
            DOWNLOAD_FAILED,
            "The lake took longer to answer than the run waits. What it fetched is kept; run again to go on.",
        )
    logger.exception("an options backtest run failed unexpectedly", extra={"action": "options_backtest_run_failed"})
    return RunFailure(
        INTERNAL_ERROR, f"The run stopped because of an unexpected error ({type(exc).__name__}). Nothing was saved."
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
