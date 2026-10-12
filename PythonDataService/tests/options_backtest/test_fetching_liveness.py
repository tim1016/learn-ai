"""The download phase under load, in contention, and when a run ends (#3030).

Three things are held here. The vendor limit belongs to the process and not to a run. A day another request is fetching
right now is waited for and not failed, within a stated bound. And a phase that has stopped (told to, failed, or out of
patience) starts no further vendor request, while a healthy phase asks whether to stop often enough to keep a job's
lease alive: the job's lease lasts five minutes and is renewed only by a stop poll or an emit.

Hand-worked figures:

* Eight contracts a session (the strikes 496 to 504 less the money, 500), four requests in flight at most. The vendor
  holds the requests of the tests that stop, fail or run out of time a batch until the test lets them answer, so the
  first four are in flight together however long the machine takes to bring the fourth, and the other four are queued
  behind them for a place.
* The job's thread acts (stops, fails, runs out of time) at the moment the vendor says four requests are in flight,
  never at a time: a stop noticed then finds four requests in flight, so four requests are asked for in all, and no
  fifth ever is. A wait that runs out then finds the same four.
* Two runs over one session of eight contracts, one starting when the other's first request arrives: each of the eight
  days is fetched once by whichever got its row first, so eight requests and not sixteen, and both runs finish.
* Fake time. A stage of the phase is held for 600 seconds (ten minutes) and the stop poll is every 5 seconds, so the
  longest stretch without asking is 5 seconds; the rule is 30.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from concurrent.futures import wait as wait_for_futures
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.data_lake.option_bars import option_minute_trade_dch
from app.data_lake.option_lake import LEASE_TTL_MS, MAX_VENDOR_REQUESTS_IN_FLIGHT
from app.data_lake.option_listing import ensure_option_listing, option_listing_dch
from app.data_lake.run_materialization import materialize_engine_run
from app.data_lake.types import ArtifactIdentity
from app.options.contract import OptionContract
from app.options_backtest import fetching
from app.options_backtest.fetching import DownloadFailed, DownloadStopped, Pace, ensure_run_data
from app.options_backtest.lake_data import LakeRunData
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import THU, WED
from tests.options_backtest._run_lake import FakeTime, Vendor, eight_legs_setup, ladder_day
from tests.options_backtest._words import assert_plain
from tests.options_backtest.setup_fixtures import leg, setup

# 2026-10-12 14:00 New York.
NOW_MS = 1791828000000
SPY_500_CALL = OptionContract.of(root="SPY", expiry_date=WED, right="call", strike=500)
# The stage of the phase a request belongs to, the switch of the fake vendor that holds it, and the hook that says it came.
HOLDS = {"underlying": "stock_release", "listing": "listing_release", "contracts": "option_release"}
ON_REQUEST = {"underlying": "on_stock_request", "listing": "on_listing_request", "contracts": "on_option_request"}


def the_money_call() -> Any:
    """One same-day call at the money: on a 500.20 underlying, the 500 call."""
    return setup(legs=[leg(expiry={"kind": "same_day"})])


async def _run(
    sessions: list[date],
    *,
    should_stop: Any = lambda: False,
    pace: Pace | None = None,
    setup: Any = None,
    on_log: Any = None,
) -> Any:
    extra: dict[str, Any] = {} if pace is None else {"pace": pace}
    if on_log is not None:
        extra["on_log"] = on_log
    return await asyncio.to_thread(
        ensure_run_data,
        setup or eight_legs_setup(),
        sessions,
        now_ms=NOW_MS,
        should_stop=should_stop,
        **extra,
    )


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the waits that are minutes long, so the tests that run in real time take a second."""
    monkeypatch.setattr(fetching, "STOP_POLL_SECONDS", 0.05)
    monkeypatch.setattr(fetching, "STOP_GRACE_SECONDS", 5.0)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_POLL_SECONDS", 0.02)


# ── The limit belongs to the process ─────────────────────────────────────


