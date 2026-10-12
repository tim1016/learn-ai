"""The download phase of a run: bring into the lake everything the run can need (#3030).

Formula: none. For each session that needs data, in date order: the underlying's raw minute bars, the session's
  listing of option contracts, and then the minute bars of each contract ``session_needs`` names.
Reference: the order of ``run_sessions`` (a session whose window does not fit reads nothing; any other reads the
  underlying's day, the listing and the needed contracts); and the lake's own capture functions, which are the only way
  anything is fetched here.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_fetching.py.

Everything is fetched through the lake's functions and nothing else, so its rules decide what may be fetched and how a
failure reads: ``materialize_engine_run`` for the underlying, ``ensure_option_listing`` for a listing and
``ensure_option_day_bars`` for a contract's day. A typed failure from any of them for something a session needs stops
the phase with ``DownloadFailed``, naming the session and the lake's reason: a run never goes on over data that was
only partly fetched. The listing of a session is read from the lake once, and handed to every contract capture of that
session, so it is not parsed again for each contract.

The functions of the lake that fetch options are coroutines whose catalog pool belongs to one event loop; a worker
thread has none. The phase therefore hands each batch to the process's background loop and waits for it, as
``materialize_engine_run`` does for the underlying. At most ``MAX_VENDOR_REQUESTS_IN_FLIGHT`` contract requests are
in flight at once; a session's batch is finished before the next session starts, and once one request has failed no
further request of that batch begins. A stop is asked for between sessions and between chunks of the underlying.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from app.data_lake.option_bars import ensure_option_day_bars
from app.data_lake.option_lake import OptionCaptureResult
from app.data_lake.option_listing import OptionListing, ensure_option_listing
from app.data_lake.run_materialization import LakeMaterializationError, materialize_engine_run
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.lake_data import LakeRunData, captured_listing_of
from app.options_backtest.needs import session_needs
from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.background_loop import run_on_background_loop

# The most contract requests the vendor is asked for at once.
MAX_VENDOR_REQUESTS_IN_FLIGHT = 4
# Sessions of the underlying asked for in one call. That call waits at most ten minutes for a window and reports
# nothing until it returns, so a long range is asked for in chunks: each has its own wait, a line in the log and a stop.
UNDERLYING_CHUNK_SESSIONS = 25
# How long the phase waits for one session's listing, and for one session's contracts.
_LISTING_TIMEOUT_SECONDS = 600.0
_CONTRACTS_TIMEOUT_SECONDS = 1800.0


class DownloadFailed(Exception):
    """The lake could not bring in something a session needs. Nothing after it was fetched.

    ``session`` is ``None`` for the underlying's bars, which are asked for in chunks. ``reason`` is the lake's own
    reason code; ``detail`` is its words.
    """

    def __init__(self, *, session: date | None, what: str, reason: str, detail: str) -> None:
        self.session = session
        self.what = what
        self.reason = reason
        self.detail = detail
        where = "" if session is None else f" for {session.isoformat()}"
        super().__init__(f"{what}{where} could not be fetched ({reason}): {detail}")


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


def ensure_run_data(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    *,
    now_ms: int,
    on_progress: Callable[[int, int], None] = _ignore_progress,
    should_stop: Callable[[], bool],
    on_log: Callable[[str], None] = _ignore_log,
    data: LakeRunData | None = None,
) -> DownloadReport:
    """Bring into the lake what a run of ``setup`` over ``sessions`` (in date order) can read.

    ``on_progress(done, total)`` is called after each session, in sessions. ``should_stop()`` is asked before each
    session and each chunk of the underlying; a yes raises ``DownloadStopped``. ``now_ms`` is the instant the lake's
    rules judge a session by. Raises ``DownloadFailed`` for a typed failure of the lake; a catalog outage, a file that
    stays unreadable after it was fetched, and anything unexpected propagate as they are. Blocking: call it from a
    worker thread.
    """
    reader = data or LakeRunData(setup.underlying)
    needing = [day for day in sessions if _needs_data(setup, day)]
    counts = _Counts()
    _bring_in_the_underlying(setup.underlying, needing, should_stop=should_stop, on_log=on_log)
    needing_data = set(needing)
    for done, day in enumerate(sessions):
        if should_stop():
            raise DownloadStopped(done)
        if day in needing_data:
            _bring_in_the_session(
                setup, day, reader, now_ms=now_ms, counts=counts, on_log=on_log, position=(done + 1, len(sessions))
            )
            counts.sessions += 1
        on_progress(done + 1, len(sessions))
    return counts.report()


def _needs_data(setup: OptionsBacktestSetup, day: date) -> bool:
    """A session whose window does not fit reads nothing, so it fetches nothing."""
    window = session_window_for_date(day)
    return setup.window_fits(session_open_ms=window.open_ms_utc, session_close_ms=window.close_ms_utc)


def _bring_in_the_underlying(
    underlying: str, needing: Sequence[date], *, should_stop: Callable[[], bool], on_log: Callable[[str], None]
) -> None:
    for first in range(0, len(needing), UNDERLYING_CHUNK_SESSIONS):
        chunk = needing[first : first + UNDERLYING_CHUNK_SESSIONS]
        if should_stop():
            raise DownloadStopped(0)
        on_log(
            f"Fetching {underlying} minute bars for {len(chunk)} sessions."
        )
        try:
            materialize_engine_run(
                symbol=underlying, start=chunk[0], end=chunk[-1], resolution="minute", price_adjustment_mode="raw"
            )
        except LakeMaterializationError as exc:
            raise DownloadFailed(
                session=None,
                what=f"the minute bars of {underlying}",
                reason="underlying_bars_not_materialized",
                detail=str(exc),
            ) from exc


def _bring_in_the_session(
    setup: OptionsBacktestSetup,
    day: date,
    reader: LakeRunData,
    *,
    now_ms: int,
    counts: _Counts,
    on_log: Callable[[str], None],
    position: tuple[int, int],
) -> None:
    underlying = setup.underlying
    listed = run_on_background_loop(
        ensure_option_listing(underlying, day, now_ms=now_ms), timeout=_LISTING_TIMEOUT_SECONDS
    )
    _require(listed, session=day, what=f"the {underlying} option listing")
    if listed.reused:
        counts.listings_reused += 1
    else:
        counts.listings_fetched += 1

    bars = reader.underlying_day(day)
    if bars is None:
        raise DownloadFailed(
            session=day,
            what=f"the minute bars of {underlying}",
            reason="not_in_the_lake",
            detail="they are still not in the lake after they were fetched",
        )
    stored = reader.stored_listing(day)
    if stored is None:
        raise DownloadFailed(
            session=day,
            what=f"the {underlying} option listing",
            reason="not_in_the_lake",
            detail="it is still not in the lake after it was fetched",
        )
    window = session_window_for_date(day)
    needs = session_needs(
        setup,
        trading_date=day,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        underlying=tuple(bars.value),
        listing=captured_listing_of(stored).value,
    )
    contracts = sorted(needs.contracts, key=lambda each: each.occ_symbol)
    if not contracts:
        return
    on_log(f"Session {position[0]} of {position[1]}: bringing in {len(contracts)} contract days.")
    results = run_on_background_loop(
        _ensure_contracts(contracts, day, underlying, stored, now_ms), timeout=_CONTRACTS_TIMEOUT_SECONDS
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    for contract, result in zip(contracts, results, strict=True):
        if isinstance(result, OptionCaptureResult):
            _require(result, session=day, what=f"the bars of {contract.occ_symbol}")
            if result.reused:
                counts.days_reused += 1
            else:
                counts.days_fetched += 1


async def _ensure_contracts(
    contracts: Sequence[OptionContract], day: date, underlying: str, stored: OptionListing, now_ms: int
) -> list[OptionCaptureResult | BaseException | None]:
    """Capture each contract's day, at most four requests at once; none starts once one has failed."""
    gate = asyncio.Semaphore(MAX_VENDOR_REQUESTS_IN_FLIGHT)
    stopped = False

    async def capture(contract: OptionContract) -> OptionCaptureResult | None:
        nonlocal stopped
        async with gate:
            if stopped:
                return None
            try:
                result = await ensure_option_day_bars(
                    contract, day, listed_under=underlying, listing=stored, now_ms=now_ms
                )
            except BaseException:
                stopped = True
                raise
            stopped = stopped or result.failure is not None
            return result

    return list(await asyncio.gather(*(capture(contract) for contract in contracts), return_exceptions=True))


def _require(result: OptionCaptureResult, *, session: date, what: str) -> None:
    failure = result.failure
    if failure is not None:
        raise DownloadFailed(session=session, what=what, reason=failure.reason, detail=failure.detail)
