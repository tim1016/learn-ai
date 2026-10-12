"""What a run reads of the data lake, through ``LakeRunData`` (#3030).

A small lake is built in a temporary folder with the lake's own writers behind the fake catalog and read back through
the reader a run uses. Hand-worked figures:

* 2025-06-13 is a full Friday session. It opens at 13:30 UTC = 1749821400000 ms and closes at 20:00 UTC =
  1749844800000 ms. The vendor stamps a bar with its start; the bar that starts at 13:30:00 closes at 13:31:00 =
  1749821460000, the one that starts at 13:31:00 closes at 13:32:00 = 1749821520000, and the 16:00 print (start
  20:00:00 = 1749844800000) closes at 20:01:00 = 1749844860000.
* 2025-11-28 is the half day after Thanksgiving: it closes at 13:00 New York = 18:00 UTC = 1764352800000. The bar
  that starts at 12:59 (17:59 UTC = 1764352740000) is its last and closes at 1764352800000.
* New York midnight of 2025-06-13 is 04:00 UTC = 1749787200000, and the date ends 86 400 000 ms later. A bar starting
  at 23:59 New York is 03:59 UTC on the 14th = 1749787200000 + 86 400 000 - 60 000 = 1749873540000, and closes at
  1749873600000, which is midnight and so the 14th. The bar starting a minute earlier (23:58, 1749873480000) closes at
  23:59 on the 13th, 1749873540000. A bar starting at 10:00:30 (14:00:30 UTC = 1749821400000 + 600 000 + 30 000 =
  1749822030000) closes at 1749822090000, which is 30 000 ms past a whole minute.
* The underlying's last regular-session bars: the one starting at 15:59:00 (19:59 UTC = 1749844740000) closes at
  20:00:00 = 1749844800000, the one starting at 15:59:30 (1749844770000) closes at 1749844830000, off the minute.
* The same last minute of a New York date in winter, 2025-01-15 (a Wednesday, EST = UTC-5): midnight is 05:00 UTC.
  The epoch day of 2025-01-15 is 20103 (2025-01-01 is day 20089, and 14 days on), 20103 * 86 400 000 =
  1736899200000, plus 18 000 000 is midnight, 1736917200000. The 09:30 open is 34 200 000 ms on, 1736951400000; the bar
  starting at 23:59 is 86 340 000 ms on, 1737003540000, and closes at 1737003600000, which is midnight of the 16th; the
  bar starting at 23:58 (1737003480000) closes at 1737003540000, 23:59 on the 15th.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import shutil
import zipfile
import zlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_bars import OptionBar, OptionBarsFileError, OptionDayBars
from app.data_lake.option_listing import OptionListingFileError
from app.engine.data.availability import check_availability
from app.engine.data.lean_format import LeanMinuteDataReader, write_lean_day_zip
from app.options.contract import OptionContract
from app.options_backtest import lake_data as lake_data_module
from app.options_backtest.lake_data import (
    _STOCK_FILE_ERRORS,
    LakeRunData,
    StoredListing,
    UnderlyingDayUnreadable,
)
from app.options_backtest.session_market import bars_by_close
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_vendor import bar_row
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_CLOSE_MS,
    FRIDAY_OPEN_MS,
    MINUTE_MS,
    minute_bar,
    option_bars_path,
    option_day_zip,
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


def _what_the_app_logged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """Every record the app wrote, at any level, as (logger, message); asyncio's own debug lines are not the app's."""
    return [(record.name, record.getMessage()) for record in caplog.records if record.name.startswith("app")]


# ── The underlying ───────────────────────────────────────────────────────


async def test_the_underlying_is_its_regular_session_bars_named_by_their_close_with_the_hash_of_the_file(
    fake_catalog: FakeCatalog, tmp_lake: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    read = await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert read is not None
    assert _what_the_app_logged(caplog) == []  # a day with every bar in place is read in silence
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


@pytest.mark.parametrize(
    ("bars", "held"),
    [
        pytest.param(None, False, id="nothing stored"),
        pytest.param(
            [stock_bar(PRE_MARKET_START_MS, "599.00", "599.10", "598.90", "599.05")],
            False,
            id="only a pre-market bar, no bar in the regular session",
        ),
        pytest.param(_underlying_bars(), True, id="a normal day"),
    ],
)
async def test_the_underlying_is_held_exactly_when_the_lakes_own_coverage_check_says_so(
    bars: list | None, held: bool, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    if bars is not None:
        await store_underlying_day(fake_catalog, tmp_lake, bars)

    report = await asyncio.to_thread(check_availability, [raw_root(tmp_lake)], "SPY", FRIDAY, FRIDAY)
    read = await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert (report.available_days == 1) is held
    assert (read is not None) is held
    assert report.missing_days == ([] if held else [FRIDAY])


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


# What the lake's stock minute file holds in a row: the time in ms after New York midnight, then four prices in ten
# thousandths of a dollar and the volume. 34 200 000 ms is 09:30:00.
_ROW = "34200000,600000,600500,599750,600250,100"
_MEMBER = "20250613_spy_minute_trade.csv"


def _stock_zip(rows: bytes, *, member: str = _MEMBER) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, rows)
    return buffer.getvalue()


def _a_zip_with_no_member() -> bytes:
    buffer = io.BytesIO()
    zipfile.ZipFile(buffer, "w").close()
    return buffer.getvalue()


def _a_deflate_member_that_is_not_a_deflate_stream() -> bytes:
    # A stored member whose method is then changed to deflate in both headers: its seven bytes are a block of the
    # reserved type 3 (the first byte 0b00000111 is "last block" and type 3), which no zlib decodes.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(_MEMBER, b"\x07" * 7)
    zipped = bytearray(buffer.getvalue())
    deflate = (8).to_bytes(2, "little")
    zipped[8:10] = deflate  # the local header's method
    central = zipped.index(b"PK\x01\x02")
    zipped[central + 10 : central + 12] = deflate  # the central directory's method
    return bytes(zipped)


# Each kind of damage a stored stock file can have that the stock reader lets out as a different error type, and the type.
_DAMAGED_STOCK_FILES = [
    pytest.param(b"admitted, but not a zip", zipfile.BadZipFile, id="not a zip"),
    pytest.param(_a_zip_with_no_member(), IndexError, id="a zip with no member"),
    pytest.param(_a_deflate_member_that_is_not_a_deflate_stream(), zlib.error, id="a corrupt compressed stream"),
    pytest.param(
        _stock_zip(b"34200000,abc,600500,599750,600250,100"), ArithmeticError, id="a price that is not a number"
    ),
    pytest.param(_stock_zip(b"later,600000,600500,599750,600250,100"), ValueError, id="a time that is not a number"),
]


@pytest.mark.parametrize(("payload", "cause"), _DAMAGED_STOCK_FILES)
async def test_a_stock_file_damaged_in_each_way_the_reader_lets_out_is_the_one_named_error_with_its_cause(
    payload: bytes, cause: type[Exception], fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_PATH, payload, admit=True)

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert "SPY" in str(caught.value) and "2025-06-13" in str(caught.value)
    assert isinstance(caught.value.__cause__, cause)


async def test_a_directory_where_the_stock_file_should_be_is_the_one_named_error_with_an_os_error_as_its_cause(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    path = await store_underlying_day(fake_catalog, tmp_lake, _underlying_bars())
    path.unlink()
    path.mkdir()

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize(
    "error", [kind for kind in _STOCK_FILE_ERRORS if kind is not LakeAdmissionError], ids=lambda kind: kind.__name__
)
async def test_every_error_the_stock_reader_lets_out_for_a_file_it_cannot_decode_is_the_one_named_error(
    error: type[Exception], fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_decode(self: LeanMinuteDataReader, symbol: str, trading_date: date) -> list:
        raise error("a decode that failed")

    monkeypatch.setattr(LeanMinuteDataReader, "read_day", cannot_decode)

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert type(caught.value.__cause__) is error and "a decode that failed" in str(caught.value)


async def test_the_lakes_own_admission_error_is_the_one_kind_the_stock_reader_lets_out_that_is_not_wrapped(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_admitted(self: LeanMinuteDataReader, symbol: str, trading_date: date) -> list:
        raise LakeAdmissionError("no receipt")

    monkeypatch.setattr(LeanMinuteDataReader, "read_day", not_admitted)

    with pytest.raises(LakeAdmissionError):
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)


def test_the_stock_readers_list_of_errors_is_the_eight_kinds_these_tests_cover() -> None:
    # A kind added to the stock list fails here, to be given a damaged file or a double of its own above.
    assert set(_STOCK_FILE_ERRORS) == {
        LakeAdmissionError,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        IndexError,
        ValueError,
        ArithmeticError,
        OSError,
    }


# The same day stored in the lake's raw tree and in the split-adjusted tree, at other prices. The adjusted file is
# written without the receipt and the adjustment record the lake gives a captured one, so a reader that opened it would
# fail on that and not hand its prices over: either way a test below fails if the adjusted tree is consulted at all.
def _write_the_adjusted_tree(tmp_lake: Path, bars: list) -> Path:
    return write_lean_day_zip(tmp_lake / "lake" / "polygon_split_adjusted", "SPY", FRIDAY, bars)


async def test_the_underlying_comes_from_the_raw_tree_and_never_the_split_adjusted_one(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25")]
    )
    # A 2-for-1 split later, the adjusted tree holds half the prices for the same minute.
    _write_the_adjusted_tree(tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "300.00", "300.25", "299.88", "300.12")])

    read = await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    assert read is not None
    assert [(bar.close_ms, bar.open, bar.close) for bar in read.value] == [
        (FRIDAY_OPEN_MS + MINUTE_MS, D("600.00"), D("600.25"))
    ]


async def test_a_day_only_the_split_adjusted_tree_holds_is_not_captured(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _write_the_adjusted_tree(tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "300.00", "300.25", "299.88", "300.12")])

    assert await _read(LakeRunData("SPY").underlying_day, FRIDAY) is None


# ── A bar no replay can place makes the day unreadable ─────────────────────

WINTER = date(2025, 1, 15)  # a Wednesday and a full session; EST, so New York midnight is 05:00 UTC
WINTER_OPEN_MS = 1736951400000
# (the date, its 09:30 open, the bar starting in the date's last minute, 23:59 New York, and the bar before it, 23:58)
_LAST_MINUTES = [
    pytest.param(FRIDAY, FRIDAY_OPEN_MS, 1749873540000, 1749873480000, id="summer"),
    pytest.param(WINTER, WINTER_OPEN_MS, 1737003540000, 1737003480000, id="winter"),
]


@pytest.mark.parametrize(
    ("offset_ms", "first_start_ms"),
    [
        pytest.param(1, 1749844740001, id="a millisecond past the minute"),
        pytest.param(30_000, 1749844770000, id="half a minute past it"),
    ],
)
async def test_an_underlying_day_with_a_bar_stamped_off_the_minute_is_unreadable_and_hands_nothing_over(
    offset_ms: int, first_start_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # 15:59:00 New York is 1749844740000; the third bar starts offset_ms later and closes off a whole minute.
    await store_underlying_day(
        fake_catalog,
        tmp_lake,
        [
            stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25"),
            stock_bar(LAST_REGULAR_START_MS, "601.10", "601.20", "601.00", "601.15"),
            stock_bar(LAST_REGULAR_START_MS + offset_ms, "601.15", "601.30", "601.10", "601.20"),
        ],
    )

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    message = str(caught.value)
    assert "SPY" in message and "1 of 3" in message and f"{first_start_ms} ms" in message
    assert "not on a whole minute" in message
    assert str(tmp_lake) not in message and ".zip" not in message


async def test_an_underlying_day_with_several_bars_off_the_minute_counts_them_all_and_names_the_first(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # 09:30:30 (1749821430000) and 15:59:00.001 (1749844740001) are off the minute, 09:30:00 and 15:59:00 are not.
    await store_underlying_day(
        fake_catalog,
        tmp_lake,
        [
            stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25"),
            stock_bar(FRIDAY_OPEN_MS + 30_000, "600.25", "600.60", "600.20", "600.40"),
            stock_bar(LAST_REGULAR_START_MS, "601.10", "601.20", "601.00", "601.15"),
            stock_bar(LAST_REGULAR_START_MS + 1, "601.15", "601.30", "601.10", "601.20"),
        ],
    )

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, FRIDAY)

    message = str(caught.value)
    assert "2 of 4" in message and "1749821430000 ms" in message and "1749844740001" not in message


@pytest.mark.parametrize(("day", "open_ms", "last_start_ms", "before_last_start_ms"), _LAST_MINUTES)
async def test_an_underlying_bar_that_closes_on_the_next_new_york_date_makes_the_day_unreadable(
    day: date,
    open_ms: int,
    last_start_ms: int,
    before_last_start_ms: int,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stock reader keeps the regular session only, so a print at 23:59 in a stock file is never read (as the 08:00
    # print above is not) and cannot reach this check. The reader is made to hand one over, which is what the check
    # is for should its filter ever change; the bar starting at 23:59 closes at midnight, which is the next date.
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(open_ms, "600.00", "600.50", "599.75", "600.25")], day=day
    )
    real_read_day = LeanMinuteDataReader.read_day

    def with_the_last_minute_of_the_date(self: LeanMinuteDataReader, symbol: str, trading_date: date) -> list:
        return [*real_read_day(self, symbol, trading_date), stock_bar(last_start_ms, "1.00", "1.00", "1.00", "1.00")]

    monkeypatch.setattr(LeanMinuteDataReader, "read_day", with_the_last_minute_of_the_date)

    with pytest.raises(UnderlyingDayUnreadable) as caught:
        await _read(LakeRunData("SPY").underlying_day, day)

    message = str(caught.value)
    assert "SPY" in message and "1 of 2" in message and f"{last_start_ms} ms" in message
    assert "closes on another New York date" in message


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
    fake_catalog: FakeCatalog, tmp_lake: Path, caplog: pytest.LogCaptureFixture
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
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    read = await _read(LakeRunData("SPY").option_day, CALL_600, FRIDAY)

    assert read is not None
    assert _what_the_app_logged(caplog) == []  # a day with every bar in place is read in silence
    # The vendor's starts were 13:30:00, 13:31:00 and 20:00:00 UTC; each close is a minute later.
    assert [bar.close_ms for bar in read.value] == [1749821460000, 1749821520000, 1749844860000]
    assert [(bar.open, bar.high, bar.low, bar.close) for bar in read.value] == [
        (D("1.27"), D("1.31"), D("1.25"), D("1.27")),
        (D("1.3"), D("1.4"), D("1.28"), D("1.35")),
        (D("0.9"), D("0.95"), D("0.88"), D("0.92")),
    ]
    assert str(read.value[0].open) == "1.27" and str(read.value[0].close) == "1.27"
    assert read.sha256 == hashlib.sha256(the_file_of(raw_root(tmp_lake), BARS_PATH_600)).hexdigest()


async def _an_option_day_holding(starts_ms: list[int], *, day: date = FRIDAY) -> OptionContract:
    """A call whose stored day holds one bar starting at each of these instants, on a minute or not.

    It is captured the way a day is, with the first bar, and the file is then replaced by one holding all the bars
    (the capture refuses a vendor bar that is off the minute, but the lake's reader must still meet a file that has one).
    """
    contract = OptionContract.of(root="SPY", expiry_date=day, right="call", strike=600)
    await store_listing([vendor_listing_row(contract)], day=day)
    await store_option_day(
        contract, [bar_row(0, t=starts_ms[0], o=1.27, h=1.31, l=1.25, c=1.27, v=10, vw=1.27, n=1)], day=day
    )
    return contract


@respx.mock
@pytest.mark.parametrize(
    ("offset_ms", "first_start_ms"),
    [
        pytest.param(1, 1749822060001, id="a millisecond past the minute"),
        pytest.param(30_000, 1749822090000, id="half a minute past it"),
    ],
)
async def test_an_option_day_with_a_bar_stamped_off_the_minute_is_unreadable_and_hands_nothing_over(
    offset_ms: int, first_start_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    contract = await _an_option_day_holding([FRIDAY_OPEN_MS])
    # 09:30 is 1749821400000; 09:40 is ten minutes on, 1749822000000; the third bar starts a minute after that, at
    # 1749822060000, plus the offset, and closes off a whole minute.
    starts = [FRIDAY_OPEN_MS, FRIDAY_OPEN_MS + 10 * MINUTE_MS, FRIDAY_OPEN_MS + 11 * MINUTE_MS + offset_ms]
    replace_the_stored_file(
        fake_catalog, raw_root(tmp_lake), option_bars_path(contract), option_day_zip(contract, starts), admit=True
    )

    # The lake's own parser of a stored option day refuses a start off the minute, naming the first such bar (index 2).
    with pytest.raises(OptionBarsFileError) as caught:
        await _read(LakeRunData("SPY").option_day, contract, FRIDAY)

    message = str(caught.value)
    assert contract.occ_symbol in message and "bar[2]" in message and f"t_ms={first_start_ms}" in message
    assert "does not start on a whole minute" in message
    assert str(tmp_lake) not in message and ".zip" not in message


async def test_the_readers_own_guard_for_the_close_still_counts_option_bars_it_cannot_place_should_the_lake_hand_over_one(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lake's parser refuses a start off the minute (above), so a stored option day cannot reach this check with one;
    # it stays as the reader's own guard for the close, and a double of the lake's read shows it still counts and names.
    # 09:30:30 (1749821430000) is off the minute, 23:59 (1749873540000) closes on the 14th, 09:30 and 09:31 are in place.
    starts = [FRIDAY_OPEN_MS, FRIDAY_OPEN_MS + 30_000, FRIDAY_OPEN_MS + MINUTE_MS, 1749873540000]
    bars = tuple(
        OptionBar(start_ms=start_ms, open=D("1.27"), high=D("1.31"), low=D("1.25"), close=D("1.27"), volume=D(10))
        for start_ms in starts
    )
    monkeypatch.setattr(
        lake_data_module,
        "read_option_day_bars",
        lambda contract, day, **kwargs: OptionDayBars(
            contract_symbol=contract.occ_symbol, trading_date=day, bars=bars, sha256="0" * 64
        ),
    )

    with pytest.raises(OptionBarsFileError) as caught:
        await _read(LakeRunData("SPY").option_day, CALL_600, FRIDAY)

    message = str(caught.value)
    assert "2 of 4" in message and "1749821430000 ms" in message and "1749873540000" not in message
    assert "not on a whole minute" in message


@respx.mock
@pytest.mark.parametrize(("day", "open_ms", "last_start_ms", "before_last_start_ms"), _LAST_MINUTES)
async def test_an_option_bar_that_closes_on_the_next_new_york_date_makes_the_day_unreadable(
    day: date, open_ms: int, last_start_ms: int, before_last_start_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    contract = await _an_option_day_holding([open_ms], day=day)
    replace_the_stored_file(
        fake_catalog,
        raw_root(tmp_lake),
        option_bars_path(contract, day),
        option_day_zip(contract, [open_ms, last_start_ms], day=day),
        admit=True,
    )

    with pytest.raises(OptionBarsFileError) as caught:
        await _read(LakeRunData("SPY").option_day, contract, day)

    message = str(caught.value)
    assert contract.occ_symbol in message and "1 of 2" in message and f"{last_start_ms} ms" in message
    assert "closes on another New York date" in message


@respx.mock
@pytest.mark.parametrize(("day", "open_ms", "last_start_ms", "before_last_start_ms"), _LAST_MINUTES)
async def test_an_option_bar_that_closes_a_minute_before_new_york_midnight_is_in_place_and_the_day_is_read(
    day: date, open_ms: int, last_start_ms: int, before_last_start_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    contract = await _an_option_day_holding([open_ms], day=day)
    replace_the_stored_file(
        fake_catalog,
        raw_root(tmp_lake),
        option_bars_path(contract, day),
        option_day_zip(contract, [open_ms, before_last_start_ms], day=day),
        admit=True,
    )

    read = await _read(LakeRunData("SPY").option_day, contract, day)

    assert read is not None
    # Each closes a minute after it starts: 09:31, and 23:59 on the date itself (a minute before its midnight).
    assert [bar.close_ms for bar in read.value] == [open_ms + MINUTE_MS, before_last_start_ms + MINUTE_MS]
    # and the reader of a session accepts what is handed over, whole
    assert len(bars_by_close(read.value, trading_date=day, what=contract.occ_symbol)) == 2


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