@respx.mock
async def test_two_runs_at_once_share_the_one_limit_of_four_requests_in_flight(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    vendor = Vendor({WED: ladder_day(), THU: ladder_day()}, option_latency_s=0.15)
    vendor.hold_the_first(4)  # four are in flight together on any machine; the rest overlap as the latency lets them
    vendor.mount()

    await asyncio.gather(_run([WED]), _run([THU]))

    assert MAX_VENDOR_REQUESTS_IN_FLIGHT == 4
    assert len(vendor.option_requests) == 16
    assert vendor.peak_in_flight == 4, "two runs of four each would have been eight"


@respx.mock
async def test_a_second_run_over_the_same_day_waits_for_the_first_and_both_finish_with_each_day_fetched_once(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    first_request_arrived = threading.Event()
    vendor = Vendor({WED: ladder_day()}, option_latency_s=0.3)
    vendor.on_option_request = lambda _symbol: first_request_arrived.set()
    vendor.mount()

    async def the_second_run() -> Any:
        await asyncio.to_thread(first_request_arrived.wait, 20)
        return await _run([WED])

    first, second = await asyncio.gather(_run([WED]), the_second_run())

    assert len(vendor.option_requests) == 8, "each contract day was fetched once, by whichever run got to it first"
    assert first.option_days_fetched + second.option_days_fetched == 8
    assert (first.option_days_reused, second.option_days_reused) == (
        8 - first.option_days_fetched,
        8 - second.option_days_fetched,
    )


@respx.mock
async def test_a_second_run_over_the_same_session_waits_for_the_listing_another_run_is_fetching(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    listing_asked = threading.Event()
    vendor = Vendor({WED: ladder_day()}, listing_latency_s=0.3)
    vendor.on_listing_request = lambda _day: listing_asked.set()
    vendor.mount()

    async def the_second_run() -> Any:
        await asyncio.to_thread(listing_asked.wait, 20)
        return await _run([WED])

    await asyncio.gather(_run([WED]), the_second_run())

    assert vendor.listing_requests == [WED], "the listing was fetched once"


# ── A day another request holds for good ─────────────────────────────────


async def _hold_the_row_as_another_worker(fake_catalog: FakeCatalog, identity: ArtifactIdentity, recipe: str) -> None:
    """Another worker claimed the row a minute ago and is, as far as the lake can tell, still fetching it."""
    claimed = await fake_catalog.claim_minute_bar(identity, "another-worker", 600_000, recipe, "somewhere.zip")
    assert claimed is not None


@respx.mock
async def test_a_contract_day_another_request_holds_until_the_wait_runs_out_fails_saying_so_without_asking_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    await ensure_option_listing("SPY", WED, now_ms=NOW_MS)
    await _hold_the_row_as_another_worker(
        fake_catalog,
        ArtifactIdentity(
            artifact_kind="time_series_bars",
            market="usa_options",
            symbol=SPY_500_CALL.occ_symbol,
            trading_date=WED,
            resolution="minute",
            data_type="trade",
            provider="polygon",
            price_adjustment_mode="raw",
        ),
        option_minute_trade_dch(),
    )
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 0.3)

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED], setup=the_money_call())

    assert caught.value.session == WED
    assert caught.value.reason == "another_request_still_fetching"
    assert SPY_500_CALL.occ_symbol in caught.value.what
    assert vendor.option_requests == [], "the row was another request's, so the vendor was not asked"


@respx.mock
async def test_a_listing_another_request_holds_until_the_wait_runs_out_fails_saying_so(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    claimed = await fake_catalog.claim_metadata_artifact(
        ArtifactIdentity(
            artifact_kind="metadata",
            market="usa_options",
            symbol="SPY",
            provider="polygon",
            price_adjustment_mode="raw",
        ),
        "another-worker",
        600_000,
        option_listing_dch("SPY", WED),
        "somewhere.zip",
    )
    assert claimed is not None
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 0.3)

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED], setup=the_money_call())

    assert (caught.value.session, caught.value.reason) == (WED, "another_request_still_fetching")
    assert "listing" in caught.value.what
    assert vendor.listing_requests == [] and vendor.option_requests == []


# ── A wait for another request is said, once ─────────────────────────────
# While a capture waits for a day another request is fetching, nothing else moves: the phase and the progress stay as
# they were. So the log says so when a wait begins and when it ends, once each, however many slices it lasts and however
# many captures wait. The words are the owner's to read: no date, no contract, no count that could go stale.

WAITING = "Waiting for data another request is already fetching."
FINISHED = "The other request has finished; carrying on."
ANOTHER_WORKER = "another-worker"


def _the_call(day: date, strike: int = 500) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=day, right="call", strike=strike)


