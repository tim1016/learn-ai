"""The raw stored listing a run reads once and hands to a capture, beside the picker's listing made of it (#3030)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import respx

from app.options.contract import OptionContract
from app.options_backtest.lake_data import LakeRunData, captured_listing_of
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import FRIDAY, store_listing, vendor_listing_row
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

CALL_600 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=600)
PUT_600 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=600)


@respx.mock
async def test_the_listing_a_run_hands_to_a_capture_and_the_one_it_picks_from_are_the_same_read(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing([vendor_listing_row(CALL_600), vendor_listing_row(PUT_600)])
    reader = LakeRunData("SPY")

    stored = await asyncio.to_thread(reader.stored_listing, FRIDAY)
    picked_from = await asyncio.to_thread(reader.listing, FRIDAY)

    assert stored is not None and picked_from is not None
    assert {row.ticker for row in stored.rows} == {f"O:{CALL_600.occ_symbol}", f"O:{PUT_600.occ_symbol}"}
    assert captured_listing_of(stored) == picked_from
    assert picked_from.sha256 == stored.sha256


@respx.mock
async def test_a_date_with_no_stored_listing_is_none(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    assert await asyncio.to_thread(LakeRunData("SPY").stored_listing, FRIDAY) is None


async def test_the_raw_listing_is_a_blocking_read_and_refuses_the_event_loop() -> None:
    with pytest.raises(RuntimeError, match="worker thread"):
        LakeRunData("SPY").stored_listing(FRIDAY)
