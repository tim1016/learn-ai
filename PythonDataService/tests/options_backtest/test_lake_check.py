"""What the lake wrote down when it failed to fetch a thing (#3030): found through the real capture functions.

Each failure here is written by the lake's own capture (the vendor faked at the HTTP layer), and read back by
``LakeCheckData``, so the identities this file asks about are held to the ones the lake writes. A row's attempts are
the asks made of it: the first ask claims the row at attempt 1 and each ask after a failure spends one more. The lake
asks again until three are spent (``MAX_CLAIM_RETRIES``), except for an answer that the plan does not reach.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
import respx

from app.data_lake.option_bars import ensure_option_day_bars
from app.data_lake.option_listing import ensure_option_listing
from app.options.contract import OptionContract
from app.options_backtest.data_check import DataKind, DataThing, RecordedFailure
from app.options_backtest.fetching import DownloadFailed, ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import NOW_2026_10_12_MS
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import THU, WED, Day, flat, steady
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import leg, setup
from tests.unit.data_lake.test_option_lake_catalog import clean_artifacts as clean_artifacts
from tests.unit.data_lake.test_option_lake_catalog import pool as pool

THE_FLOOR_DAY = date(2024, 10, 14)


def _scene(*days: date) -> dict[date, Day]:
    return {day: Day(underlying=steady("500.20"), options={("call", 500): flat("1.00", 1, 390)}) for day in days}


async def _recorded(thing: DataThing, day: date) -> RecordedFailure | None:
    return await asyncio.to_thread(LakeCheckData("SPY").recorded_failure, thing, day)


LISTING = DataThing(DataKind.LISTING)


@respx.mock
async def test_nothing_is_recorded_for_a_thing_never_asked_for_or_fetched_whole(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(_scene(WED)).mount()
    assert await _recorded(LISTING, WED) is None

    await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)

    assert await _recorded(LISTING, WED) is None


@respx.mock
async def test_a_listing_that_failed_is_recorded_with_the_lakes_reason_and_asked_again_until_three_tries_are_spent(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(_scene(WED), listing_errors={WED: 503}).mount()

    for asks in (1, 2, 3):
        result = await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)
        assert result.failure is not None and result.failure.reason == "provider_api_error"

        recorded = await _recorded(LISTING, WED)

        assert recorded is not None and recorded.reason == "provider_api_error"
        # One, then two asks leave a try; the third spends the last.
        assert recorded.will_be_asked_again is (asks < 3)


@respx.mock
async def test_a_contract_day_that_failed_is_found_by_the_identity_the_lake_wrote_it_under(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    call = OptionContract.of(root="SPY", expiry_date=WED, right="call", strike=500)
    vendor = Vendor(_scene(WED), option_errors={call.occ_symbol: 500})
    vendor.mount()
    await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)
    failed = await ensure_option_day_bars(call, WED, listed_under="SPY", now_ms=NOW_2026_10_12_MS)
    assert failed.failure is not None

    recorded = await _recorded(DataThing(DataKind.CONTRACTS, call), WED)
    another_day = await _recorded(DataThing(DataKind.CONTRACTS, call), THU)

    assert recorded == RecordedFailure(reason="provider_api_error", will_be_asked_again=True)
    assert another_day is None


@respx.mock
async def test_the_underlyings_failed_day_is_found_by_the_identity_the_stock_capture_wrote_it_under(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(_scene(WED), stock_errors={WED: 500}).mount()
    try:
        await asyncio.to_thread(
            ensure_run_data,
            setup(legs=[leg(expiry={"kind": "same_day"})]),
            [WED],
            now_ms=NOW_2026_10_12_MS,
            should_stop=lambda: False,
        )
    except DownloadFailed:
        pass
    else:
        raise AssertionError("the underlying was meant to fail")

    recorded = await _recorded(DataThing(DataKind.UNDERLYING_BARS), WED)

    assert recorded is not None
    assert recorded.reason == "provider_api_error"


@respx.mock
async def test_a_day_the_plan_does_not_reach_is_asked_again_however_often_it_was_refused(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The floor day is taken, so the vendor is asked, and it says the plan does not include that timeframe. The lake
    # never latches that answer: the plan can change.
    call = OptionContract.of(root="SPY", expiry_date=THE_FLOOR_DAY, right="call", strike=500)
    Vendor(_scene(THE_FLOOR_DAY), outside_plan={call.occ_symbol}).mount()
    await ensure_option_listing("SPY", THE_FLOOR_DAY, now_ms=NOW_2026_10_12_MS)

    for _ in range(5):
        result = await ensure_option_day_bars(call, THE_FLOOR_DAY, listed_under="SPY", now_ms=NOW_2026_10_12_MS)
        assert result.failure is not None and result.failure.reason == "outside_data_plan"

    recorded = await _recorded(DataThing(DataKind.CONTRACTS, call), THE_FLOOR_DAY)

    assert recorded == RecordedFailure(reason="outside_data_plan", will_be_asked_again=True)


# ── On the real catalog table ────────────────────────────────────────────
# The twins of the tests above, against the real SQL. They skip without a database attested as disposable, and the
# catalog table must exist (it is created by a .NET migration; see test_option_lake_catalog.py).


@respx.mock
async def test_on_the_real_table_a_failed_listing_contract_day_and_stock_day_are_each_found_by_their_own_identity(
    clean_artifacts: None, pool: None, tmp_lake: Path
) -> None:
    call = OptionContract.of(root="SPY", expiry_date=WED, right="call", strike=500)
    vendor = Vendor(
        _scene(WED, THU), option_errors={call.occ_symbol: 500}, listing_errors={THU: 503}, stock_errors={THU: 500}
    )
    vendor.mount()
    # Wednesday's listing is stored whole, so its contract can be asked for and can fail; Thursday's listing fails.
    assert (await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)).failure is None
    assert (await ensure_option_day_bars(call, WED, listed_under="SPY", now_ms=NOW_2026_10_12_MS)).failure is not None
    assert (await ensure_option_listing("SPY", THU, now_ms=NOW_2026_10_12_MS)).failure is not None
    with pytest.raises(DownloadFailed):
        await asyncio.to_thread(
            ensure_run_data,
            setup(legs=[leg(expiry={"kind": "same_day"})]),
            [THU],
            now_ms=NOW_2026_10_12_MS,
            should_stop=lambda: False,
        )

    contract_day = await _recorded(DataThing(DataKind.CONTRACTS, call), WED)
    listing_stored = await _recorded(LISTING, WED)
    listing_failed = await _recorded(LISTING, THU)
    stock_failed = await _recorded(DataThing(DataKind.UNDERLYING_BARS), THU)
    stock_never_asked = await _recorded(DataThing(DataKind.UNDERLYING_BARS), WED)

    assert contract_day == RecordedFailure(reason="provider_api_error", will_be_asked_again=True)
    assert listing_stored is None, "a listing fetched whole has no failure"
    assert listing_failed is not None and listing_failed.reason == "provider_api_error"
    assert stock_failed is not None and stock_failed.reason == "provider_api_error"
    assert stock_never_asked is None