async def _another_worker_holds(fake_catalog: FakeCatalog, what: str, day: date, *, strike: int = 500) -> None:
    if what == "a listing":
        claimed = await fake_catalog.claim_metadata_artifact(
            ArtifactIdentity(
                artifact_kind="metadata",
                market="usa_options",
                symbol="SPY",
                provider="polygon",
                price_adjustment_mode="raw",
            ),
            ANOTHER_WORKER,
            600_000,
            option_listing_dch("SPY", day),
            f"option/usa/listing/spy/{day:%Y%m%d}_contracts.zip",
        )
        assert claimed is not None
        return
    await _hold_the_row_as_another_worker(
        fake_catalog,
        ArtifactIdentity(
            artifact_kind="time_series_bars",
            market="usa_options",
            symbol=_the_call(day, strike).occ_symbol,
            trading_date=day,
            resolution="minute",
            data_type="trade",
            provider="polygon",
            price_adjustment_mode="raw",
        ),
        option_minute_trade_dch(),
    )


def _let_go_of_what_another_worker_holds(fake_catalog: FakeCatalog) -> None:
    """The other request is over: the first row it held is failed, once, and the lake takes the day over."""
    held = [row for row in fake_catalog.rows.values() if row["lease_owner"] == ANOTHER_WORKER]
    assert held, "nothing was held"
    held[0].update(status="failed", last_error="provider_api_error", lease_owner=None, lease_expires_at_ms=None)


async def _store_the_underlying_and_the_listing_of(*days: date, listing: bool) -> None:
    await asyncio.to_thread(
        materialize_engine_run,
        symbol="SPY",
        start=days[0],
        end=days[-1],
        resolution="minute",
        price_adjustment_mode="raw",
    )
    if listing:
        for day in days:
            await ensure_option_listing("SPY", day, now_ms=NOW_MS)


WHAT_IS_HELD = pytest.mark.parametrize("what", ["a contract day", "a listing"])


@WHAT_IS_HELD
@respx.mock
async def test_a_wait_for_another_requests_fetch_is_said_once_when_it_begins_and_once_when_it_ends_and_the_run_completes(
    what: str, fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    await _store_the_underlying_and_the_listing_of(WED, listing=what == "a contract day")
    await _another_worker_holds(fake_catalog, what, WED)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 20.0)
    lines: list[str] = []

    def on_log(line: str) -> None:
        lines.append(line)
        if line == WAITING:
            _let_go_of_what_another_worker_holds(fake_catalog)  # the other request finishes once the wait is said

    report = await _run([WED], setup=the_money_call(), on_log=on_log)

    assert [line for line in lines if line in (WAITING, FINISHED)] == [WAITING, FINISHED]
    assert report.sessions_prepared == 1 and (report.listings_fetched, report.option_days_fetched) == (
        (1, 1) if what == "a listing" else (0, 1)
    )
    assert_plain(WAITING)
    assert_plain(FINISHED)


@respx.mock
async def test_a_second_wait_later_in_the_same_run_says_the_first_line_again_and_no_line_repeats_within_a_wait(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day(), THU: ladder_day()})
    vendor.mount()
    await _store_the_underlying_and_the_listing_of(WED, THU, listing=True)
    await _another_worker_holds(fake_catalog, "a contract day", WED)
    await _another_worker_holds(fake_catalog, "a contract day", THU)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 20.0)
    lines: list[str] = []

    def on_log(line: str) -> None:
        lines.append(line)
        if line == WAITING:
            _let_go_of_what_another_worker_holds(fake_catalog)

    await _run([WED, THU], setup=the_money_call(), on_log=on_log)

    # Wednesday's wait, then Thursday's: four lines, and each pair in order.
    assert [line for line in lines if line in (WAITING, FINISHED)] == [WAITING, FINISHED, WAITING, FINISHED]


