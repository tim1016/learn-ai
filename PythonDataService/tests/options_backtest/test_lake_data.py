"""What a run reads of the data lake, through ``LakeRunData`` (#3030).

A small lake is built in a temporary folder with the lake's own writers behind the fake catalog and read back through
the reader a run uses. Hand-worked figures:

* 2025-06-13 is a full Friday session. It opens at 13:30 UTC = 1749821400000 ms and closes at 20:00 UTC =
  1749844800000 ms. The vendor stamps a bar with its start; the bar that starts at 13:30:00 closes at 13:31:00 =
  1749821460000, the one that starts at 13:31:00 closes at 13:32:00 = 1749821520000, and the 16:00 print (start
  20:00:00 = 1749844800000) closes at 20:01:00 = 1749844860000.
* 2025-11-28 is the half day after Thanksgiving: it closes at 13:00 New York = 18:00 UTC = 1764352800000. The bar
  that starts at 12:59 (17:59 UTC = 1764352740000) is its last and closes at 1764352800000.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_listing import OptionListingFileError
from app.engine.data.availability import check_availability
from app.options.contract import OptionContract
from app.options_backtest.lake_data import LakeRunData, StoredListing, UnderlyingDayUnreadable
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_CLOSE_MS,
    FRIDAY_OPEN_MS,
    MINUTE_MS,
    minute_bar,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    the_file_of,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

D = Decimal
ET = ZoneInfo("America/New_York")

CALL_600 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=600)
CALL_601 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=601)
CALL_602 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=602)
CALL_603 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=603)
CALL_604 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=604)
PUT_600 = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=600)

STOCK_PATH = "equity/usa/minute/spy/20250613_trade.zip"
LISTING_PATH = "option/usa/listing/spy/20250613_contracts.zip"
BARS_PATH_600 = "option/usa/minute/spy/spy250613c00600000/20250613_trade.zip"

# The underlying's stored day: a pre-market print, the first two minutes of the session, the last minute of it, and
# the 16:00 print, which starts at the close. Only the three in between are in the regular session.
PRE_MARKET_START_MS = 1749816000000  # 08:00 New York
LAST_REGULAR_START_MS = 1749844740000  # 15:59 New York


def _underlying_bars():
    return [
        stock_bar(PRE_MARKET_START_MS, "599.00", "599.10", "598.90", "599.05"),
        stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25"),
        stock_bar(FRIDAY_OPEN_MS + MINUTE_MS, "600.25", "601.00", "600.25", "600.75"),
        stock_bar(LAST_REGULAR_START_MS, "601.10", "601.20", "601.00", "601.15"),
        stock_bar(FRIDAY_CLOSE_MS, "601.20", "601.30", "601.10", "601.25"),
    ]


def _the_option_rows():
    """Six stored rows: four that can be listed contracts and two that cannot."""
    return [
        vendor_listing_row(CALL_600),
        vendor_listing_row(CALL_601),
        # Named, but ten shares: a contract that is not standard.
        vendor_listing_row(CALL_602, shares_per_contract=10),
        vendor_listing_row(PUT_600),
        # An adjusted root (it carries a digit): no contract can be named from this ticker.
        vendor_listing_row(CALL_603, ticker="O:SPY1250613C00603000"),
        # The ticker names a contract, but the vendor sent no share count.
        vendor_listing_row(CALL_604, shares_per_contract=None),
    ]


async def _read(method, *args):
    # The readers block and need a thread with no event loop, as a run's worker thread is.
    return await asyncio.to_thread(method, *args)


# ── The underlying ───────────────────────────────────────────────────────


async def test_the_underlying_is_its_regular_session_bars_named_by_their_close_with_the_hash_of_the_file(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    path = await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())

    read = await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert read is not None
    # 09:30 -> 13:31:00, 09:31 -> 13:32:00, and 15:59 -> 20:00:00, the close. The 08:00 print is before the session and
    # the 16:00 print starts at the close: neither is a regular-session bar.
    assert [bar.close_ms for bar in read.value] == [1749821460000, 1749821520000, 1749844800000]
    assert [(bar.open, bar.high, bar.low, bar.close) for bar in read.value] == [
        (D("600.00"), D("600.50"), D("599.75"), D("600.25")),
        (D("600.25"), D("601.00"), D("600.25"), D("600.75")),
        (D("601.10"), D("601.20"), D("601.00"), D("601.15")),
    ]
    assert str(read.value[0].open) == "600" and str(read.value[0].close) == "600.25"  # the digits, not a float's
    assert read.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


async def test_a_half_day_keeps_its_last_minute_and_drops_the_one_after_the_early_close(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    half_day = date(2025, 11, 28)
    await store_underlying_day(
        fake_catalog,
        tmp_lake,
        [
            stock_bar(1764340200000, "600.00", "600.50", "599.75", "600.25"),  # 09:30
            stock_bar(1764352740000, "601.10", "601.20", "601.00", "601.15"),  # 12:59
            stock_bar(1764352800000, "601.20", "601.30", "601.10", "601.25"),  # 13:00, after the early close
        ],
        day=half_day,
    )

    read = await _read(LakeRunData("SPY").underlying_day, half_day)

    assert read is not None
    assert [bar.close_ms for bar in read.value] == [1764340260000, 1764352800000]


async def test_an_underlying_day_the_lake_does_not_hold_is_none(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    assert await _read(LakeRunData("SPY").underlying_day, FRIDAY) is None


async def test_an_underlying_file_with_no_bar_in_the_regular_session_is_not_held(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(PRE_MARKET_START_MS, "599.00", "599.10", "598.90", "599.05")]
    )

    assert await _read(LakeRunData("SPY").underlying_day, FRIDAY) is None


@pytest.mark.parametrize("held", [False, True], ids=["nothing stored", "a normal day"])
async def test_the_underlying_is_held_exactly_when_the_lakes_own_coverage_check_says_so(
    held: bool, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    if held:
        await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())

    report = await asyncio.to_thread(check_availability, [raw_root(tmp_lake)], "SPY", FRIDAY, FRIDAY)
    read = await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert (report.available_days == 1) is held
    assert (read is not None) is held


async def test_the_same_holds_for_a_file_with_only_pre_market_bars(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(PRE_MARKET_START_MS, "599.00", "599.10", "598.90", "599.05")]
    )

    report = await asyncio.to_thread(check_availability, [raw_root(tmp_lake)], "SPY", FRIDAY, FRIDAY)

    assert report.available_days == 0 and report.missing_days == [FRIDAY]
    assert await _read(LakeRunData("SPY").underlying_day, FRIDAY) is None


async def test_a_damaged_underlying_file_is_one_named_error_and_not_none(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_PATH, b"admitted, but not a zip", admit=True)

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert "SPY" in str(caught.value) and "2025-06-13" in str(caught.value)
    assert caught.value.__cause__ is not None


async def test_underlying_bytes_the_catalog_does_not_admit_are_refused_and_not_read_as_missing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_PATH, b"changed after its receipt", admit=False)

    with pytest.raises(LakeAdmissionError):
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)


# ── The listing ──────────────────────────────────────────────────────────


@respx.mock
async def test_a_stored_listing_is_the_pickers_listing_and_a_row_that_cannot_be_a_contract_is_left_out_and_counted(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())

    read = await _read(LakeRunData("SPY").listing, FRIDAY)

    assert read is not None
    listing = read.value
    assert isinstance(listing, StoredListing)
    assert (listing.underlying, listing.as_of, listing.complete) == ("SPY", FRIDAY, True)
    # The file sorts its rows by the vendor's ticker: O:SPY1... first, then the calls by strike, then the put. The
    # adjusted-root row (603) and the row with no share count (604) are the two that are left out.
    assert [(c.contract.occ_symbol, c.multiplier, c.standard, c.is_pickable) for c in listing.contracts] == [
        ("SPY250613C00600000", 100, True, True),
        ("SPY250613C00601000", 100, True, True),
        ("SPY250613C00602000", 10, False, False),
        ("SPY250613P00600000", 100, True, True),
    ]
    assert listing.rows_left_out == 2
    assert read.sha256 == hashlib.sha256(the_file_of(raw_root(tmp_lake), LISTING_PATH)).hexdigest()


async def test_a_listing_the_lake_does_not_hold_is_none(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    assert await _read(LakeRunData("SPY").listing, FRIDAY) is None


@respx.mock
async def test_a_listing_none_of_whose_rows_can_be_a_contract_is_captured_and_empty_not_none(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing([vendor_listing_row(CALL_603, ticker="O:SPY1250613C00603000")])

    read = await _read(LakeRunData("SPY").listing, FRIDAY)

    assert read is not None
    assert read.value.contracts == () and read.value.complete is True
    assert isinstance(read.value, StoredListing) and read.value.rows_left_out == 1


@respx.mock
async def test_a_damaged_listing_is_the_lakes_named_error_and_not_none(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), LISTING_PATH, b"admitted, but not a zip", admit=True)

    with pytest.raises(OptionListingFileError):
        await _read(LakeRunData("SPY").listing, FRIDAY)


@respx.mock
async def test_listing_bytes_the_catalog_does_not_admit_are_refused_and_not_read_as_missing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), LISTING_PATH, b"changed after its receipt", admit=False)

    with pytest.raises(LakeAdmissionError):
        await _read(LakeRunData("SPY").listing, FRIDAY)


# ── An option's day ──────────────────────────────────────────────────────


@respx.mock
async def test_an_option_day_is_every_stored_bar_named_by_its_close_one_minute_after_its_start_with_exact_prices(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    await store_option_day(
        CALL_600,
        [
            minute_bar(0, 1.27, 1.31, 1.25, 1.27),
            minute_bar(1, 1.30, 1.40, 1.28, 1.35),
            minute_bar(390, 0.90, 0.95, 0.88, 0.92),  # the 16:00 print: stored, handed over, outside the session
        ],
    )

    read = await _read(LakeRunData("SPY").option_day, CALL_600, FRIDAY)

    assert read is not None
    # The vendor's starts were 13:30:00, 13:31:00 and 20:00:00 UTC; each close is a minute later.
    assert [bar.close_ms for bar in read.value] == [1749821460000, 1749821520000, 1749844860000]
    assert [(bar.open, bar.high, bar.low, bar.close) for bar in read.value] == [
        (D("1.27"), D("1.31"), D("1.25"), D("1.27")),
        (D("1.3"), D("1.4"), D("1.28"), D("1.35")),
        (D("0.9"), D("0.95"), D("0.88"), D("0.92")),
    ]
    assert str(read.value[0].open) == "1.27" and str(read.value[0].close) == "1.27"
    assert read.sha256 == hashlib.sha256(the_file_of(raw_root(tmp_lake), BARS_PATH_600)).hexdigest()


@respx.mock
async def test_an_option_day_not_captured_is_none_and_one_captured_with_no_trade_is_empty_with_its_own_hash(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    await store_option_day(CALL_601, [])  # listed, and the vendor says nothing traded

    never_asked = await _read(LakeRunData("SPY").option_day, CALL_602, FRIDAY)
    quiet = await _read(LakeRunData("SPY").option_day, CALL_601, FRIDAY)

    assert never_asked is None
    assert quiet is not None and tuple(quiet.value) == ()
    quiet_file = "option/usa/minute/spy/spy250613c00601000/20250613_trade.zip"
    assert quiet.sha256 == hashlib.sha256(the_file_of(raw_root(tmp_lake), quiet_file)).hexdigest()


@respx.mock
async def test_a_damaged_option_day_is_the_lakes_named_error_and_not_none_or_empty(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    await store_option_day(CALL_600, [minute_bar(0, 1.27, 1.31, 1.25, 1.27)])
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), BARS_PATH_600, b"admitted, but not a zip", admit=True)

    with pytest.raises(OptionBarsFileError):
        await _read(LakeRunData("SPY").option_day, CALL_600, FRIDAY)


@respx.mock
async def test_option_bytes_the_catalog_does_not_admit_are_refused_and_not_read_as_missing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    await store_option_day(CALL_600, [minute_bar(0, 1.27, 1.31, 1.25, 1.27)])
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), BARS_PATH_600, b"changed after its receipt", admit=False)

    with pytest.raises(LakeAdmissionError):
        await _read(LakeRunData("SPY").option_day, CALL_600, FRIDAY)


# ── All three ────────────────────────────────────────────────────────────


@respx.mock
async def test_an_unreachable_catalog_is_an_error_for_every_kind_and_never_a_missing_day(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    await store_listing(_the_option_rows())
    await store_option_day(CALL_600, [minute_bar(0, 1.27, 1.31, 1.25, 1.27)])

    async def down(*args: object, **kwargs: object) -> bool:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)
    data = LakeRunData("SPY")

    with pytest.raises(catalog_client.CatalogUnavailableError):
        await _read(data.underlying_day, FRIDAY)
    with pytest.raises(catalog_client.CatalogUnavailableError):
        await _read(data.listing, FRIDAY)
    with pytest.raises(catalog_client.CatalogUnavailableError):
        await _read(data.option_day, CALL_600, FRIDAY)


async def test_every_read_refuses_the_event_loop_thread_even_for_a_day_with_no_file(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    data = LakeRunData("SPY")

    with pytest.raises(RuntimeError, match="worker thread"):
        data.underlying_day(FRIDAY)
    with pytest.raises(RuntimeError, match="worker thread"):
        data.listing(FRIDAY)
    with pytest.raises(RuntimeError, match="worker thread"):
        data.option_day(CALL_600, FRIDAY)


@pytest.mark.parametrize("underlying", ["spy", "Spy", "", "SPY/QQQ", "1SPY", "../SPY"])
def test_an_underlying_the_lake_cannot_hold_is_refused_at_once(underlying: str) -> None:
    with pytest.raises(ValueError, match="upper-case ticker"):
        LakeRunData(underlying)


@respx.mock
async def test_nothing_is_kept_between_asks_so_a_replaced_file_reads_as_what_it_now_is(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_listing(_the_option_rows())
    await store_option_day(CALL_600, [minute_bar(0, 1.27, 1.31, 1.25, 1.27)])
    data = LakeRunData("SPY")
    before = await _read(data.option_day, CALL_600, FRIDAY)
    # Other bytes for the same day: one bar at 09:30 with a close of 2.05, given the receipt that admits them.
    replacement = build_minute_trade_zip_bytes(
        symbol=CALL_600.occ_symbol,
        trading_date_yyyymmdd="20250613",
        bars=[
            MinuteTradeBar(
                bar_start_et=datetime(2025, 6, 13, 9, 30, tzinfo=ET),
                open=D("2.00"),
                high=D("2.10"),
                low=D("1.90"),
                close=D("2.05"),
                volume=D(5),
            )
        ],
    )
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), BARS_PATH_600, replacement, admit=True)

    after = await _read(data.option_day, CALL_600, FRIDAY)

    assert before is not None and after is not None
    assert before.value[0].close == D("1.27") and after.value[0].close == D("2.05")
    assert after.sha256 == hashlib.sha256(replacement).hexdigest() != before.sha256


@respx.mock
async def test_an_alternate_mount_of_the_lake_is_read_for_all_three_kinds_and_the_configured_root_is_not(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path
) -> None:
    await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    await store_listing(_the_option_rows())
    await store_option_day(CALL_600, [minute_bar(0, 1.27, 1.31, 1.25, 1.27)])
    # The same lake mounted elsewhere; the configured root then holds nothing.
    mount = tmp_path / "mount"
    shutil.copytree(tmp_lake / "lake", mount / "lake")
    shutil.rmtree(raw_root(tmp_lake))
    raw_root(tmp_lake).mkdir(parents=True)

    on_the_mount = LakeRunData("SPY", lake_root=mount / "lake" / "raw")
    configured = LakeRunData("SPY")

    assert (await _read(on_the_mount.underlying_day, FRIDAY)) is not None
    assert (await _read(on_the_mount.listing, FRIDAY)) is not None
    assert (await _read(on_the_mount.option_day, CALL_600, FRIDAY)) is not None
    assert await _read(configured.underlying_day, FRIDAY) is None
    assert await _read(configured.listing, FRIDAY) is None
    assert await _read(configured.option_day, CALL_600, FRIDAY) is None


async def test_a_root_that_is_not_a_raw_lake_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="raw root"):
        await _read(LakeRunData("SPY", lake_root=tmp_path).underlying_day, FRIDAY)
