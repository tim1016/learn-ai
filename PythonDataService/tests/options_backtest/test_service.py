"""The run service end to end: a request in, a saved run out, over the lake's own functions and a faked vendor (#3030).

Four sessions of SPY, Tuesday 2026-10-06 to Friday 2026-10-09. "Now" is Monday 2026-10-12 14:00 New York, more than a
day after the last of them. One same-day at-the-money call (the underlying closes at 500.20 all day, so the money is the
500 strike), bought on the minute after the entry time (minute 5 after the open, filling at the open of minute 6) and
sold on the minute after the exit time (the exit is 46 minutes before the 390-minute close, minute 344, so the sale
fills at the open of minute 345). Slippage 0.3 percent, a price step of 0.01, a fee of 0.65 a contract on every fill,
10,000.00 to start. A buy fills at the price seen plus 0.3 percent rounded up to the cent; a sale at the price seen less
0.3 percent rounded down to the cent.

    Tue 10-06  bought at 4.00 -> 4.012 -> 4.02. The contract's last bar is minute 100, closing 3.50; the sale never
               fills, so the leg is marked at 3.50 (no exit fee):  (3.50 - 4.02) x 100 - 0.65 = -52.65   incomplete
    Wed 10-07  bought at 2.00 -> 2.006 -> 2.01, sold at 2.40 -> 2.3928 -> 2.39:
               (2.39 - 2.01) x 100 - 0.65 - 0.65 = 38.00 - 1.30 = +36.70
    Thu 10-08  bought at 3.00 -> 3.009 -> 3.01, sold at 2.50 -> 2.4925 -> 2.49:
               (2.49 - 3.01) x 100 - 1.30 = -52.00 - 1.30 = -53.30
    Fri 10-09  the contract was listed and never traded: the entry is dropped, no trade, 0.00

    Cash: 10,000.00 - 52.65 = 9,947.35; + 36.70 = 9,984.05; - 53.30 = 9,930.75; Friday leaves it there.
    Net profit -52.65 + 36.70 - 53.30 = -69.25. Gross profit 36.70, gross loss 52.65 + 53.30 = 105.95.
    Fees 0.65 + 1.30 + 1.30 = 3.25. Slippage paid: Tue 2.00 (4.02 - 4.00, x 100); Wed 1.00 + 1.00; Thu 1.00 + 1.00 = 6.00.
    Equity falls from the starting 10,000.00 to 9,930.75 at Thursday's end: 69.25.
    At no slippage: Tue (3.50 - 4.00) x 100 - 0.65 = -50.65; Wed 40.00 - 1.30 = 38.70; Thu -50.00 - 1.30 = -51.30;
    net -63.25. A loss even at the lowest value on the ladder.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import respx

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.engine import run_gate
from app.jobs.phases import JOB_PHASES
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest import service
from app.options_backtest.data_check import DataCheck
from app.options_backtest.fees import FeeRateNotPublished
from app.options_backtest.fetching import DownloadFailed, DownloadReport
from app.options_backtest.lake_data import UnderlyingDayUnreadable
from app.options_backtest.results import SessionStatus
from app.options_backtest.run import RunResult, SessionDataMissing
from app.options_backtest.saved_runs import DbRunStore, NewRun, RunFailure, create_run, get_run, read_result
from app.options_backtest.service import (
    MAX_RANGE_SESSIONS,
    RunCancelled,
    RunFailedError,
    RunHooks,
    RunRefused,
    execute_run,
    failure_of,
    sessions_of_range,
)
from app.options_backtest.session_market import MalformedBars
from app.options_backtest.setup import FeePolicy, OptionsBacktestSetup
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.session_anchors import et_day_end_ms, et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import FRI, THU, WED, Day, flat, steady
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import leg
from tests.options_backtest.setup_fixtures import setup as setup_of
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal
# 2026-10-12 14:00 New York.
NOW_MS = 1791828000000
TUE = date(2026, 10, 6)
SESSION_OF_THE_RANGE = [TUE, WED, THU, FRI]
CALL_500 = ("call", 500)
FINISHED_AT_MS = NOW_MS + 5

for _day in (TUE, WED, THU, FRI):
    session_window_for_date(_day)  # raises for a day that is not a session


def the_setup() -> OptionsBacktestSetup:
    return setup_of(
        legs=[leg(expiry={"kind": "same_day"})],
        fees=FeePolicy(kind="per_contract", per_contract_usd=D("0.65")).model_dump(mode="json"),
        slippage_percent="0.3",
    )


def the_scene() -> dict[date, Day]:
    return {
        TUE: Day(underlying=steady("500.20"), options={CALL_500: {**flat("4.00", 5, 99), 100: ("4.00", "3.50")}}),
        WED: Day(underlying=steady("500.20"), options={CALL_500: {**flat("2.00", 5, 344), 345: ("2.40", "2.40")}}),
        THU: Day(underlying=steady("500.20"), options={CALL_500: {**flat("3.00", 5, 344), 345: ("2.50", "2.50")}}),
        FRI: Day(underlying=steady("500.20"), options={CALL_500: {}}),
    }


@dataclass
class FakeStore:
    """Records how a run ended, as ``DbRunStore`` would write it."""

    completed: list[tuple[int, RunResult, int]] = field(default_factory=list)
    failed: list[tuple[int, RunFailure, int]] = field(default_factory=list)
    cancelled: list[tuple[int, int]] = field(default_factory=list)

    def complete(self, run_id: int, result: RunResult, *, finished_at_ms: int) -> None:
        self.completed.append((run_id, result, finished_at_ms))

    def fail(self, run_id: int, failure: RunFailure, *, finished_at_ms: int) -> None:
        self.failed.append((run_id, failure, finished_at_ms))

    def cancel(self, run_id: int, *, finished_at_ms: int) -> None:
        self.cancelled.append((run_id, finished_at_ms))

    @property
    def endings(self) -> int:
        return len(self.completed) + len(self.failed) + len(self.cancelled)


@dataclass
class Job:
    """The callbacks of a job, remembered, with a way to ask for a stop at a chosen moment."""

    phases: list[str] = field(default_factory=list)
    progress: list[tuple[str, int, int]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    on_progress_hook: Any = None

    @property
    def hooks(self) -> RunHooks:
        return RunHooks(
            on_phase=self.phases.append,
            on_progress=self._progress,
            on_log=self.logs.append,
            should_stop=self.stop.is_set,
        )

    def _progress(self, done: int, total: int) -> None:
        phase = self.phases[-1] if self.phases else ""
        self.progress.append((phase, done, total))
        if self.on_progress_hook is not None:
            self.on_progress_hook(phase, done, total)


def _clock() -> Callable[[], int]:
    """The first reading is when the run starts, and every later one is when it ends."""
    readings = itertools.count()
    return lambda: NOW_MS if next(readings) == 0 else FINISHED_AT_MS


def _watching(job: Job, phase: str, seen: threading.Event) -> RunHooks:
    """The job's hooks, which also say when the run reports ``phase``."""
    hooks = job.hooks

    def on_phase(reported: str) -> None:
        hooks.on_phase(reported)
        if reported == phase:
            seen.set()

    return RunHooks(on_phase, hooks.on_progress, hooks.on_log, hooks.should_stop)