@respx.mock
async def test_a_run_that_never_waits_for_another_request_says_nothing_about_one(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    Vendor({WED: ladder_day()}, option_latency_s=0.15).mount()
    lines: list[str] = []

    await _run([WED], on_log=lines.append)

    assert lines, "the phase says what it is doing"
    assert WAITING not in lines and FINISHED not in lines


@WHAT_IS_HELD
@respx.mock
async def test_a_wait_that_runs_out_says_it_began_once_and_never_that_the_run_carries_on(
    what: str, fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    Vendor({WED: ladder_day()}).mount()
    await _store_the_underlying_and_the_listing_of(WED, listing=what == "a contract day")
    await _another_worker_holds(fake_catalog, what, WED)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 0.5)
    lines: list[str] = []

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED], setup=the_money_call(), on_log=lines.append)

    assert caught.value.reason == "another_request_still_fetching"
    assert [line for line in lines if line in (WAITING, FINISHED)] == [WAITING]


# The two lines are only ever true. The first is written while a capture really is waiting and the phase is still going,
# and the second only when the wait ended and no capture of the batch has failed.


@respx.mock
async def test_a_wait_that_began_and_ended_before_the_job_looked_is_not_announced_once_the_run_is_over(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One contract, held by another request. The job looks every 3 seconds, and the run is over in about a tenth: the
    # other request ends at 0.1 s, the capture takes its row over at the next ask (0.02 s on) and the vendor refuses it
    # (429). The first look comes when the run has failed, so no line is written, neither "waiting" nor "carrying on".
    Vendor({WED: ladder_day()}, option_errors={"SPY261007C00500000": 429}).mount()
    await _store_the_underlying_and_the_listing_of(WED, listing=True)
    await _another_worker_holds(fake_catalog, "a contract day", WED)
    monkeypatch.setattr(fetching, "STOP_POLL_SECONDS", 3.0)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 20.0)
    lines: list[str] = []
    threading.Timer(0.1, _let_go_of_what_another_worker_holds, args=(fake_catalog,)).start()

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED], setup=the_money_call(), on_log=lines.append)

    assert caught.value.reason == "provider_rate_limited"
    assert [line for line in lines if line in (WAITING, FINISHED)] == []


@respx.mock
async def test_a_wait_that_ends_while_another_capture_has_failed_never_says_the_run_carries_on(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two contracts, the 499 call and the 501 call, each request taking 0.4 s. The 499 call is held by another request
    # and waits; the job says so at its first look (0.05 s) and the other request ends then. The 499 call takes its row
    # over at the next ask (0.02 s on) and its request ends at about 0.42 s. The 501 call was asked at the start, is
    # refused by the vendor (500) and fails at 0.4 s. So the wait ends, at 0.42 s, with a capture failed: the run fails
    # and the log says waiting once and never that it carries on.
    Vendor({WED: ladder_day()}, option_latency_s=0.4, option_errors={"SPY261007C00501000": 500}).mount()
    await _store_the_underlying_and_the_listing_of(WED, listing=True)
    await _another_worker_holds(fake_catalog, "a contract day", WED, strike=499)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 20.0)
    lines: list[str] = []

    def on_log(line: str) -> None:
        lines.append(line)
        if line == WAITING:
            _let_go_of_what_another_worker_holds(fake_catalog)

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED], setup=eight_legs_setup(steps=(-1, 1)), on_log=on_log)

    assert caught.value.reason == "provider_api_error"
    assert [line for line in lines if line in (WAITING, FINISHED)] == [WAITING]


@respx.mock
async def test_a_wait_that_ends_because_the_run_was_told_to_stop_never_says_it_carries_on(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One contract, held by another request for good. The job says it is waiting at its first look and is told to stop
    # at the same look; the capture hears the stop at its next ask (0.02 s on) and leaves the wait. The wait has ended,
    # and not because the other request finished: the log has the first line and never the second.
    Vendor({WED: ladder_day()}).mount()
    await _store_the_underlying_and_the_listing_of(WED, listing=True)
    await _another_worker_holds(fake_catalog, "a contract day", WED)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 20.0)
    lines: list[str] = []

    with pytest.raises(DownloadStopped):
        await _run([WED], setup=the_money_call(), should_stop=lambda: WAITING in lines, on_log=lines.append)

    assert [line for line in lines if line in (WAITING, FINISHED)] == [WAITING]


