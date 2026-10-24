"""A member's option inputs, saved into the lake and read back: the real writers, readers and admission, with only the vendor faked (#3540; ADR 0078).

``admit_member_option_inputs`` picks a member's contracts from the lake's stored listings, folds each contract-day to a price, and
captures what the lake lacks through the lake's own writers. These tests run it over a pinned weekly chain
(``tests/_helpers/option_chain``) served at the vendor's HTTP seam (``tests/_helpers/option_chain_vendor``): the vendor's listing
pages, its minute bars, the claim and lease protocol, committed-byte admission and the session containers are the code under test.

Hand-worked figures: the window is the nine sessions 2024-10-01 to 2024-10-11, the chain lists a Friday every week and a
strike every five dollars (105 to 195), and the stock walks 150.0, 150.1 ... 150.8 a session, so each session picks one
expiry pair and the same ATM strike (150) and 25-delta wings; every session needs at most eight contract-days, and the first
use of an expiry is the only time its strikes are chosen.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import threading
import zipfile
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args
from weakref import WeakKeyDictionary

import pytest
import respx

from app.data_lake import option_lake as option_lake_module
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import encode_option_day, option_minute_trade_dch, read_option_day_bars
from app.data_lake.option_lake import OptionFailureReason, vendor_request_gate
from app.data_lake.option_listing import OptionListingFileError, read_option_listing
from app.data_lake.option_session_repack import publish_option_session, repack_option_session
from app.data_lake.path_policy import OptionListingPath, OptionMinuteBarPath, OptionSessionPath
from app.data_lake.polygon_option_bars import PolygonOptionBar
from app.data_lake.types import ArtifactFailureReason, ArtifactIdentity
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.options.contract import OptionContract
from app.options_backtest import fetching
from app.options_backtest.fetching import LakeOptionCapture
from app.research.options.option_inputs import (
    FAILURE_VERDICT,
    MemberOptionInputs,
    OptionInputRefusal,
    admit_member_option_inputs,
    needed_contract_days,
)
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.background_loop import background_loop, run_on_background_loop
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_chain import OptionChain, member_inputs, stock_rows
from tests._helpers.option_chain_vendor import ChainVendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root, replace_the_stored_file
from tests.options_backtest._lake import tmp_lake as tmp_lake

D = Decimal
SESSIONS = expected_sessions(date(2024, 10, 1), date(2024, 10, 11))
FIRST, LAST = SESSIONS[0], SESSIONS[-1]
#: Noon on the Tuesday after the window: every session of it is over in New York, and inside the options plan.
NOW_MS = et_midnight_ms(date(2024, 10, 15)) + 12 * 3_600_000
ROWS = stock_rows(SESSIONS)


def chain(**options: Any) -> OptionChain:
    spots = {day: row["close"] for day, row in zip(SESSIONS, ROWS, strict=True)}
    return OptionChain("SPY", spots, rate=DEFAULT_RISK_FREE_RATE, strike_step=5.0, **options)


def needed_by_the_picks(pinned: OptionChain) -> dict[date, tuple[str, ...]]:
    """The contract-days the window needs, worked out in memory from the same chain and the same picks."""
    return needed_contract_days(member_inputs(pinned, ROWS, SESSIONS).bracket)


@pytest.fixture
def the_vendor(fake_catalog: FakeCatalog, tmp_lake: Path) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def admit(
    router: respx.MockRouter,
    vendor: ChainVendor,
    *,
    now_ms: int = NOW_MS,
    should_stop: Callable[[], bool] = lambda: False,
    **options: Any,
) -> MemberOptionInputs:
    vendor.mount(router)
    return admit_member_option_inputs(
        "SPY",
        spot_rows=ROWS,
        start_date=FIRST,
        end_date=LAST,
        now_ms=now_ms,
        capture=LakeOptionCapture("SPY", now_ms=now_ms, should_stop=should_stop),
        **options,
    )


def on_the_loop(coroutine: Any) -> Any:
    return run_on_background_loop(coroutine, timeout=120)


def refusal_of(router: respx.MockRouter, vendor: ChainVendor, **options: Any) -> OptionInputRefusal:
    with pytest.raises(OptionInputRefusal) as caught:
        admit(router, vendor, **options)
    return caught.value


# ---------------------------------------------------------------------------
# A cold run saves what it needs; a rerun asks the vendor for nothing
# ---------------------------------------------------------------------------


def test_a_cold_run_saves_the_listings_and_contract_days_it_needs_and_a_rerun_reads_the_same_files(the_vendor):
    pinned = chain()
    vendor = ChainVendor(pinned, page_size=100)

    first = admit(the_vendor, vendor)
    asked = vendor.requests
    again = admit(the_vendor, vendor)

    needed = needed_by_the_picks(pinned)
    assert set(first.days) == {(contract, day) for day, contracts in needed.items() for contract in contracts}
    assert len(vendor.bar_requests) == first.coverage.days_expected == len(first.days) and all(
        len(contracts) <= 8 for contracts in needed.values()
    )
    assert vendor.listing_pages > len(vendor.listing_requests) >= len(SESSIONS), "each listing is paged, and a session is listed for its proof"
    assert (first.coverage.listings_added, first.coverage.days_added) == (first.coverage.listings_expected, first.coverage.days_expected)
    assert vendor.requests == asked, "the rerun made no request: not a listing page, not a contract's bars"
    assert again.digest() == first.digest()
    assert again.bracket.equals(first.bracket) and dict(again.days) == dict(first.days)
    assert (again.coverage.listings_added, again.coverage.days_added) == (0, 0) and again.coverage.days_reused == len(first.days)


def test_a_vendor_that_answers_differently_the_second_time_changes_nothing_because_the_second_run_reads_stored_bytes(the_vendor):
    vendor = ChainVendor(chain())
    first = admit(the_vendor, vendor)
    asked = vendor.requests
    vendor.price = lambda ticker, day: 99.99
    vendor.listing_empty = set(SESSIONS)

    again = admit(the_vendor, vendor)

    assert vendor.requests == asked
    assert again.digest() == first.digest() and dict(again.days) == dict(first.days)


def test_the_progress_hook_counts_the_sessions_the_picks_price(the_vendor):
    vendor = ChainVendor(chain())
    seen: list[tuple[int, int, str]] = []

    admit(the_vendor, vendor, on_progress=lambda done, total, message: seen.append((done, total, message)))

    assert [done for done, _, _ in seen] == list(range(1, len(SESSIONS) + 1))
    assert seen[-1] == (len(SESSIONS), len(SESSIONS), f"SPY: {len(SESSIONS)} of {len(SESSIONS)} option sessions")


def test_a_stop_asked_for_while_the_run_waits_ends_it_with_the_jobs_own_cancellation_and_asks_for_no_more(the_vendor):
    vendor = ChainVendor(chain())
    stops = threading.Event()

    class Cancelled(Exception):
        pass

    def check() -> None:
        if len(vendor.bar_requests) >= 3:
            stops.set()
            raise Cancelled

    with pytest.raises(Cancelled):
        admit(the_vendor, vendor, check_cancelled=check)

    assert stops.is_set() and len(vendor.bar_requests) < 40, "cancelled at a session boundary, not after the whole window"


# ---------------------------------------------------------------------------
# The nightly archive: a container answers, with the same digest, and a partial one is completed or refused
# ---------------------------------------------------------------------------


def repack_every_session(listed_days: list[date]) -> None:
    for day in listed_days:
        result = on_the_loop(repack_option_session("SPY", day))
        assert result.failure is None, result.failure


def test_after_the_archive_compacts_the_sessions_the_reads_come_from_the_containers_with_the_same_digest_and_frames(the_vendor):
    vendor = ChainVendor(chain())
    first = admit(the_vendor, vendor)
    asked = vendor.requests
    assert first.source == "kept" and first.coverage.days_from_archive == 0
    repack_every_session(sorted({day for _, day in first.days}))

    again = admit(the_vendor, vendor)

    assert vendor.requests == asked
    assert again.digest() == first.digest(), "a member's sha256 is the original per-contract file's, whichever it was read from"
    assert again.bracket.equals(first.bracket)
    assert {key: (v.close, v.volume, v.sha256) for key, v in again.days.items()} == {
        key: (v.close, v.volume, v.sha256) for key, v in first.days.items()
    }
    assert again.coverage.days_from_archive == len(again.days) and again.source == "archive"
    assert all(day.container_sha256 is not None for day in again.days.values())


def encoded_day(pinned: OptionChain, ticker: str, day: date, volume: int = 500) -> bytes:
    """The per-contract file the lake writes for a contract-day the chain prices at 15:59 (an empty day when it does not)."""
    price = pinned.daily_price(f"O:{ticker}", day)
    bars = [] if price is None else [
        PolygonOptionBar(session_close_ms_utc(day) - 60_000, D(repr(price)), D(repr(price)), D(repr(price)), D(repr(price)), D(volume))
    ]
    return encode_option_day(bars, symbol=ticker, trading_date=day)


def test_a_complete_container_answers_every_contract_day_with_no_per_contract_file_and_no_vendor_request(the_vendor, tmp_lake):
    pinned = chain()
    vendor = ChainVendor(pinned)
    first = admit(the_vendor, vendor)
    asked = vendor.requests
    for day in sorted({day for _, day in first.days}):
        listing = read_option_listing("SPY", day)
        captured = {row.ticker.removeprefix("O:"): encoded_day(pinned, row.ticker.removeprefix("O:"), day) for row in listing.rows}
        assert on_the_loop(publish_option_session(listing, captured)).failure is None
    for contract, day in first.days:  # the archive is the only copy left
        symbol = OptionContract.from_occ_symbol(contract.removeprefix("O:"))
        (raw_root(tmp_lake) / OptionMinuteBarPath(symbol.root, symbol.occ_symbol, day).relative_path()).unlink()

    again = admit(the_vendor, vendor)

    assert vendor.requests == asked
    assert again.digest() == first.digest() and again.source == "archive"
    assert again.coverage.days_from_archive == again.coverage.days_expected == len(first.days)


def the_first_needed_contract_day(pinned: OptionChain) -> tuple[str, date]:
    needed = needed_by_the_picks(pinned)
    day = next(day for day in sorted(needed) if day > FIRST)
    return needed[day][0], day


def test_a_container_that_leaves_a_needed_contract_unresolved_captures_it_inside_the_plan(the_vendor, tmp_lake):
    pinned = chain()
    vendor = ChainVendor(pinned)
    first = admit(the_vendor, vendor)
    contract, day = the_first_needed_contract_day(pinned)
    symbol = OptionContract.from_occ_symbol(contract.removeprefix("O:"))
    (raw_root(tmp_lake) / OptionMinuteBarPath(symbol.root, symbol.occ_symbol, day).relative_path()).unlink()
    repack_every_session(sorted({d for _, d in first.days}))
    asked = len(vendor.bar_requests)

    again = admit(the_vendor, vendor)

    assert vendor.bar_requests[asked:] == [(symbol.occ_symbol, day)], "the one unresolved contract-day, and nothing else"
    assert again.digest() == first.digest()
    assert again.coverage.days_added == 1 and again.coverage.days_reused == len(first.days) - 1


def test_the_same_unresolved_contract_outside_the_plan_refuses_the_member_and_the_vendor_is_not_asked(the_vendor, tmp_lake):
    pinned = chain()
    vendor = ChainVendor(pinned)
    first = admit(the_vendor, vendor)
    contract, day = the_first_needed_contract_day(pinned)
    symbol = OptionContract.from_occ_symbol(contract.removeprefix("O:"))
    (raw_root(tmp_lake) / OptionMinuteBarPath(symbol.root, symbol.occ_symbol, day).relative_path()).unlink()
    repack_every_session(sorted({d for _, d in first.days}))
    asked = vendor.requests
    two_years_on = et_midnight_ms(date(2027, 6, 1)) + 12 * 3_600_000

    refusal = refusal_of(the_vendor, vendor, now_ms=two_years_on)

    assert vendor.requests == asked, "older than the plan: the lake holds what it holds, and the vendor is not asked"
    assert refusal.code == "data_unavailable_from_provider" and refusal.retryable is False
    assert [(issue.subject, issue.trading_date, issue.failure_reason) for issue in refusal.issues] == [(contract, day, "outside_data_plan")]


def test_a_corrupt_container_is_data_corrupt_and_never_falls_back_to_the_good_per_contract_files(the_vendor, tmp_lake, fake_catalog):
    vendor = ChainVendor(chain())
    first = admit(the_vendor, vendor)
    day = sorted({d for _, d in first.days})[2]
    repack_every_session([day])
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), str(OptionSessionPath("SPY", day).relative_path()), b"corrupt", admit=True)
    asked = vendor.requests

    refusal = refusal_of(the_vendor, vendor)

    assert vendor.requests == asked
    assert refusal.code == "data_corrupt" and refusal.retryable is True
    assert {issue.trading_date for issue in refusal.issues} == {day} and {issue.outcome for issue in refusal.issues} == {"corrupt"}
    assert "OptionSessionFileError" in refusal.issues[0].reason, "the kind of error is named; the file's own words are not"


def test_a_stored_file_the_catalog_does_not_admit_is_not_called_damaged_a_writer_may_still_be_publishing_it(the_vendor, tmp_lake, fake_catalog):
    pinned = chain()
    vendor = ChainVendor(pinned)
    first = admit(the_vendor, vendor)
    contract, day = next(iter(sorted(first.days, key=lambda key: key[1])))
    symbol = OptionContract.from_occ_symbol(contract.removeprefix("O:"))
    path = str(OptionMinuteBarPath(symbol.root, symbol.occ_symbol, day).relative_path())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, b"another writer's bytes", admit=False)

    refusal = refusal_of(the_vendor, vendor)

    assert refusal.code == "data_building" and refusal.retryable is True
    assert refusal.issues[0].subject == contract and refusal.issues[0].failure_reason == "lease_timeout"


def test_an_unreadable_stored_listing_is_refused_not_captured_over(the_vendor, tmp_lake, fake_catalog):
    """A session container embeds the listing it was made against; a listing written afresh could disagree with it."""
    vendor = ChainVendor(chain())
    admit(the_vendor, vendor)
    path = str(OptionListingPath("SPY", FIRST).relative_path())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), path, b"not a zip", admit=True)
    asked = vendor.requests

    refusal = refusal_of(the_vendor, vendor)

    assert vendor.requests == asked and refusal.code == "data_corrupt"
    assert refusal.issues[0].kind == "listing" and refusal.issues[0].trading_date == FIRST


@pytest.mark.parametrize(
    ("error_type", "code"),
    [(CatalogUnavailableError, "lake_unavailable"), (LakeAdmissionError, "data_building"),
     (OptionListingFileError, "data_corrupt")],
)
def test_a_listing_that_becomes_unreadable_before_contract_capture_refuses_the_member(the_vendor, monkeypatch, error_type, code):
    """The compact listing cache evicts the first whole listing before contract-day capture reads its proof again."""
    first_reads = 0

    def fail_on_proof_reread(underlying: str, day: date):
        nonlocal first_reads
        if day == FIRST:
            first_reads += 1
            if first_reads == 3:  # absent, captured/read back, then proof read after other listings evict it
                raise error_type("proof listing no longer readable")
        return read_option_listing(underlying, day)

    monkeypatch.setattr("app.research.options.option_inputs.read_option_listing", fail_on_proof_reread)
    vendor = ChainVendor(chain())

    refusal = refusal_of(the_vendor, vendor)

    assert refusal.code == code and refusal.retryable is True
    assert [(issue.kind, issue.subject, issue.trading_date) for issue in refusal.issues] == [("listing", "SPY", FIRST)]
    assert vendor.bar_requests == [], "no contract capture starts with an unreadable proof listing"


# ---------------------------------------------------------------------------
# What the vendor can say, and what each answer means for the member
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "code", "retryable"),
    [
        (429, "provider_rate_limited", "data_unavailable_from_provider", True),
        (401, "provider_auth_error", "data_unavailable_from_provider", True),
        (404, "unknown_symbol", "data_unavailable_from_provider", False),
    ],
)
def test_a_vendor_that_refuses_a_contracts_bars_refuses_the_member_with_the_reason(the_vendor, status, reason, code, retryable):
    pinned = chain()
    contract, _ = the_first_needed_contract_day(pinned)
    vendor = ChainVendor(pinned, bar_status={contract.removeprefix("O:"): status})

    refusal = refusal_of(the_vendor, vendor)

    assert (refusal.code, refusal.retryable) == (code, retryable)
    assert [issue.failure_reason for issue in refusal.issues] == [reason] and refusal.issues[0].subject == contract


@pytest.mark.parametrize(
    ("how", "reason", "code"),
    [("status", "provider_auth_error", "data_unavailable_from_provider"), ("empty", "provider_no_data", "data_unavailable_from_provider")],
)
def test_a_listing_the_vendor_refuses_or_answers_with_nothing_refuses_the_member(the_vendor, how, reason, code):
    vendor = ChainVendor(chain())
    if how == "status":
        vendor.listing_status[FIRST] = 401
    else:
        vendor.listing_empty.add(FIRST)

    refusal = refusal_of(the_vendor, vendor)

    assert refusal.code == code and refusal.issues[0].kind == "listing" and refusal.issues[0].failure_reason == reason
    assert vendor.bar_requests == [], "no contract was asked for on a chain that could not be listed"


def test_a_contract_day_another_request_holds_past_the_wait_is_data_building_and_the_vendor_is_not_asked_for_it(
    the_vendor, fake_catalog, monkeypatch
):
    pinned = chain()
    contract, day = the_first_needed_contract_day(pinned)
    symbol = OptionContract.from_occ_symbol(contract.removeprefix("O:"))
    vendor = ChainVendor(pinned)
    held = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa_options", symbol=symbol.occ_symbol, trading_date=day,
        resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw",
    )
    assert on_the_loop(fake_catalog.claim_minute_bar(held, "another-worker", 600_000, option_minute_trade_dch(), "x.zip")) is not None
    monkeypatch.setattr(fetching, "OTHER_REQUEST_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(fetching, "OTHER_REQUEST_POLL_SECONDS", 0.05)

    refusal = refusal_of(the_vendor, vendor)

    assert refusal.code == "data_building" and refusal.retryable is True
    assert [(i.subject, i.trading_date, i.failure_reason) for i in refusal.issues] == [(contract, day, "lease_timeout")]
    assert (symbol.occ_symbol, day) not in vendor.bar_requests


def test_a_contract_that_the_listing_of_a_later_day_no_longer_holds_is_not_listed_and_not_retryable(the_vendor):
    pinned = chain()
    contract, day = the_first_needed_contract_day(pinned)
    vendor = ChainVendor(pinned, unlisted=lambda ticker, as_of: ticker == contract and as_of == day)

    refusal = refusal_of(the_vendor, vendor)

    assert refusal.code == "data_missing" and refusal.retryable is False
    assert [(i.subject, i.trading_date, i.failure_reason) for i in refusal.issues] == [(contract, day, "not_listed")]


def test_a_window_whose_last_session_is_not_over_in_new_york_yet_is_window_not_complete(the_vendor):
    just_after_the_close = session_close_ms_utc(LAST) + 3_600_000
    vendor = ChainVendor(chain())

    refusal = refusal_of(the_vendor, vendor, now_ms=just_after_the_close)

    assert refusal.code == "window_not_complete" and refusal.retryable is False
    assert {issue.trading_date for issue in refusal.issues} == {LAST} and refusal.issues[0].failure_reason == "session_not_over"


def test_every_reason_the_lake_can_give_for_an_option_capture_has_a_verdict_for_the_member():
    reasons: set[str] = set()
    for part in get_args(OptionFailureReason):
        reasons.update(get_args(part))

    assert reasons >= set(get_args(ArtifactFailureReason))
    assert set(FAILURE_VERDICT) == reasons, "a reason the lake gains must be given a meaning here"
    assert {verdict[0] for verdict in FAILURE_VERDICT.values()} <= {
        "unavailable_from_provider", "building", "missing", "window_not_complete", "symbol_not_addressable",
    }
    assert {r for r, (_, retryable) in FAILURE_VERDICT.items() if not retryable} == {
        "outside_data_plan", "unknown_symbol", "not_listed", "session_not_over", "not_a_session", "symbol_too_long",
    }


# ---------------------------------------------------------------------------
# Load: the study is a bounded foreground user of the process's vendor gate
# ---------------------------------------------------------------------------


def test_the_study_never_has_more_than_eight_requests_in_flight_and_leaves_the_rest_of_the_gate_to_the_archive(
    the_vendor, monkeypatch
):
    """With the gate at the archive machine's 32 slots, a waiter for a slot is served while the study is at its peak."""
    monkeypatch.setattr(option_lake_module, "MAX_VENDOR_REQUESTS_IN_FLIGHT", 32)
    monkeypatch.setattr(option_lake_module, "_request_gates", WeakKeyDictionary())
    vendor = ChainVendor(chain(), bar_latency_s=0.05, listing_latency_s=0.02)
    at_peak = threading.Event()
    waiter_served = threading.Event()
    study_failed: list[BaseException] = []

    def watch(symbol: str, day: date) -> None:
        if vendor.in_flight >= 8:
            at_peak.set()

    vendor.on_bar_request = watch

    async def the_archive_waits_for_a_slot() -> None:
        async with vendor_request_gate():
            waiter_served.set()

    def the_study() -> None:
        try:
            admit(the_vendor, vendor)
        except BaseException as error:
            study_failed.append(error)

    worker = threading.Thread(target=the_study)
    worker.start()
    try:
        assert at_peak.wait(60), "the study reached eight contract-days in flight"
        asyncio.run_coroutine_threadsafe(the_archive_waits_for_a_slot(), background_loop()).result(timeout=10)
        assert waiter_served.is_set()
    finally:
        worker.join(120)

    assert not study_failed, study_failed
    assert vendor.peak_in_flight == 8, "the study's own bound: a session's contract-days, eight at most, one session at a time"


def test_a_session_container_holds_the_exact_per_contract_files(the_vendor, tmp_lake):
    """The sha256 the digest uses is the original file's: the archive retains each member byte for byte."""
    vendor = ChainVendor(chain())
    first = admit(the_vendor, vendor)
    day = sorted({d for _, d in first.days})[0]
    repack_every_session([day])
    contract = next(c for c, d in first.days if d == day)
    symbol = OptionContract.from_occ_symbol(contract.removeprefix("O:"))

    stored = read_option_day_bars(symbol, day, listed_under="SPY")

    assert stored is not None and stored.reference is not None and stored.reference.state == "captured"
    assert stored.sha256 == first.days[(contract, day)].sha256
    container = (raw_root(tmp_lake) / OptionSessionPath("SPY", day).relative_path()).read_bytes()
    with zipfile.ZipFile(io.BytesIO(container)) as archive:
        member = archive.read(f"contracts/{symbol.occ_symbol}.zip")
    assert hashlib.sha256(member).hexdigest() == stored.sha256