async def _execute(
    store: FakeStore,
    job: Job,
    *,
    setup: OptionsBacktestSetup | None = None,
    sessions: list[date] | None = None,
    hooks: RunHooks | None = None,
) -> RunResult:
    return await asyncio.to_thread(
        execute_run,
        run_id=7,
        setup=setup or the_setup(),
        sessions=sessions or SESSION_OF_THE_RANGE,
        store=store,
        hooks=hooks or job.hooks,
        clock=_clock(),
    )


# ── The seam: a request in, a saved run out ──────────────────────────────


@respx.mock
async def test_a_request_becomes_a_saved_run_with_every_figure_the_hand_worked_scene_says(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    store, job = FakeStore(), Job()

    returned = await _execute(store, job)

    [(run_id, saved, finished_at_ms)] = store.completed
    assert returned is saved and run_id == 7 and finished_at_ms == FINISHED_AT_MS
    assert store.failed == [] and store.cancelled == []
    summary = saved.summary
    assert [(s.trading_date, s.status) for s in saved.sessions] == [
        (TUE, SessionStatus.INCOMPLETE),
        (WED, SessionStatus.TRADED),
        (THU, SessionStatus.TRADED),
        (FRI, SessionStatus.NO_TRADE),
    ]
    assert [s.pnl_usd for s in saved.sessions] == [D("-52.65"), D("36.70"), D("-53.30"), D("0")]
    assert [s.cash_after_usd for s in saved.sessions] == [D("9947.35"), D("9984.05"), D("9930.75"), D("9930.75")]
    assert (
        summary.sessions_tested,
        summary.sessions_traded,
        summary.sessions_no_trade,
        summary.sessions_incomplete,
    ) == (4, 3, 1, 1)
    assert (summary.trades, summary.trades_won, summary.trades_lost) == (3, 1, 2)
    assert summary.net_profit_usd == D("-69.25")
    assert (summary.gross_profit_usd, summary.gross_loss_usd) == (D("36.70"), D("105.95"))
    assert (summary.total_fees_usd, summary.slippage_paid_usd) == (D("3.25"), D("6.00"))
    assert summary.ending_cash_usd == D("9930.75")
    drawdown = summary.drawdown_by_dollars
    assert drawdown is not None
    assert (drawdown.peak_equity_usd, drawdown.trough_equity_usd, drawdown.fall_usd) == (
        D("10000.00"),
        D("9930.75"),
        D("69.25"),
    )
    assert drawdown.peak_date is None and drawdown.trough_date == THU


@respx.mock
async def test_the_ladder_replays_the_whole_run_and_a_run_that_loses_even_with_no_slippage_survives_none(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    store = FakeStore()

    await _execute(store, Job())

    saved = store.completed[0][1]
    by_percent = {point.slippage_percent: point for point in saved.slippage}
    assert by_percent[D("0")].net_profit_usd == D("-63.25") and by_percent[D("0")].trades == 3
    assert by_percent[D("0.3")].net_profit_usd == D("-69.25") and by_percent[D("0.3")].is_setups_own
    assert saved.slippage_tolerance.highest_profitable_percent is None
    assert saved.slippage_tolerance.first_unprofitable_percent == D("0")


@respx.mock
async def test_the_job_reports_its_phases_in_order_and_progress_in_sessions(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    job = Job()

    await _execute(FakeStore(), job)

    assert job.phases == ["checking_data", "downloading_data", "replaying", "saving"]
    registered = [phase.id for phase in JOB_PHASES["options_backtest"]]
    assert [registered.index(phase) for phase in job.phases] == sorted(registered.index(phase) for phase in job.phases)
    assert [(phase, done, total) for phase, done, total in job.progress if phase == "downloading_data"] == [
        ("downloading_data", n, 4) for n in (1, 2, 3, 4)
    ]
    assert [(phase, done, total) for phase, done, total in job.progress if phase == "replaying"] == [
        ("replaying", n, 4) for n in (1, 2, 3, 4)
    ]


@respx.mock
async def test_a_second_request_for_the_same_range_fetches_nothing_and_gives_the_same_run(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(the_scene())
    vendor.mount()
    first_store = FakeStore()
    await _execute(first_store, Job())
    asked = vendor.vendor_requests

    second_store = FakeStore()
    await _execute(second_store, Job())

    assert vendor.vendor_requests == asked
    assert second_store.completed[0][1] == first_store.completed[0][1]


# ── Refused before anything is fetched ───────────────────────────────────


@respx.mock
async def test_a_session_that_cannot_be_tested_fails_the_run_at_once_naming_it_and_nothing_is_fetched_or_replayed(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    older_than_the_plan = date(2024, 10, 11)
    vendor = Vendor({**the_scene(), older_than_the_plan: Day(underlying=steady("500.20"))})
    vendor.mount()
    store, job = FakeStore(), Job()

    with pytest.raises(RunFailedError) as caught:
        # The range is not shortened: Tuesday to Friday would run, and the whole run is refused for the one session.
        await _execute(store, job, sessions=[older_than_the_plan, *SESSION_OF_THE_RANGE])

    [(_, failure, _)] = store.failed
    assert failure.code == "SESSIONS_CANNOT_BE_TESTED" and caught.value.failure == failure
    assert "1 of 5 sessions" in failure.reason and "2024-10-11" in failure.reason
    assert [(item.session_open_ms, item.code) for item in failure.items] == [
        (session_window_for_date(older_than_the_plan).open_ms_utc, "older_than_the_data_plan")
    ]
    assert vendor.vendor_requests == 0
    assert job.phases == ["checking_data"], "no download and no replay began"
    assert store.completed == [] and store.cancelled == []


@respx.mock
async def test_estimated_fees_on_a_date_without_published_rates_fail_the_run_before_any_fetch(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The fee model's first published rates are of July 2026; these two sessions are of the last days of June.
    june = [date(2026, 6, 29), date(2026, 6, 30)]
    vendor = Vendor(the_scene())
    vendor.mount()
    store = FakeStore()
    estimated = the_setup().model_copy(update={"fees": FeePolicy(kind="estimated")})

    with pytest.raises(RunFailedError):
        await _execute(store, Job(), setup=estimated, sessions=june)

    [(_, failure, _)] = store.failed
    assert failure.code == "SESSIONS_CANNOT_BE_TESTED"
    assert [item.code for item in failure.items] == ["no_published_fee_rates", "no_published_fee_rates"]
    assert [item.session_open_ms for item in failure.items] == [
        session_window_for_date(day).open_ms_utc for day in june
    ]
    assert vendor.vendor_requests == 0


# ── A failure in the download ────────────────────────────────────────────


@respx.mock
async def test_a_vendor_failure_in_the_middle_of_the_download_fails_the_run_naming_the_session_and_replays_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(the_scene(), option_errors={"SPY261008C00500000": 429})
    vendor.mount()
    store, job = FakeStore(), Job()

    with pytest.raises(RunFailedError):
        await _execute(store, job)

    [(_, failure, _)] = store.failed
    assert failure.code == "DOWNLOAD_FAILED"
    assert (
        "nothing was replayed" in failure.reason
        and "2026-10-08" in failure.reason
        and "provider_rate_limited" in failure.reason
    )
    assert [(item.session_open_ms, item.code) for item in failure.items] == [
        (session_window_for_date(THU).open_ms_utc, "provider_rate_limited")
    ]
    assert "replaying" not in job.phases and store.completed == []
    assert FRI not in vendor.listing_requests, "nothing after the failed session was fetched"


@respx.mock
async def test_a_download_that_left_something_missing_does_not_let_the_replay_go_on_over_it(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing was fetched at all, as if the download had stopped short without saying so: the replay finds the
    # underlying's day missing and the run fails, naming the session.
    Vendor(the_scene()).mount()
    monkeypatch.setattr(service, "ensure_run_data", lambda *args, **kwargs: DownloadReport(0, 0, 0, 0, 0))
    monkeypatch.setattr(service, "check_data", lambda *args, **kwargs: DataCheck((), date(2024, 10, 14)))
    store = FakeStore()

    with pytest.raises(RunFailedError):
        await _execute(store, Job())

    [(_, failure, _)] = store.failed
    assert failure.code == "DATA_MISSING"
    assert failure.items[0].session_open_ms == session_window_for_date(TUE).open_ms_utc
    assert store.completed == []


# ── Cancel ───────────────────────────────────────────────────────────────


@respx.mock
async def test_a_cancel_during_the_download_marks_the_run_cancelled_and_saves_nothing_else(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(the_scene())
    job, store = Job(), FakeStore()
    # Asked for as the first contract of the first session is requested.
    vendor.on_option_request = lambda _symbol: job.stop.set()
    vendor.mount()

    with pytest.raises(RunCancelled):
        await _execute(store, job)

    assert store.cancelled == [(7, FINISHED_AT_MS)]
    assert store.completed == [] and store.failed == []
    assert "replaying" not in job.phases and "saving" not in job.phases
    assert vendor.listing_requests == [TUE], "no session after the one in flight was begun"


@respx.mock
async def test_a_cancel_during_the_replay_marks_the_run_cancelled_and_saves_no_part_of_it(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    job, store = Job(), FakeStore()
    job.on_progress_hook = lambda phase, done, _total: job.stop.set() if (phase, done) == ("replaying", 2) else None

    with pytest.raises(RunCancelled):
        await _execute(store, job)

    assert store.cancelled == [(7, FINISHED_AT_MS)]
    assert store.completed == [] and store.failed == []
    assert [(done) for phase, done, _ in job.progress if phase == "replaying"] == [1, 2], (
        "the third session was never begun"
    )
    assert "saving" not in job.phases


@respx.mock
async def test_a_cancel_that_comes_during_the_last_session_still_cancels_the_run_before_it_is_saved(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The replay asks to stop before each session and never after the last, so a cancel that arrives while the last
    # one is replayed is found only by the check made before the save.
    Vendor(the_scene()).mount()
    job, store = Job(), FakeStore()
    job.on_progress_hook = lambda phase, done, _total: job.stop.set() if (phase, done) == ("replaying", 4) else None

    with pytest.raises(RunCancelled):
        await _execute(store, job)

    assert store.cancelled == [(7, FINISHED_AT_MS)] and store.completed == []
    assert "saving" not in job.phases


@respx.mock
async def test_a_stop_that_comes_before_anything_starts_cancels_without_a_check(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor(the_scene())
    vendor.mount()
    job, store = Job(), FakeStore()
    job.stop.set()

    with pytest.raises(RunCancelled):
        await _execute(store, job)

    assert len(store.cancelled) == 1 and job.phases == [] and vendor.vendor_requests == 0


# ── The run gate ─────────────────────────────────────────────────────────


@pytest.fixture
def quick_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_gate, "WAIT_POLL_SECONDS", 0.01)


class _HeldGate:
    """Another backtest in flight: a thread that holds the process-wide gate until told to let go."""

    def __init__(self) -> None:
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with run_gate.one_backtest_in_flight():
            self.holding.set()
            self.release.wait(timeout=30)

    def __enter__(self) -> _HeldGate:
        self.thread.start()
        assert self.holding.wait(timeout=10)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self.thread.join(timeout=10)


@respx.mock
async def test_a_run_waits_for_the_backtest_in_flight_says_so_and_goes_on_when_it_ends(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick_gate: None
) -> None:
    Vendor(the_scene()).mount()
    job, store, waiting = Job(), FakeStore(), threading.Event()

    with _HeldGate() as gate:
        run = asyncio.create_task(_execute(store, job, hooks=_watching(job, "waiting_for_engine", waiting)))
        assert await asyncio.to_thread(waiting.wait, 20), "the run never said it was waiting"
        assert store.endings == 0 and "replaying" not in job.phases
        gate.release.set()
        await run

    assert job.phases == ["checking_data", "downloading_data", "waiting_for_engine", "replaying", "saving"]
    assert len(store.completed) == 1


@respx.mock
async def test_a_cancel_while_waiting_for_the_gate_marks_the_run_cancelled(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick_gate: None
) -> None:
    Vendor(the_scene()).mount()
    job, store, waiting = Job(), FakeStore(), threading.Event()

    with _HeldGate():
        run = asyncio.create_task(_execute(store, job, hooks=_watching(job, "waiting_for_engine", waiting)))
        assert await asyncio.to_thread(waiting.wait, 20)
        job.stop.set()
        with pytest.raises(RunCancelled):
            await run

    assert store.cancelled == [(7, FINISHED_AT_MS)] and store.completed == []
    assert "replaying" not in job.phases


# ── What a failure says ──────────────────────────────────────────────────


def test_a_session_whose_data_is_missing_fails_with_the_session_named() -> None:
    failure = failure_of(SessionDataMissing(WED, underlying=False, listing=True, contracts=()))

    assert failure.code == "DATA_MISSING"
    assert "2026-10-07" in failure.reason and "contract listing" in failure.reason
    assert failure.items[0].session_open_ms == session_window_for_date(WED).open_ms_utc


def test_an_unpublished_fee_rate_fails_with_its_date_and_types() -> None:
    failure = failure_of(FeeRateNotPublished(date(2026, 6, 1), ("cat", "orf")))

    assert failure.code == "FEE_RATE_NOT_PUBLISHED"
    assert "2026-06-01" in failure.reason and "cat, orf" in failure.reason
    assert failure.items[0].code == "no_published_fee_rates"


@pytest.mark.parametrize(
    "error",
    [
        LakeAdmissionError("no receipt"),
        UnderlyingDayUnreadable("bad zip"),
        MalformedBars("a repeated minute"),
    ],
)
def test_a_stored_file_that_cannot_be_read_is_a_failure_that_says_so(error: Exception) -> None:
    failure = failure_of(error)

    assert failure.code == "DATA_UNREADABLE"
    assert type(error).__name__ in failure.reason and "Nothing was saved" in failure.reason


def test_an_unreachable_catalog_and_a_wait_that_ran_out_are_told_apart() -> None:
    assert failure_of(CatalogUnavailableError("down")).code == "DATA_CATALOG_UNAVAILABLE"
    waited = failure_of(CallerStoppedWaitingError("still running"))
    assert waited.code == "DOWNLOAD_FAILED" and "run again" in waited.reason


def test_an_unexpected_error_is_named_by_its_type_and_says_nothing_was_saved() -> None:
    failure = failure_of(KeyError("secret detail"))

    assert failure.code == "INTERNAL_ERROR"
    assert "KeyError" in failure.reason and "Nothing was saved" in failure.reason
    assert "secret detail" not in failure.reason


def test_a_download_failure_with_no_session_is_a_failure_of_the_run_as_a_whole() -> None:
    failure = failure_of(DownloadFailed(session=None, what="the minute bars of SPY", reason="x", detail="y"))

    assert failure.code == "DOWNLOAD_FAILED" and failure.items[0].session_open_ms is None


# ── The range ────────────────────────────────────────────────────────────


def test_a_range_is_the_sessions_between_new_york_midnight_of_the_first_date_and_the_end_of_the_last() -> None:
    # Saturday 10-03 to Sunday 10-11 holds the sessions of two weeks, less the weekend days.
    start_ms, end_ms = et_midnight_ms(date(2026, 10, 3)), et_day_end_ms(date(2026, 10, 11))

    assert sessions_of_range(start_ms, end_ms) == (
        date(2026, 10, 5),
        TUE,
        WED,
        THU,
        FRI,
    )


def test_an_end_at_new_york_midnight_leaves_that_day_out() -> None:
    assert sessions_of_range(et_midnight_ms(WED), et_midnight_ms(FRI)) == (WED, THU)


def test_a_range_of_a_weekend_holds_no_session_and_an_empty_or_reversed_one_is_refused() -> None:
    saturday, sunday = date(2026, 10, 10), date(2026, 10, 11)
    for start_ms, end_ms, code in (
        (et_midnight_ms(saturday), et_day_end_ms(sunday), "OPTIONS_BACKTEST_RANGE_HAS_NO_SESSION"),
        (et_midnight_ms(WED), et_midnight_ms(WED), "OPTIONS_BACKTEST_RANGE_EMPTY"),
        (et_midnight_ms(FRI), et_midnight_ms(WED), "OPTIONS_BACKTEST_RANGE_EMPTY"),
    ):
        with pytest.raises(RunRefused) as caught:
            sessions_of_range(start_ms, end_ms)
        assert caught.value.code == code


def test_a_range_of_exactly_the_most_sessions_a_run_takes_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_RANGE_SESSIONS", 4)

    assert len(sessions_of_range(et_midnight_ms(TUE), et_day_end_ms(FRI))) == 4


def test_a_range_past_the_calendars_reach_or_longer_than_a_run_takes_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RunRefused) as far:
        sessions_of_range(et_midnight_ms(WED), et_midnight_ms(date(2300, 1, 1)))
    assert far.value.code == "OPTIONS_BACKTEST_RANGE_TOO_FAR"

    monkeypatch.setattr(service, "MAX_RANGE_SESSIONS", 3)
    with pytest.raises(RunRefused) as long:
        sessions_of_range(et_midnight_ms(TUE), et_day_end_ms(FRI))
    assert long.value.code == "OPTIONS_BACKTEST_RANGE_TOO_LONG" and "4 trading days" in long.value.message
    assert MAX_RANGE_SESSIONS == 1_300


# ── Saved to the database ────────────────────────────────────────────────
# These run only against a database attested as disposable and skip without one.


@pytest.fixture
async def accepted(conn: asyncpg.Connection, unique: str):  # type: ignore[no-untyped-def]
    """A run row as the job route makes it, and the clean-up of every row this test made."""

    async def accept(sessions: list[date]) -> int:
        return await create_run(
            conn,
            NewRun(
                job_id=f"service-{unique}-{len(sessions)}",
                created_at_ms=NOW_MS,
                setup=the_setup(),
                setup_name=None,
                first_session_open_ms=session_window_for_date(sessions[0]).open_ms_utc,
                last_session_open_ms=session_window_for_date(sessions[-1]).open_ms_utc,
                sessions_requested=len(sessions),
            ),
        )

    yield accept
    await conn.execute("DELETE FROM research_options_backtest_runs WHERE job_id LIKE $1", f"service-{unique}-%")


@respx.mock
async def test_a_run_saved_by_the_service_reads_back_as_the_run_the_replay_made(
    conn: asyncpg.Connection, accepted: Any, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    run_id = await accepted(SESSION_OF_THE_RANGE)

    made = await asyncio.to_thread(
        execute_run,
        run_id=run_id,
        setup=the_setup(),
        sessions=SESSION_OF_THE_RANGE,
        store=DbRunStore(),
        hooks=Job().hooks,
        clock=_clock(),
    )

    assert await read_result(conn, run_id) == made
    stored = await get_run(conn, run_id, is_live=lambda _job_id: False)
    assert stored is not None
    assert (stored.record.status, stored.record.finished_at_ms) == ("complete", FINISHED_AT_MS)
    assert stored.record.headline is not None and stored.record.headline.net_profit_usd == D("-69.25")


@respx.mock
async def test_a_run_that_cannot_be_tested_is_saved_failed_with_no_figures(
    conn: asyncpg.Connection, accepted: Any, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    too_old = [date(2024, 10, 11)]
    run_id = await accepted(too_old)

    with pytest.raises(RunFailedError):
        await asyncio.to_thread(
            execute_run,
            run_id=run_id,
            setup=the_setup(),
            sessions=too_old,
            store=DbRunStore(),
            hooks=Job().hooks,
            clock=_clock(),
        )

    stored = await get_run(conn, run_id, is_live=lambda _job_id: False)
    assert stored is not None and stored.figures is None
    assert stored.record.status == "failed" and stored.record.failure is not None
    assert stored.record.failure.code == "SESSIONS_CANNOT_BE_TESTED"
    assert stored.record.failure.items[0].code == "older_than_the_data_plan"
    assert await read_result(conn, run_id) is None


@respx.mock
async def test_a_run_cancelled_before_it_starts_is_saved_cancelled(
    conn: asyncpg.Connection, accepted: Any, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    run_id = await accepted(SESSION_OF_THE_RANGE)
    job = Job()
    job.stop.set()

    with pytest.raises(RunCancelled):
        await asyncio.to_thread(
            execute_run,
            run_id=run_id,
            setup=the_setup(),
            sessions=SESSION_OF_THE_RANGE,
            store=DbRunStore(),
            hooks=job.hooks,
            clock=_clock(),
        )

    stored = await get_run(conn, run_id, is_live=lambda _job_id: False)
    assert stored is not None and stored.record.status == "cancelled" and stored.figures is None