@respx.mock
async def test_a_capture_that_waits_for_another_request_asks_again_only_once_every_poll(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wait of 0.5 s that asks again every 0.1 s asks at 0, 0.1, 0.2, 0.3, 0.4 and, at the deadline, once more: six
    # times at the most. A wait that did not pause between asks would ask thousands of times.
    Vendor({WED: ladder_day()}).mount()
    await _store_the_underlying_and_the_listing_of(WED, listing=True)
    await _another_worker_holds(fake_catalog, "a contract day", WED)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_POLL_SECONDS", 0.1)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 0.5)
    asks = 0
    capture = fetching.ensure_option_day_bars

    async def counted_capture(*args: Any, **kwargs: Any) -> Any:
        nonlocal asks
        asks += 1
        return await capture(*args, **kwargs)

    monkeypatch.setattr(fetching, "ensure_option_day_bars", counted_capture)

    with pytest.raises(DownloadFailed):
        await _run([WED], setup=the_money_call())

    assert 2 <= asks <= 6


def test_a_capture_waits_for_another_request_for_longer_than_the_lake_lets_a_request_hold_a_row() -> None:
    # The lake's lease on a row is 300 000 ms = 300 s. A request that died while fetching holds its row for that long,
    # and the next ask takes it over only after; a wait of 300 s and a minute (360 s) outlasts it. A shorter wait would
    # fail a run for a row that was about to be free.
    assert fetching.OTHER_REQUEST_WAIT_SECONDS > LEASE_TTL_MS / 1000


# ── A phase that has stopped asks for nothing more ───────────────────────
# The captures of a batch begin one after another on the background loop, each doing some work of its own before its
# request reaches the vendor, so how many requests have arrived at a given time depends on the machine. These tests
# therefore never act at a time. The vendor holds the requests it is asked for until the test lets them answer, says when
# four are in flight (``Vendor.when_in_flight``), and the job's thread stops, fails or runs out of time at that moment.

# How long a test waits, in real time, for the vendor to have the requests it needs in flight, or for a batch to end,
# before it fails. It is far more than any machine needs, so a failure is a fault of the code or the test.
WITHIN_SECONDS = 10.0
# A reading of the phase's clock past every limit it has.
PAST_EVERY_LIMIT = 1e12


async def _happens(event: threading.Event, what: str) -> None:
    assert await asyncio.to_thread(event.wait, WITHIN_SECONDS), f"{what} did not happen within {WITHIN_SECONDS} seconds"


class _TheMoment:
    """The moment the job's thread acts, which the vendor fixes by saying that the requests the test needs are in flight.

    Until then the job's thread acts on nothing: its stop poll says no and its clock stands still. From then on the stop
    poll says yes and the clock reads past every limit, so a stop or a wait that runs out is heard at the first look after
    the requests are in flight. With ``answers``, the vendor is let answer the next time the job's thread waits: it has
    told the batch to stop before it waits again, so the batch begins no request after the vendor has answered.
    """

    def __init__(self, in_flight: threading.Event, answers: threading.Event | None = None) -> None:
        self._in_flight = in_flight
        self._answers = answers
        self._began = time.monotonic()
        self.has_come = False

    def is_here(self) -> bool:
        if not self.has_come:
            if self._in_flight.is_set():
                self.has_come = True
            elif time.monotonic() - self._began > WITHIN_SECONDS:
                raise AssertionError(f"the vendor had not four requests in flight within {WITHIN_SECONDS} seconds")
        return self.has_come

    def wait_until_here(self) -> None:
        """Hold the job's thread, inside one of its hooks, until the moment comes."""
        if not self._in_flight.wait(WITHIN_SECONDS):
            raise AssertionError(f"the vendor had not four requests in flight within {WITHIN_SECONDS} seconds")
        self.has_come = True

    def now(self) -> float:
        return PAST_EVERY_LIMIT if self.is_here() else 0.0

    def wait_for(self, future: Future[Any], seconds: float) -> None:
        if self.has_come and self._answers is not None:
            self._answers.set()
        wait_for_futures([future], timeout=seconds)

    def pace(self, *, clock: bool = False) -> Pace:
        return Pace(now=self.now, wait_for=self.wait_for) if clock else Pace(wait_for=self.wait_for)


@pytest.fixture
def the_batch_ends(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set when a session's batch of contract captures, which runs on the background loop, has returned or raised."""
    ended = threading.Event()
    batch = fetching._capture_contract_days

    async def watched(*args: Any, **kwargs: Any) -> Any:
        try:
            return await batch(*args, **kwargs)
        finally:
            ended.set()

    monkeypatch.setattr(fetching, "_capture_contract_days", watched)
    return ended


async def _the_vendor_is_asked_for_nothing_more(vendor: Vendor) -> None:
    asked = len(vendor.requests)
    await asyncio.sleep(0.6)
    assert len(vendor.requests) == asked, "a request was started after the phase had ended"


@respx.mock
async def test_a_stop_in_the_middle_of_a_session_batch_is_heard_at_once_and_no_further_request_is_made(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    # Eight contracts, four places. The vendor holds the requests it is asked for, so four are in flight together; the
    # stop is asked for the moment the fourth is, and the vendor answers once the batch has been told. The four finish,
    # and the four queued behind them for a place find the batch stopped: four requests in all, not eight.
    answers = threading.Event()
    vendor = Vendor({WED: ladder_day()}, option_release=answers)
    vendor.mount()
    moment = _TheMoment(vendor.when_in_flight(4), answers)

    try:
        with pytest.raises(DownloadStopped):
            await _run([WED], should_stop=moment.is_here, pace=moment.pace())
    finally:
        answers.set()

    assert len(vendor.option_requests) == 4, "the four in flight finished; the other four were never asked"
    await _the_vendor_is_asked_for_nothing_more(vendor)


@respx.mock
async def test_a_wait_that_runs_out_fails_the_run_and_the_batch_is_told_to_stop_before_it_does(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    # As above, but nobody asks the job to stop: its clock stands still until four requests are in flight and then reads
    # past the wait's limit, so the wait has run out with the same four in flight.
    answers = threading.Event()
    vendor = Vendor({WED: ladder_day()}, option_release=answers)
    vendor.mount()
    moment = _TheMoment(vendor.when_in_flight(4), answers)

    try:
        with pytest.raises(DownloadFailed) as caught:
            await _run([WED], pace=moment.pace(clock=True))
    finally:
        answers.set()

    assert (caught.value.session, caught.value.reason) == (WED, "took_too_long")
    assert len(vendor.option_requests) == 4
    await _the_vendor_is_asked_for_nothing_more(vendor)


@respx.mock
async def test_a_failure_in_a_batch_ends_the_phase_and_nothing_keeps_asking_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    vendor = Vendor({WED: ladder_day()}, option_latency_s=0.2, option_errors={"SPY261007C00496000": 429})
    vendor.mount()

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED])

    assert caught.value.reason == "provider_rate_limited"
    assert len(vendor.option_requests) == 4
    await _the_vendor_is_asked_for_nothing_more(vendor)


class _HookFailed(Exception):
    pass


@pytest.mark.parametrize("hook", ["the log", "the stop poll"])
@respx.mock
async def test_a_wait_the_jobs_thread_leaves_by_an_exception_stops_its_batch_so_the_vendor_is_asked_for_nothing_after_it(
    hook: str,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    quick: None,
    monkeypatch: pytest.MonkeyPatch,
    the_batch_ends: threading.Event,
) -> None:
    # Eight contracts, four places. The 496 call is held by another request: it waits, and never asks the vendor. The
    # other seven are asked four at a time, and the vendor holds the four in flight. The hook fails at the first look
    # after all four are (the log when it says the wait began, the stop poll when it is asked); the three still queued
    # must never be asked. Without the stop they are asked as the four answer, and the count goes from 4 to 7.
    answers = threading.Event()
    vendor = Vendor({WED: ladder_day()}, option_release=answers)
    vendor.mount()
    moment = _TheMoment(vendor.when_in_flight(4))
    await _store_the_underlying_and_the_listing_of(WED, listing=True)
    await _another_worker_holds(fake_catalog, "a contract day", WED, strike=496)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 20.0)

    def on_log(line: str) -> None:
        if hook == "the log" and line == WAITING:
            moment.wait_until_here()
            raise _HookFailed("the log could not be written")

    def should_stop() -> bool:
        if hook == "the stop poll" and moment.is_here():
            raise _HookFailed("the stop poll could not be read")
        return False

    try:
        with pytest.raises(_HookFailed):
            await _run([WED], setup=eight_legs_setup(), should_stop=should_stop, on_log=on_log)
        asked = len(vendor.option_requests)
        answers.set()
        await _happens(the_batch_ends, "the end of the batch")
    finally:
        answers.set()

    assert (asked, len(vendor.option_requests)) == (4, 4)


@respx.mock
async def test_an_error_in_one_capture_of_a_batch_stops_the_others_before_they_ask_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Eight contracts, four places, 0.4 s a request. The first four hold the places: the 496, 498 and 499 calls ask
    # the vendor, and the 497 call's capture raises after 0.1 s. The four queued behind it must then find the batch
    # broken and ask for nothing: three requests in all. Without that, the 501 call takes the freed place and the
    # count goes to 7.
    vendor = Vendor({WED: ladder_day()}, option_latency_s=0.4)
    vendor.mount()
    capture = fetching.ensure_option_day_bars

    async def raises_for_the_497_call(contract: OptionContract, day: date, **kwargs: Any) -> Any:
        if int(contract.strike) == 497:
            await asyncio.sleep(0.1)
            raise RuntimeError("the lake broke")
        return await capture(contract, day, **kwargs)

    monkeypatch.setattr(fetching, "ensure_option_day_bars", raises_for_the_497_call)

    with pytest.raises(RuntimeError, match="the lake broke"):
        await _run([WED])

    await asyncio.sleep(0.9)  # two requests' time
    assert len(vendor.option_requests) == 3


@respx.mock
async def test_a_request_that_never_comes_back_is_cancelled_after_the_grace_and_its_row_stays_fetching_until_its_lease_ends(
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    quick: None,
    monkeypatch: pytest.MonkeyPatch,
    the_batch_ends: threading.Event,
) -> None:
    # Eight contracts, four places, and a vendor that never answers: the first four (the 496 to 499 calls) are asked and
    # their rows claimed. The stop is heard at the first look after the fourth is in flight, the grace (0.3 s) passes and
    # the captures are cancelled. The vendor then answers, too late for anything that was cut off: had the cancel not
    # happened, the four captures would finish and their rows would read complete. They are cut off, so the four rows are
    # still fetching, and nothing was asked beyond the four.
    monkeypatch.setattr(fetching, "STOP_GRACE_SECONDS", 0.3)
    never = threading.Event()
    vendor = Vendor({WED: ladder_day()}, option_release=never)
    vendor.mount()
    moment = _TheMoment(vendor.when_in_flight(4))

    try:
        with pytest.raises(DownloadStopped):
            await _run([WED], should_stop=moment.is_here)
        # The cancel is delivered on the background loop some time after the thread that sent it has moved on, and the
        # vendor may not answer before it has been: wait until the four requests have been cut off.
        await _happens(vendor.when_every_request_has_ended(), "the cut-off of the four requests")
    finally:
        never.set()
    await _happens(the_batch_ends, "the end of the batch")

    assert _contract_day_rows(fake_catalog) == [
        ("SPY261007C00496000", "fetching"),
        ("SPY261007C00497000", "fetching"),
        ("SPY261007C00498000", "fetching"),
        ("SPY261007C00499000", "fetching"),
    ]
    assert len(vendor.option_requests) == 4

    # Once their lease has ended, the next ask for those days takes them over: with the other four contracts that is
    # eight days fetched, and every row reads complete.
    for row in list(fake_catalog.rows.values()):
        if row["status"] == "fetching":
            row["lease_expires_at_ms"] = 0
    report = await _run([WED])

    assert report.option_days_fetched == 8
    assert {status for _, status in _contract_day_rows(fake_catalog)} == {"complete"}


def _contract_day_rows(fake_catalog: FakeCatalog) -> list[tuple[str, str]]:
    """The catalog's row of each option contract day, by symbol: a snapshot, as the captures add rows from a thread."""
    return sorted(
        (row["symbol"], row["status"])
        for row in list(fake_catalog.rows.values())
        if row["market"] == "usa_options" and row["artifact_kind"] == "time_series_bars"
    )


# ── A phase that waits asks whether to stop, on a clock the test controls ─


async def _store_the_underlying_and_the_listing(*, listing: bool) -> None:
    await asyncio.to_thread(
        materialize_engine_run, symbol="SPY", start=WED, end=WED, resolution="minute", price_adjustment_mode="raw"
    )
    if listing:
        await ensure_option_listing("SPY", WED, now_ms=NOW_MS)


def _pace(fake: FakeTime) -> Pace:
    return Pace(now=fake.now, wait_for=fake.wait_for)


def _hold_the_stage(vendor: Vendor, stage: str) -> tuple[threading.Event, FakeTime]:
    """The vendor holds the requests of ``stage`` until the event returned is set, and the fake clock starts at the
    first of them: the stages before it take as many slices as the machine needs and do not use the clock up."""
    release, asked = threading.Event(), threading.Event()
    setattr(vendor, HOLDS[stage], release)
    setattr(vendor, ON_REQUEST[stage], lambda *_arguments: asked.set())
    return release, FakeTime(starts_when=asked)


@pytest.mark.parametrize("stage", ["underlying", "listing", "contracts"])
@respx.mock
async def test_a_stage_that_takes_ten_minutes_asks_whether_to_stop_at_least_every_thirty_seconds(
    stage: str, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The listing's own limit is ten minutes, the time this stage is held for, and a limit met at the moment the vendor
    # answers is a different test (the wait that runs out). The limits are put out of the way: an hour.
    for limit in ("_UNDERLYING_TIMEOUT_SECONDS", "_LISTING_TIMEOUT_SECONDS", "_CONTRACTS_TIMEOUT_SECONDS"):
        monkeypatch.setattr(fetching, limit, 3600.0)
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    if stage != "underlying":
        await _store_the_underlying_and_the_listing(listing=stage == "contracts")
    release, fake = _hold_the_stage(vendor, stage)
    fake.release_at(600, release)

    report = await _run([WED], should_stop=fake.stop_at(10_000), pace=_pace(fake))

    assert report.sessions_prepared == 1
    assert fake.at >= 600, "the stage took its ten minutes of fake time"
    assert fake.longest_gap_between_polls() <= 30
    assert len(fake.polls) >= 100


@pytest.mark.parametrize("stage", ["underlying", "listing", "contracts"])
@respx.mock
async def test_a_stop_asked_in_any_stage_is_heard_within_one_poll_and_ends_the_phase_when_the_request_in_flight_does(
    stage: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    if stage != "underlying":
        await _store_the_underlying_and_the_listing(listing=stage == "contracts")
    release, fake = _hold_the_stage(vendor, stage)
    asked = fake.stop_at(100)

    def stop_and_let_what_is_in_flight_finish() -> bool:
        said_yes = asked()
        if said_yes:
            release.set()
            fake.real_seconds_per_wait = 5.0  # let the request in flight finish before the clock moves on
        return said_yes

    with pytest.raises(DownloadStopped):
        await _run([WED], should_stop=stop_and_let_what_is_in_flight_finish, pace=_pace(fake))

    # The stop is first heard at fake second 100; the phase is over by the next poll, five seconds later.
    assert 100 <= fake.at <= 100 + fetching.STOP_POLL_SECONDS
    release.set()


@pytest.mark.parametrize("stage", ["underlying", "listing", "contracts"])
@respx.mock
async def test_a_request_that_never_comes_back_is_given_a_minute_after_the_stop_and_then_let_go(
    stage: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    if stage != "underlying":
        await _store_the_underlying_and_the_listing(listing=stage == "contracts")
    never, fake = _hold_the_stage(vendor, stage)

    try:
        with pytest.raises(DownloadStopped):
            await _run([WED], should_stop=fake.stop_at(100), pace=_pace(fake))
        # The underlying's chunk cannot be told to stop and is let go at once; the others are given the grace.
        grace = 0 if stage == "underlying" else fetching.STOP_GRACE_SECONDS
        assert 100 + grace <= fake.at <= 100 + grace + fetching.STOP_POLL_SECONDS
        if stage != "underlying":
            # The cancel reaches the request on the background loop after this thread has moved on; the vendor answers
            # only once it has, so that a request cut off is not answered (and its file written) after the test.
            await _happens(vendor.when_every_request_has_ended(), "the cut-off of the request")
    finally:
        never.set()


@respx.mock
async def test_the_lake_is_not_asked_about_the_next_session_once_a_stop_has_been_heard(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor({WED: ladder_day(), THU: ladder_day()})
    vendor.mount()
    stopped = {"yes": False}
    vendor.on_option_request = lambda _symbol: stopped.update(yes=True)

    with pytest.raises(DownloadStopped) as caught:
        await _run([WED, THU], should_stop=lambda: stopped["yes"])

    assert caught.value.sessions_done in (0, 1)
    assert THU not in vendor.listing_requests
    assert await asyncio.to_thread(LakeRunData("SPY").stored_listing, THU) is None
