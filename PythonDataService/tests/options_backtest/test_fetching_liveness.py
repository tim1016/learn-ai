"""The download phase under load, in contention, and when a run ends (#3030).

Three things are held here. The vendor limit belongs to the process and not to a run. A day another request is fetching
right now is waited for and not failed, within a stated bound. And a phase that has stopped (told to, failed, or out of
patience) starts no further vendor request, while a healthy phase asks whether to stop often enough to keep a job's
lease alive: the job's lease lasts five minutes and is renewed only by a stop poll or an emit.

Hand-worked figures:

* Eight contracts a session (the strikes 496 to 504 less the money, 500), four requests in flight at most. A request
  takes 0.4 seconds, so the first four are in flight together and the other four would start at 0.4 seconds.
* A stop noticed 0.05 seconds in (the stop poll is shortened to that) finds four requests in flight, so four requests
  are asked for in all, and no fifth ever is. A wait that runs out at 0.3 seconds finds the same four.
* Two runs over one session of eight contracts, one starting when the other's first request arrives: each of the eight
  days is fetched once by whichever got its row first, so eight requests and not sixteen, and both runs finish.
* Fake time. A stage of the phase is held for 600 seconds (ten minutes) and the stop poll is every 5 seconds, so the
  longest stretch without asking is 5 seconds; the rule is 30.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.data_lake.option_bars import option_minute_trade_dch
from app.data_lake.option_lake import MAX_VENDOR_REQUESTS_IN_FLIGHT
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
# The stage of the phase a request belongs to, and the switch of the fake vendor that holds it.
HOLDS = {"underlying": "stock_release", "listing": "listing_release", "contracts": "option_release"}


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


def _the_500_call(day: date) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=day, right="call", strike=500)


async def _another_worker_holds(fake_catalog: FakeCatalog, what: str, day: date) -> None:
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
            symbol=_the_500_call(day).occ_symbol,
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


# ── A phase that has stopped asks for nothing more ───────────────────────


async def _the_vendor_is_asked_for_nothing_more(vendor: Vendor) -> None:
    asked = len(vendor.requests)
    await asyncio.sleep(0.6)
    assert len(vendor.requests) == asked, "a request was started after the phase had ended"


@respx.mock
async def test_a_stop_in_the_middle_of_a_session_batch_is_heard_at_once_and_no_further_request_is_made(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None
) -> None:
    # Eight contracts, four in flight at a time, 0.4 seconds each. The stop is asked for as the first request arrives.
    vendor = Vendor({WED: ladder_day()}, option_latency_s=0.4)
    vendor.mount()

    with pytest.raises(DownloadStopped):
        await _run([WED], should_stop=lambda: len(vendor.option_requests) >= 1)

    assert len(vendor.option_requests) == 4, "the four in flight finished; the other four were never asked"
    await _the_vendor_is_asked_for_nothing_more(vendor)


@respx.mock
async def test_a_wait_that_runs_out_fails_the_run_and_the_batch_is_told_to_stop_before_it_does(
    fake_catalog: FakeCatalog, tmp_lake: Path, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor = Vendor({WED: ladder_day()}, option_latency_s=0.4)
    vendor.mount()
    monkeypatch.setattr(fetching, "_CONTRACTS_TIMEOUT_SECONDS", 0.3)

    with pytest.raises(DownloadFailed) as caught:
        await _run([WED])

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


# ── A phase that waits asks whether to stop, on a clock the test controls ─


async def _store_the_underlying_and_the_listing(*, listing: bool) -> None:
    await asyncio.to_thread(
        materialize_engine_run, symbol="SPY", start=WED, end=WED, resolution="minute", price_adjustment_mode="raw"
    )
    if listing:
        await ensure_option_listing("SPY", WED, now_ms=NOW_MS)


def _pace(fake: FakeTime) -> Pace:
    return Pace(now=fake.now, wait_for=fake.wait_for)


@pytest.mark.parametrize("stage", ["underlying", "listing", "contracts"])
@respx.mock
async def test_a_stage_that_takes_ten_minutes_asks_whether_to_stop_at_least_every_thirty_seconds(
    stage: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    if stage != "underlying":
        await _store_the_underlying_and_the_listing(listing=stage == "contracts")
    release, fake = threading.Event(), FakeTime()
    setattr(vendor, HOLDS[stage], release)
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
    release, fake = threading.Event(), FakeTime()
    setattr(vendor, HOLDS[stage], release)
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
    never, fake = threading.Event(), FakeTime()
    setattr(vendor, HOLDS[stage], never)

    try:
        with pytest.raises(DownloadStopped):
            await _run([WED], should_stop=fake.stop_at(100), pace=_pace(fake))
        # The underlying's chunk cannot be told to stop and is let go at once; the others are given the grace.
        grace = 0 if stage == "underlying" else fetching.STOP_GRACE_SECONDS
        assert 100 + grace <= fake.at <= 100 + grace + fetching.STOP_POLL_SECONDS
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
