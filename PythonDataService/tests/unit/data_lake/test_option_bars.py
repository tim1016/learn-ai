"""One option contract's minute bars for one session, in the data lake (#3030).

Drives the real capture and read functions against the in-memory catalog
(``FakeCatalog``) and a fake vendor (``respx``). The Postgres-backed twin of
the catalog claims is ``test_option_lake_catalog.py``.

Hand-worked figures used below:

* 2025-06-13 is a full Friday session. Its open, 09:30 EDT, is 13:30 UTC =
  1749821400000 ms, and its close, 16:00 EDT, is 20:00 UTC = 1749844800000 ms.
* A session's option data may be captured from New York midnight after its date: for 2025-06-13 that
  is 00:00 EDT on the 14th = 04:00 UTC = 1749873600000 ms (the close plus 8 hours).
* 09:30 is 34 200 000 ms after midnight, New York time, which is what the zip
  stores as a bar's start. A premium of 1.27 is stored as 12700 ten-thousandths.
* 2025-11-28 (the day after Thanksgiving) closes at 13:00 EST = 18:00 UTC =
  1764352800000 ms.
* Fixed "now" 2026-10-12 14:00 EDT = 1791828000000 ms. Two years back is
  2024-10-12; the lake composes two days inside that (as it does for stocks),
  so the oldest date asked for is 2024-10-14. Probed on the owner's key the
  same day: 2024-10-11 answered NOT_AUTHORIZED, 2024-10-14 answered with bars.

The vendor is faked with the answers probed on the wire (``tests/_helpers/option_vendor.py``).
A day is captured only for a contract the stored listing of its date holds, so each
capture here first stores that listing the way a real one is stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import shutil
import zipfile
import zlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake import option_bars as option_bars_module
from app.data_lake import option_lake as option_lake_module
from app.data_lake import option_listing as option_listing_module
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.ensure_data import _minute_trade_dch
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes, from_deci_cent, to_deci_cent
from app.data_lake.option_bars import (
    OptionBar,
    OptionBarsFileError,
    OptionDayBars,
    captured_option_days,
    ensure_option_day_bars,
    option_minute_trade_dch,
    parse_option_day_zip,
    read_option_day_bars,
)
from app.data_lake.option_lake import (
    MAX_FAILURE_TEXT_CHARS,
    HeldLease,
    OptionDataFailure,
    fail_row,
    option_history_floor,
    publish_option_artifact,
    storable_failure_text,
)
from app.data_lake.option_listing import ensure_option_listing, read_option_listing
from app.data_lake.path_policy import lake_subpath
from app.data_lake.polygon_option_bars import PolygonOptionBar
from app.data_lake.types import ArtifactIdentity
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, point_lake_writer_at_tmp
from tests._helpers.option_lake_cases import (
    CAPTURE_OPENS,
    HALF_DAY_CLOSE_MS,
    complete_row_of_another_provider,
    one_bar_file,
    pieces_of_the_key_in,
    publish_stale_writer_first_once_both_have_staged,
    refuse_what_postgres_refuses,
)
from tests._helpers.option_vendor import (
    bad_bars_answers,
    bar_row,
    no_trades,
    outside_plan,
    raw_json,
    traded,
)

TRADING_DAY = date(2025, 6, 13)
SESSION_OPEN_MS = 1749821400000
SESSION_CLOSE_MS = 1749844800000
MINUTE_MS = 60_000
NOW_2026_10_12_MS = 1791828000000
CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"
SENTINEL_KEY = "sentinel-key-4f9a1c"

CONTRACT = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=600)
SYMBOL = "SPY250613C00600000"
RELATIVE_PATH = "option/usa/minute/spy/spy250613c00600000/20250613_trade.zip"


@pytest.fixture(autouse=True)
def _the_clock_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every capture without its own ``now_ms`` runs on 2026-10-12, the day the plan's floor was probed.

    The plan floor moves forward with the calendar, so a test that asked for 2025-06-13 against the
    real clock would be refused as outside the plan from 2027-06-12 on.
    """
    for module in (option_lake_module, option_bars_module, option_listing_module):
        monkeypatch.setattr(module, "current_ms", lambda: NOW_2026_10_12_MS)


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def _bar(index: int = 0, **overrides: Any) -> dict[str, Any]:
    return bar_row(index, **({"v": 12, "vw": 1.27, "o": 1.27, "h": 1.31, "l": 1.25, "c": 1.27, "n": 3} | overrides))


def _ok(results: list[dict[str, Any]], symbol: str = SYMBOL) -> httpx.Response:
    return traded(symbol, results)


def _mock_vendor(*responses: httpx.Response, contract: str = SYMBOL) -> respx.Route:
    """One answer for every ask, or the answers in turn when several are given."""
    route = respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/O:{contract}/range/1/minute/.*")
    if len(responses) > 1:
        return route.mock(side_effect=list(responses))
    return route.mock(return_value=responses[0] if responses else _ok([_bar(0)], contract))


def _mock_vendor_with(side_effect: Any, contract: str = SYMBOL) -> respx.Route:
    """Answer every ask for the contract by calling ``side_effect(request)``."""
    return respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/O:{contract}/range/1/minute/.*").mock(
        side_effect=side_effect
    )


def _listing_row(contract: OptionContract, underlying: str = "SPY") -> dict[str, Any]:
    """A listed contract exactly as the vendor sends it: nine keys, and no ``additional_underlyings``."""
    strike = contract.strike
    return {
        "cfi": "OCASPS" if contract.right == "call" else "OPASPS",
        "contract_type": contract.right,
        "exercise_style": "american",
        "expiration_date": contract.expiry_date.isoformat(),
        "primary_exchange": "BATO",
        "shares_per_contract": 100,
        "strike_price": int(strike) if strike == int(strike) else float(strike),
        "ticker": f"O:{contract.occ_symbol}",
        "underlying_ticker": underlying,
    }


async def _list(
    *contracts: OptionContract, day: date = TRADING_DAY, underlying: str = "SPY", now_ms: int | None = None
) -> None:
    """Store the listing of ``underlying`` for ``day`` holding these contracts, as a real capture does."""
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            200, json={"request_id": "r", "status": "OK", "results": [_listing_row(c, underlying) for c in contracts]}
        )
    )
    listed = await ensure_option_listing(underlying, day, now_ms=now_ms)
    assert listed.failure is None


async def _ensure(contract: OptionContract = CONTRACT, day: date = TRADING_DAY, **kwargs: Any):
    """Capture a contract's session after its date's listing is stored (a listing already stored is reused)."""
    await _list(contract, day=day, now_ms=kwargs.get("now_ms"))
    return await ensure_option_day_bars(contract, day, listed_under="SPY", **kwargs)


def _bar_rows(fake_catalog: FakeCatalog) -> list[dict[str, Any]]:
    """The catalog's rows for option bars (the listing the capture rests on has a row of its own)."""
    return [row for row in fake_catalog.rows.values() if row["artifact_kind"] == "time_series_bars"]


def _raw_root(tmp_lake: Path) -> Path:
    return tmp_lake / lake_subpath("raw")


def _lake_files(tmp_lake: Path) -> list[Path]:
    """The option-bars files on the lake (the stored listing's file is not one)."""
    root = _raw_root(tmp_lake)
    return [p for p in root.rglob("*.zip") if "minute" in p.parts] if root.exists() else []


def _staged_files(tmp_lake: Path) -> list[Path]:
    return [p for p in (tmp_lake / "staging").rglob("*") if p.is_file()]


async def _read(contract: OptionContract = CONTRACT, day: date = TRADING_DAY) -> OptionDayBars | None:
    # Admission asks the catalog from a worker thread, as the research workers do.
    return await asyncio.to_thread(read_option_day_bars, contract, day)


def _zip_rows(payload: bytes) -> tuple[list[str], list[str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return zf.namelist(), zf.read(zf.namelist()[0]).decode().splitlines()


# ── The happy path ─────────────────────────────────────────────────────


@respx.mock
async def test_a_traded_session_is_captured_into_the_lake_and_the_catalog(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor(_ok([_bar(0), _bar(1, o=1.30, h=1.40, l=1.28, c=1.35, v=40)]))

    result = await _ensure()

    assert result.failure is None and result.reused is False
    record = result.record
    assert record is not None
    # The catalog row: the stock key with the option market name and the contract's own symbol.
    assert (record.artifact_kind, record.market, record.symbol, record.trading_date) == (
        "time_series_bars", "usa_options", SYMBOL, TRADING_DAY,
    )
    assert (record.resolution, record.data_type, record.provider, record.price_adjustment_mode) == (
        "minute", "trade", "polygon", "raw",
    )
    assert record.file_path == RELATIVE_PATH
    assert record.row_count == 2
    assert (record.first_bar_start_ms, record.last_bar_start_ms) == (SESSION_OPEN_MS, SESSION_OPEN_MS + MINUTE_MS)
    assert record.data_contract_hash == option_minute_trade_dch()
    row = fake_catalog.rows[record.id]
    assert row["status"] == "complete"
    # The lake holds exactly that file, and its bytes are the receipt's.
    [file] = _lake_files(tmp_lake)
    assert file == _raw_root(tmp_lake) / RELATIVE_PATH
    assert hashlib.sha256(file.read_bytes()).hexdigest() == record.file_sha256
    assert len(file.read_bytes()) == record.file_size_bytes
    assert _staged_files(tmp_lake) == []


@respx.mock
async def test_the_file_is_the_stock_minute_zip_with_start_times_and_ten_thousandths(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(_ok([_bar(0), _bar(1, o=1.30, h=1.40, l=1.28, c=1.35, v=40)]))

    result = await _ensure()

    assert result.record is not None
    names, rows = _zip_rows((_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes())
    assert names == ["20250613_spy250613c00600000_minute_trade.csv"]
    # 34 200 000 ms = 09:30 New York, the bar's start; 1.27 -> 12700; 1.31 -> 13100; volume as given.
    assert rows == ["34200000,12700,13100,12500,12700,12", "34260000,13000,14000,12800,13500,40"]


@respx.mock
async def test_the_vendor_is_asked_for_the_o_prefixed_ticker_raw_and_the_catalog_never_sees_the_prefix(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor()

    await _ensure()

    asked = route.calls[0].request
    assert f"/ticker/O:{SYMBOL}/range/1/minute/2025-06-13/2025-06-13" in str(asked.url)
    assert asked.url.params["adjusted"] == "false"
    assert {row["symbol"] for row in _bar_rows(fake_catalog)} == {SYMBOL}


@respx.mock
async def test_a_premium_of_1_27_reads_back_as_the_exact_decimal_1_27(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    # 0.29, 1.1 and 4.35 are prices whose float neighbours are not the decimal the vendor wrote.
    _mock_vendor(_ok([_bar(0, o=1.27, h=1.27, l=1.27, c=1.27), _bar(1, o=0.29, h=0.29, l=0.29, c=0.29, v=1), _bar(2, o=1.1, h=4.35, l=1.1, c=4.35, v=5)]))
    await _ensure()

    day = await _read()

    assert day is not None
    first, second, third = day.bars
    assert (first.open, first.high, first.low, first.close) == (Decimal("1.27"),) * 4
    assert (second.open, second.close) == (Decimal("0.29"), Decimal("0.29"))
    assert (third.open, third.high) == (Decimal("1.1"), Decimal("4.35"))
    for bar in day.bars:
        for price in (bar.open, bar.high, bar.low, bar.close):
            assert type(price) is Decimal
        assert type(bar.volume) is Decimal
    assert str(first.open) == "1.27"


@respx.mock
async def test_a_bar_is_read_back_by_its_start_in_epoch_milliseconds(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor(_ok([_bar(0), _bar(1), _bar(5)]))
    await _ensure()

    day = await _read()

    assert day is not None
    assert [bar.start_ms for bar in day.bars] == [SESSION_OPEN_MS, SESSION_OPEN_MS + MINUTE_MS, SESSION_OPEN_MS + 5 * MINUTE_MS]
    assert day.bars[0] == OptionBar(
        start_ms=SESSION_OPEN_MS, open=Decimal("1.27"), high=Decimal("1.31"), low=Decimal("1.25"), close=Decimal("1.27"),
        volume=Decimal("12"),
    )


@respx.mock
async def test_the_read_gives_back_the_files_sha256_for_pinning(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor()
    result = await _ensure()

    day = await _read()

    assert day is not None and result.record is not None
    assert day.sha256 == result.record.file_sha256 == hashlib.sha256((_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes()).hexdigest()
    assert (day.contract_symbol, day.trading_date) == (SYMBOL, TRADING_DAY)


@respx.mock
async def test_spy_options_print_until_16_15_and_what_the_vendor_sent_is_kept(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """The calendar closes at 16:00; a 16:14 print is still that date's data (trimming is the caller's)."""
    after_close = SESSION_CLOSE_MS + 14 * MINUTE_MS
    _mock_vendor(_ok([_bar(0), _bar(t=after_close)]))

    await _ensure()

    day = await _read()
    assert day is not None
    assert day.bars[-1].start_ms == after_close


#: Midnight New York as epoch ms UTC, worked by hand as UTC midnight plus that date's offset:
#: 2025-01-15 is EST (UTC-5, 05:00 UTC); 2025-03-10 is the Monday after clocks went forward on Sunday 03-09 (EDT, UTC-4);
#: 2025-06-13 is EDT; 2025-11-03 is the Monday after clocks went back on Sunday 11-02 (EST).
_MIDNIGHT_NEW_YORK_MS = {
    date(2025, 1, 15): 1736917200000,
    date(2025, 3, 10): 1741579200000,
    date(2025, 6, 13): 1749787200000,
    date(2025, 11, 3): 1762146000000,
}
#: Midnight, before 04:00, before the open, the open, the close, a print after the close, the last minute the
#: underlying trades, and the last minute of the date.
_EVERY_HOUR = [(0, 0), (3, 59), (4, 0), (9, 29), (9, 30), (16, 0), (16, 14), (19, 59), (23, 59)]
#: What the file stores for those: the wall clock as milliseconds after midnight, the same on every date.
_EVERY_HOUR_STORED = [0, 14_340_000, 14_400_000, 34_140_000, 34_200_000, 57_600_000, 58_440_000, 71_940_000, 86_340_000]


@respx.mock
@pytest.mark.parametrize("day", list(_MIDNIGHT_NEW_YORK_MS), ids=["winter", "after spring forward", "summer", "after fall back"])
async def test_every_bar_the_vendor_sent_for_the_date_comes_back_at_the_vendors_own_stamp_whatever_the_hour(
    day: date, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Trimming to a session is the caller's business: the lake keeps and returns whatever printed on the date."""
    contract = OptionContract.of(root="SPY", expiry_date=day, right="call", strike=600)
    stamps = [_MIDNIGHT_NEW_YORK_MS[day] + (hour * 60 + minute) * MINUTE_MS for hour, minute in _EVERY_HOUR]
    _mock_vendor(_ok([_bar(t=t) for t in stamps], contract.occ_symbol), contract=contract.occ_symbol)

    result = await _ensure(contract, day)
    read = await _read(contract, day)

    assert result.failure is None and result.record is not None
    assert (result.record.row_count, result.record.first_bar_start_ms, result.record.last_bar_start_ms) == (
        len(stamps), stamps[0], stamps[-1],
    )
    _, rows = _zip_rows((_raw_root(tmp_lake) / result.record.file_path).read_bytes())
    assert [int(row.split(",")[0]) for row in rows] == _EVERY_HOUR_STORED
    assert read is not None
    assert [bar.start_ms for bar in read.bars] == stamps


@respx.mock
async def test_a_volume_with_a_fraction_and_a_volume_too_large_for_a_float_read_back_exactly(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Whole contracts are the norm, but the file holds a fractional volume and any size of whole one, exactly."""
    volumes = ["0.000001", "1234.567891", "9007199254740993", "123456789012345678901234567890"]  # 2**53 + 1; 30 digits
    rows = ",".join(
        f'{{"t":{SESSION_OPEN_MS + i * MINUTE_MS},"o":1.27,"h":1.31,"l":1.25,"c":1.27,"v":{v}}}' for i, v in enumerate(volumes)
    )
    _mock_vendor(raw_json(f'{{"status":"OK","ticker":"O:{SYMBOL}","resultsCount":{len(volumes)},"results":[{rows}]}}'))
    result = await _ensure()

    day = await _read()

    assert result.failure is None
    assert day is not None
    assert [bar.volume for bar in day.bars] == [Decimal(v) for v in volumes]
    assert [str(bar.volume) for bar in day.bars] == volumes


@respx.mock
async def test_the_same_vendor_answer_always_makes_the_same_bytes(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _ok([_bar(0), _bar(1, v=7), _bar(2, o=2.05, h=2.1, l=2.0, c=2.1)])
    _mock_vendor(payload)
    first = await _ensure()
    first_bytes = (_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes()

    (tmp_path / "again").mkdir()
    second_lake = point_lake_writer_at_tmp(tmp_path / "again", monkeypatch)
    install_fake_catalog(monkeypatch)
    second = await _ensure()

    assert first.record is not None and second.record is not None
    assert (_raw_root(second_lake) / RELATIVE_PATH).read_bytes() == first_bytes
    assert second.record.file_sha256 == first.record.file_sha256


# ── The three outcomes the stock path does not have ───────────────────


@respx.mock
async def test_a_listed_contract_that_never_traded_is_a_complete_artifact_with_no_rows(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(no_trades(SYMBOL))

    result = await _ensure()

    assert result.failure is None
    record = result.record
    assert record is not None
    assert record.row_count == 0
    assert fake_catalog.rows[record.id]["status"] == "complete"
    [file] = _lake_files(tmp_lake)
    names, rows = _zip_rows(file.read_bytes())
    assert names == ["20250613_spy250613c00600000_minute_trade.csv"]
    assert rows == []


@respx.mock
async def test_a_day_with_no_trades_reads_back_as_captured_with_no_bars_not_as_missing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(no_trades(SYMBOL))
    result = await _ensure()

    captured_empty = await _read()
    never_asked = await _read(day=date(2025, 6, 12))

    assert result.record is not None
    assert captured_empty is not None
    assert captured_empty.bars == ()
    assert captured_empty.sha256 == result.record.file_sha256
    assert never_asked is None


@respx.mock
async def test_a_day_with_no_trades_is_pinned_and_the_vendor_is_never_asked_again(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor(no_trades(SYMBOL))
    first = await _ensure()

    again = await _ensure()

    assert route.call_count == 1
    assert again.reused is True
    assert again.record == first.record


# ── An empty answer proves nothing: the stored listing does ───────────


OTHER_CONTRACT = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=601)


@respx.mock
async def test_a_contract_with_no_stored_listing_for_its_date_is_refused_with_no_row_and_no_vendor_call(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor(no_trades(SYMBOL))

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "listing_not_captured"
    assert (result.failure.symbol, result.failure.trading_date) == (SYMBOL, TRADING_DAY)
    assert route.call_count == 0
    assert fake_catalog.rows == {} and _lake_files(tmp_lake) == []


@respx.mock
@pytest.mark.parametrize("answer", [no_trades(SYMBOL), traded(SYMBOL, [bar_row(0)])], ids=["a quiet day", "a traded day"])
async def test_a_contract_the_stored_listing_does_not_hold_is_refused_whatever_the_vendor_would_say(
    answer: httpx.Response, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The vendor answers a contract that never existed exactly as it answers one that did not trade."""
    route = _mock_vendor(answer)
    await _list(OTHER_CONTRACT)

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "not_listed"
    assert SYMBOL in result.failure.detail and "SPY" in result.failure.detail
    assert route.call_count == 0
    assert _bar_rows(fake_catalog) == [] and _lake_files(tmp_lake) == []
    assert await _read() is None  # no "captured, never traded" day was stored for it


@respx.mock
async def test_a_listing_of_another_date_does_not_vouch_for_this_date(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor(no_trades(SYMBOL))
    await _list(CONTRACT, day=date(2025, 6, 12))

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.failure is not None and result.failure.reason == "listing_not_captured"
    assert route.call_count == 0


@respx.mock
async def test_a_listing_of_another_underlying_does_not_vouch_for_this_one(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor(no_trades(SYMBOL))
    await _list(CONTRACT)

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="QQQ")

    assert result.failure is not None and result.failure.reason == "listing_not_captured"
    assert route.call_count == 0


@respx.mock
async def test_a_contract_whose_root_is_not_its_underlyings_ticker_is_captured_under_that_underlyings_listing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """``BRK.B`` options are written ``BRKB...``; the listing is the underlying's, the symbol the root's."""
    brk = OptionContract.of(root="BRKB", expiry_date=date(2025, 6, 13), right="call", strike=500)
    _mock_vendor(_ok([_bar(0)], brk.occ_symbol), contract=brk.occ_symbol)
    await _list(brk, underlying="BRK.B")

    result = await ensure_option_day_bars(brk, TRADING_DAY, listed_under="BRK.B")

    assert result.failure is None and result.record is not None
    assert result.record.symbol == "BRKB250613C00500000"


@respx.mock
async def test_a_contract_listed_as_a_non_standard_row_is_still_held_by_the_listing(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(no_trades(SYMBOL))
    respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": "OK", "results": [{**_listing_row(CONTRACT), "exercise_style": "european", "shares_per_contract": 10}]},
        )
    )
    assert (await ensure_option_listing("SPY", TRADING_DAY)).failure is None

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.failure is None and result.record is not None and result.record.row_count == 0


@respx.mock
async def test_a_stored_listing_that_is_not_admitted_is_no_usable_listing_and_nothing_is_asked(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor(no_trades(SYMBOL))
    await _list(CONTRACT)
    (_raw_root(tmp_lake) / "option/usa/listing/spy/20250613_contracts.zip").write_bytes(b"tampered")

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.failure is not None and result.failure.reason == "listing_not_captured"
    assert "capture the listing again" in result.failure.detail
    assert route.call_count == 0 and _bar_rows(fake_catalog) == []


@respx.mock
@pytest.mark.parametrize("damage", ["cut", "zeros", "garbage deflate", "not text"])
async def test_a_stored_listing_that_is_admitted_but_damaged_is_no_usable_listing_whatever_the_damage(
    damage: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The zip module raises a different error for each kind; none of them may escape a capture."""
    route = _mock_vendor(no_trades(SYMBOL))
    await _list(CONTRACT)
    [listing_row] = [row for row in fake_catalog.rows.values() if row["artifact_kind"] == "metadata"]
    listing_file = _raw_root(tmp_lake) / listing_row["file_path"]
    written = listing_file.read_bytes()
    member = "20250613_spy_contracts.csv"
    damaged = {
        "cut": written[: len(written) // 2],
        "zeros": b"\x00" * len(written),
        "garbage deflate": written[: 30 + len(member)] + b"\xff" * 10 + written[40 + len(member):],
        "not text": _zip(member, "ticker\n").replace(b"ticker", b"\xff\xfe\xfd\xfc\xfb\xfa"),
    }[damage]
    if damage == "garbage deflate":  # the premise: the zip module itself raises zlib.error for this one
        with pytest.raises(zlib.error):
            zipfile.ZipFile(io.BytesIO(damaged)).read(member)
    listing_file.write_bytes(damaged)
    listing_row["file_sha256"] = hashlib.sha256(damaged).hexdigest()  # admitted: the catalog holds a receipt for these bytes
    listing_row["file_size_bytes"] = len(damaged)

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "listing_not_captured"
    assert route.call_count == 0 and _bar_rows(fake_catalog) == []


@respx.mock
async def test_an_outage_of_the_catalog_while_reading_the_listing_is_not_a_verdict_on_it(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*args: Any, **kwargs: Any) -> None:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(option_bars_module, "read_option_listing", unreachable)

    with pytest.raises(catalog_client.CatalogUnavailableError):
        await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert fake_catalog.rows == {}


@pytest.mark.parametrize("bad", ["spy", "", "SPY/QQQ", "1SPY", "../SPY"])
async def test_the_underlying_a_contract_is_listed_under_must_be_a_ticker(
    bad: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    with pytest.raises(ValueError, match="listed_under"):
        await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under=bad)
    assert fake_catalog.rows == {}


# ── Beyond the plan's reach: ours before a claim, the vendor's at the edge ─


@respx.mock
@pytest.mark.parametrize("old_day", [date(2024, 10, 11), date(2024, 6, 14), date(2020, 3, 2)])
async def test_a_session_older_than_the_plan_floor_is_refused_with_no_row_and_no_vendor_call(
    old_day: date, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    route = _mock_vendor(outside_plan(), contract="SPY241018C00580000")

    results = [
        await ensure_option_day_bars(contract, old_day, listed_under="SPY", now_ms=NOW_2026_10_12_MS) for _ in range(5)
    ]

    assert [r.record for r in results] == [None] * 5
    assert [r.failure.reason for r in results if r.failure is not None] == ["outside_data_plan"] * 5
    assert all(r.failure is not None and old_day.isoformat() in r.failure.detail for r in results)
    assert route.call_count == 0  # asked five times, never once of the vendor
    assert fake_catalog.rows == {}  # and no row for attempts to pile up on
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
async def test_the_floor_day_itself_is_not_refused_and_the_day_before_it_is(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """On 2026-10-12 the floor is 2024-10-14: 2024-10-11 answered NOT_AUTHORIZED and 2024-10-14 answered with bars."""
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    symbol = contract.occ_symbol
    open_ms = session_open_ms_utc(date(2024, 10, 14))
    route = _mock_vendor(_ok([_bar(t=open_ms)], symbol), contract=symbol)

    day_before = await ensure_option_day_bars(contract, date(2024, 10, 11), listed_under="SPY", now_ms=NOW_2026_10_12_MS)
    floor_day = await _ensure(contract, date(2024, 10, 14), now_ms=NOW_2026_10_12_MS)

    assert day_before.failure is not None and day_before.failure.reason == "outside_data_plan"
    assert floor_day.failure is None and floor_day.record is not None
    assert route.call_count == 1


@respx.mock
async def test_a_day_at_the_edge_the_vendor_says_is_outside_the_plan_is_told_so_by_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    edge_day = date(2024, 10, 14)
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    route = _mock_vendor(outside_plan(), contract=contract.occ_symbol)

    result = await _ensure(contract, edge_day, now_ms=NOW_2026_10_12_MS)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "outside_data_plan"
    assert "timeframe" in result.failure.detail
    assert route.call_count == 1
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"]) == ("failed", "outside_data_plan")
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
async def test_the_vendors_word_decides_not_the_date_so_a_recent_day_told_so_is_outside_the_plan_too(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(outside_plan())

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "outside_data_plan"


@respx.mock
async def test_outside_data_plan_at_the_edge_is_asked_again_every_time_and_never_latches_into_a_timeout(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """A plan upgrade clears it, so more asks than the retry budget still answer outside_data_plan."""
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    route = _mock_vendor(outside_plan(), contract=contract.occ_symbol)

    results = [await _ensure(contract, date(2024, 10, 14), now_ms=NOW_2026_10_12_MS) for _ in range(5)]

    assert [r.failure.reason for r in results if r.failure is not None] == ["outside_data_plan"] * 5
    assert route.call_count == 5


@respx.mock
async def test_after_the_plan_is_upgraded_an_edge_day_that_failed_outside_the_plan_is_captured(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    symbol = contract.occ_symbol
    open_ms = session_open_ms_utc(date(2024, 10, 14))
    _mock_vendor(outside_plan(), outside_plan(), outside_plan(), outside_plan(), _ok([_bar(t=open_ms)], symbol), contract=symbol)

    asked = [await _ensure(contract, date(2024, 10, 14), now_ms=NOW_2026_10_12_MS) for _ in range(5)]

    assert [r.failure.reason for r in asked[:4] if r.failure is not None] == ["outside_data_plan"] * 4
    assert asked[4].failure is None and asked[4].record is not None


@respx.mock
async def test_a_day_told_it_is_outside_the_plan_twice_and_then_hit_by_one_transient_failure_has_spent_its_attempts(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """A known gap, pinned at its true size (the comment at ``NOT_LATCHED_ERRORS`` states it): the next ask is a timeout.

    The exemption lasts only while the row's last error is the plan's, and every ask counts toward the
    three attempts, so two plan answers and one 429 leave none, though the vendor would now answer.
    """
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    open_ms = session_open_ms_utc(date(2024, 10, 14))
    route = _mock_vendor(
        outside_plan(), outside_plan(), httpx.Response(429), _ok([_bar(t=open_ms)], contract.occ_symbol),
        contract=contract.occ_symbol,
    )

    asked = [await _ensure(contract, date(2024, 10, 14), now_ms=NOW_2026_10_12_MS) for _ in range(4)]

    assert [r.failure.reason if r.failure else "captured" for r in asked] == [
        "outside_data_plan", "outside_data_plan", "provider_rate_limited", "fetch_timeout",
    ]
    assert route.call_count == 3  # the fourth ask never reached the vendor, who would have answered with bars


@respx.mock
async def test_a_day_told_it_is_outside_the_plan_once_survives_one_transient_failure_and_is_captured(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The other side of the same number: with one plan answer behind it, a 429 leaves an attempt."""
    contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    open_ms = session_open_ms_utc(date(2024, 10, 14))
    route = _mock_vendor(
        outside_plan(), httpx.Response(429), _ok([_bar(t=open_ms)], contract.occ_symbol), contract=contract.occ_symbol
    )

    asked = [await _ensure(contract, date(2024, 10, 14), now_ms=NOW_2026_10_12_MS) for _ in range(3)]

    assert [r.failure.reason if r.failure else "captured" for r in asked] == [
        "outside_data_plan", "provider_rate_limited", "captured",
    ]
    assert route.call_count == 3


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        httpx.Response(403, text="not entitled"),
        httpx.Response(403, json={"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}),
        httpx.Response(403, json={"status": "NOT_AUTHORIZED"}),
        httpx.Response(403, json={"status": "ERROR", "message": "Your plan doesn't include this data timeframe."}),
    ],
)
async def test_any_other_403_is_an_entitlement_problem_whatever_the_date(
    body: httpx.Response, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(body)

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "provider_entitlement_error"


def test_the_plan_floor_is_two_years_back_plus_the_two_days_the_lake_composes_inside() -> None:
    assert option_history_floor(date(2026, 10, 12)) == date(2024, 10, 14)
    # 29 February has no anniversary two years on from a leap year's: 2028-02-29 -> 2026-02-28, plus two days.
    assert option_history_floor(date(2028, 2, 29)) == date(2026, 3, 2)


@respx.mock
@pytest.mark.parametrize(("session", "opens_ms"), [(day, ms) for _, day, ms in CAPTURE_OPENS], ids=[label for label, _, _ in CAPTURE_OPENS])
async def test_a_session_is_refused_until_new_york_midnight_after_it_and_taken_from_that_midnight(
    session: date, opens_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """One millisecond before the midnight that ends the session's New York date is refused; the midnight is taken.

    The days around both clock changes would each be an hour out under a fixed offset, and the two half
    days show an early close changes nothing about the rule.
    """
    contract = OptionContract.of(root="SPY", expiry_date=session, right="call", strike=600)
    route = _mock_vendor(_ok([_bar(t=session_open_ms_utc(session))], contract.occ_symbol), contract=contract.occ_symbol)

    too_early = await ensure_option_day_bars(contract, session, listed_under="SPY", now_ms=opens_ms - 1)

    assert too_early.record is None
    assert too_early.failure is not None and too_early.failure.reason == "session_not_over"
    assert route.call_count == 0
    assert fake_catalog.rows == {}  # refused before any claim
    assert _lake_files(tmp_lake) == []

    on_time = await _ensure(contract, session, now_ms=opens_ms)

    assert on_time.failure is None and on_time.record is not None
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    ("session", "close_ms"),
    [(TRADING_DAY, SESSION_CLOSE_MS), *HALF_DAY_CLOSE_MS.items()],
    ids=["a full day", "a half day in november", "a half day in july"],
)
async def test_the_evening_of_a_session_is_refused_from_the_close_to_the_midnight_on_a_full_day_or_a_half_day(
    session: date, close_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Six hours after the close it is 22:00 New York on the full day, which UTC already calls the next date."""
    contract = OptionContract.of(root="SPY", expiry_date=session, right="call", strike=600)
    route = _mock_vendor(contract=contract.occ_symbol)

    at_the_close = await ensure_option_day_bars(contract, session, listed_under="SPY", now_ms=close_ms)
    half_an_hour_on = await ensure_option_day_bars(contract, session, listed_under="SPY", now_ms=close_ms + 30 * MINUTE_MS)
    six_hours_on = await ensure_option_day_bars(contract, session, listed_under="SPY", now_ms=close_ms + 6 * 60 * MINUTE_MS)

    for refused in (at_the_close, half_an_hour_on, six_hours_on):
        assert refused.failure is not None and refused.failure.reason == "session_not_over"
    assert route.call_count == 0 and fake_catalog.rows == {}


@respx.mock
@pytest.mark.parametrize("not_a_session", [date(2025, 6, 14), date(2025, 6, 19)])  # a Saturday; Juneteenth
async def test_a_day_the_exchange_was_closed_is_refused(not_a_session: date, fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor()

    result = await ensure_option_day_bars(CONTRACT, not_a_session, listed_under="SPY")

    assert result.failure is not None and result.failure.reason == "not_a_session"
    assert route.call_count == 0 and fake_catalog.rows == {}


@respx.mock
async def test_a_symbol_longer_than_the_catalogs_twenty_characters_is_refused_before_any_claim(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    long_contract = OptionContract.of(root="ABCDEF", expiry_date=date(2025, 6, 13), right="call", strike=600)
    assert len(long_contract.occ_symbol) == 21
    route = _mock_vendor(contract=long_contract.occ_symbol)

    result = await ensure_option_day_bars(long_contract, TRADING_DAY, listed_under="SPY")

    assert result.record is None
    assert result.failure is not None
    assert result.failure.reason == "symbol_too_long"
    assert "21" in result.failure.detail and "20" in result.failure.detail
    assert route.call_count == 0 and fake_catalog.rows == {}
    assert await _read(long_contract) is None  # nothing was ever stored for it


@respx.mock
async def test_a_twenty_character_symbol_still_fits(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    fits = OptionContract.of(root="ABCDE", expiry_date=date(2025, 6, 13), right="put", strike=45.5)
    assert len(fits.occ_symbol) == 20
    _mock_vendor(contract=fits.occ_symbol)

    result = await _ensure(fits)

    assert result.failure is None and result.record is not None
    assert result.record.symbol == fits.occ_symbol


# ── A lost claim ─────────────────────────────────────────────────────────


@respx.mock
async def test_a_second_capture_reuses_the_complete_row_without_asking_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor()
    first = await _ensure()

    second = await _ensure()

    assert route.call_count == 1
    assert second.reused is True and second.failure is None
    assert second.record == first.record
    assert len(_bar_rows(fake_catalog)) == 1


# ── Another provider's row for the same contract-day is not this provider's row ──


@respx.mock
async def test_another_providers_complete_row_with_the_same_file_and_recipe_is_not_reused_for_this_providers_failed_row(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The lookup after a lost claim took the first complete row of the contract-day, whichever provider's."""
    route = _mock_vendor()
    first = await _ensure()
    assert first.record is not None
    other_id = await complete_row_of_another_provider(first.record)
    assert await catalog_client.mark_complete_artifact_failed(first.record.id, "provider_api_error", "gone wrong")

    again = await _ensure()

    assert again.failure is None and again.reused is False
    assert again.record is not None and again.record.id == first.record.id and again.record.provider == "polygon"
    assert route.call_count == 2  # asked again for its own row
    assert fake_catalog.rows[first.record.id]["status"] == "complete"
    other = fake_catalog.rows[other_id]
    assert (other["provider"], other["status"], other["lease_generation"]) == ("alpaca", "complete", 1)


@respx.mock
async def test_another_providers_complete_row_is_never_refreshed_with_this_providers_bytes(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Under another recipe the lookup's row was refreshed and published over: the other provider's artifact held our bytes."""
    _mock_vendor()
    first = await _ensure()
    assert first.record is not None
    other_id = await complete_row_of_another_provider(first.record, data_contract_hash="0" * 64, file_path="elsewhere/theirs.zip")
    before = dict(fake_catalog.rows[other_id])
    assert await catalog_client.mark_complete_artifact_failed(first.record.id, "provider_api_error", "gone wrong")

    again = await _ensure()

    assert again.failure is None and again.record is not None and again.record.id == first.record.id
    assert fake_catalog.rows[other_id] == before  # not one field of it changed
    assert fake_catalog.rows[first.record.id]["status"] == "complete"


@respx.mock
async def test_another_providers_complete_row_is_not_a_captured_option_day(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor()
    first = await _ensure()
    assert first.record is not None
    await complete_row_of_another_provider(first.record)
    assert await catalog_client.mark_complete_artifact_failed(first.record.id, "provider_api_error", "gone wrong")

    named_with_only_the_others_row = await captured_option_days([SYMBOL], TRADING_DAY)
    await _ensure()
    named_with_both = await captured_option_days([SYMBOL], TRADING_DAY)

    assert named_with_only_the_others_row == frozenset()
    assert named_with_both == frozenset({SYMBOL})


@respx.mock
async def test_a_complete_row_whose_file_has_gone_is_fetched_again(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor()
    first = await _ensure()
    (_raw_root(tmp_lake) / RELATIVE_PATH).unlink()

    again = await _ensure()

    assert route.call_count == 2
    assert again.reused is False and again.failure is None
    assert first.record is not None and again.record is not None
    assert again.record.id == first.record.id  # the same row, refreshed under a new generation
    assert fake_catalog.rows[again.record.id]["lease_generation"] == catalog_client.INITIAL_LEASE_GENERATION + 1
    assert (_raw_root(tmp_lake) / RELATIVE_PATH).is_file()


@respx.mock
async def test_a_complete_row_whose_bytes_changed_is_fetched_again(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor()
    await _ensure()
    (_raw_root(tmp_lake) / RELATIVE_PATH).write_bytes(b"not what the catalog admitted")

    again = await _ensure()

    assert route.call_count == 2
    assert again.failure is None and again.reused is False
    day = await _read()
    assert day is not None and len(day.bars) == 1


@respx.mock
async def test_a_complete_row_under_an_older_recipe_is_fetched_again(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor()
    first = await _ensure()
    assert first.record is not None
    fake_catalog.rows[first.record.id]["data_contract_hash"] = "0" * 64

    again = await _ensure()

    assert route.call_count == 2
    assert again.record is not None and again.record.data_contract_hash == option_minute_trade_dch()


@respx.mock
async def test_a_row_another_worker_is_still_fetching_is_left_alone(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor()
    other = await _ensure(now_ms=NOW_2026_10_12_MS)
    assert other.record is not None
    row = fake_catalog.rows[other.record.id]
    row.update(status="fetching", lease_owner="someone-else", lease_expires_at_ms=10**15)

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "lease_timeout"
    assert route.call_count == 1


@respx.mock
async def test_a_failed_row_inside_its_budget_is_taken_again_and_completes(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_vendor(httpx.Response(500, text="boom"), _ok([_bar(0)]))
    failed = await _ensure()
    assert failed.failure is not None and failed.failure.reason == "provider_api_error"

    retried = await _ensure()

    assert retried.failure is None and retried.record is not None
    assert route.call_count == 2
    assert [row["attempt_count"] for row in _bar_rows(fake_catalog)] == [2]


@respx.mock
async def test_a_failed_row_that_spent_its_retries_is_a_timeout_and_the_vendor_is_left_alone(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor(httpx.Response(500, text="boom"))
    for _ in range(3):
        await _ensure()

    exhausted = await _ensure()

    assert exhausted.failure is not None and exhausted.failure.reason == "fetch_timeout"
    assert route.call_count == 3


@respx.mock
async def test_a_writer_that_lost_its_lease_publishes_nothing_and_does_not_fail_the_row(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch_then_lose_the_lease(*args: Any, **kwargs: Any) -> list[PolygonOptionBar]:
        # Another worker takes the row while this one is fetching: the generation moves on.
        [row] = _bar_rows(fake_catalog)
        row["lease_generation"] += 1
        row["lease_owner"] = "the-winner"
        return [_polygon_option_bar()]

    monkeypatch.setattr(option_bars_module, "fetch_option_minute_bars", fetch_then_lose_the_lease)

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "lease_timeout"
    [row] = _bar_rows(fake_catalog)
    assert row["status"] == "fetching" and row["lease_owner"] == "the-winner"  # not failed by the loser
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


def _polygon_option_bar() -> PolygonOptionBar:
    return PolygonOptionBar(
        t_ms=SESSION_OPEN_MS, open=Decimal("1.27"), high=Decimal("1.31"), low=Decimal("1.25"), close=Decimal("1.27"),
        volume=Decimal(12),
    )


async def test_a_writer_that_lost_its_lease_and_the_winner_publishing_together_leave_the_winners_file_and_a_complete_row(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both stage their bytes first. If they staged to one path, the loser's refusal would delete the winner's staged file.

    The staging path carries the lease generation, so each writer has its own. The scheduler is told to give the
    stale writer its turn at the catalog (refusal and cleanup) before the winner's, the order that hurts.
    """
    dch = option_minute_trade_dch()
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa_options", symbol=SYMBOL, trading_date=TRADING_DAY,
        resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw",
    )
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id=option_lake_module.WORKER_ID, lease_ttl_ms=option_lake_module.LEASE_TTL_MS,
        data_contract_hash=dch, file_path=RELATIVE_PATH,
    )
    assert artifact_id is not None
    fake_catalog.rows[artifact_id]["lease_expires_at_ms"] = 1  # the first writer's lease runs out
    winner_generation = await catalog_client.steal_or_retry_minute_bar(
        artifact_id, option_lake_module.WORKER_ID, option_lake_module.LEASE_TTL_MS, option_lake_module.MAX_CLAIM_RETRIES,
        fresh_budget_for_contract=dch,
    )
    assert winner_generation == 2
    stale_file, winner_file = one_bar_file("1.11"), one_bar_file("2.22")
    publish_stale_writer_first_once_both_have_staged(monkeypatch, winner_generation=2)
    shared = {
        "rel_path": PurePosixPath(RELATIVE_PATH), "row_count": 1, "first_bar_start_ms": SESSION_OPEN_MS,
        "last_bar_start_ms": SESSION_OPEN_MS, "data_contract_hash": dch, "symbol": SYMBOL, "trading_date": TRADING_DAY,
    }

    stale, winner = await asyncio.gather(
        publish_option_artifact(payload=stale_file, lease=HeldLease(artifact_id, 1), **shared),
        publish_option_artifact(payload=winner_file, lease=HeldLease(artifact_id, 2), **shared),
    )

    assert isinstance(stale, OptionDataFailure) and stale.reason == "lease_timeout"  # told the row is not its own
    assert winner == hashlib.sha256(winner_file).hexdigest()
    assert (_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes() == winner_file
    row = fake_catalog.rows[artifact_id]
    assert (row["status"], row["file_sha256"], row["lease_generation"]) == ("complete", winner, 2)
    assert _staged_files(tmp_lake) == []


# ── A vendor error is a typed failure on the row ─────────────────────────


@respx.mock
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(401, text="no key"), "provider_auth_error"),
        (httpx.Response(403, text="not entitled"), "provider_entitlement_error"),
        (httpx.Response(429), "provider_rate_limited"),
        (httpx.Response(404), "unknown_symbol"),
        (httpx.Response(500, text="boom"), "provider_api_error"),
    ],
)
async def test_each_vendor_error_fails_the_row_with_its_own_reason_and_publishes_nothing(
    response: httpx.Response, reason: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(response)

    # The plan floor moves with the calendar, so the 403 is always asked on a named day.
    result = await _ensure(now_ms=NOW_2026_10_12_MS)

    assert result.record is None and result.reused is False
    assert result.failure is not None and result.failure.reason == reason
    assert (result.failure.symbol, result.failure.trading_date) == (SYMBOL, TRADING_DAY)
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"]) == ("failed", reason)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
async def test_a_transport_failure_is_a_provider_api_error(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/.*").mock(side_effect=httpx.ConnectError("down"))

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "provider_api_error"


@respx.mock
@pytest.mark.parametrize(
    "key",
    ["", "  \n", "Zq9xV7wK\u00e9", "Zq9xV7wK Zq9xV7wK", "Zq9xV7wK\x00", "Zq9xV7wK\ud800"],
    ids=["empty", "only whitespace", "accented letter", "inner space", "nul", "lone surrogate"],
)
async def test_a_key_that_cannot_be_sent_fails_the_row_as_an_auth_error_and_leaves_none_fetching(
    key: str, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A raw ``UnicodeEncodeError`` once escaped here and left the row ``fetching`` for the length of its lease."""
    route = _mock_vendor()
    await _list(CONTRACT)  # stored while the key is good
    stored = refuse_what_postgres_refuses(monkeypatch)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", key)
    caplog.set_level(logging.DEBUG)

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "provider_auth_error"
    assert "malformed" in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "provider_auth_error", None)
    assert route.call_count == 0  # no request was made
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    shown = [result.failure.detail, *stored, *(r.getMessage() + repr(r.__dict__) for r in caplog.records)]
    assert pieces_of_the_key_in(key.strip(), *shown) == set()


@respx.mock
async def test_a_vendor_error_text_with_a_nul_is_stored_without_it_and_the_row_is_failed_not_left_fetching(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table refuses a NUL, so recording the failure raised and the row stayed ``fetching``."""
    stored = refuse_what_postgres_refuses(monkeypatch)
    _mock_vendor(httpx.Response(500, content=b"boom\x00boom"))

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "provider_api_error"
    assert "boomboom" in result.failure.detail and "\x00" not in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "provider_api_error", None)
    assert stored == [result.failure.detail]


@respx.mock
async def test_a_vendor_status_with_a_character_no_utf8_holds_is_stored_cleaned_and_the_row_is_failed(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON can spell a lone surrogate (``\\ud800``); the status is echoed into the error text, and the driver cannot encode it."""
    stored = refuse_what_postgres_refuses(monkeypatch)
    _mock_vendor(raw_json('{"status":"\\ud800","ticker":"O:' + SYMBOL + '","resultsCount":0}'))

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "provider_api_error"
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "provider_api_error", None)
    assert len(stored) == 1 and stored[0] == result.failure.detail
    stored[0].encode("utf-8")  # storable


def test_a_failure_text_loses_its_nuls_is_made_encodable_and_is_cut_to_the_stored_length() -> None:
    assert storable_failure_text("a\x00b\x00\x00c") == "abc"
    assert storable_failure_text("x\ud800y") == "x?y"
    assert storable_failure_text("caf\u00e9 \u2603") == "caf\u00e9 \u2603"  # what UTF-8 holds is kept
    assert storable_failure_text("x" * (MAX_FAILURE_TEXT_CHARS + 500)) == "x" * MAX_FAILURE_TEXT_CHARS
    assert storable_failure_text("") == ""
    assert MAX_FAILURE_TEXT_CHARS > 200  # the vendor's body is cut to 200 elsewhere; this never clips that


async def _claim_the_contract_day() -> int:
    """This worker's own claim on the contract-day, at the first generation: the row a capture would fail."""
    claimed = await catalog_client.claim_minute_bar(
        identity=ArtifactIdentity(
            artifact_kind="time_series_bars", market="usa_options", symbol=SYMBOL, trading_date=TRADING_DAY,
            resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw",
        ),
        worker_id=option_lake_module.WORKER_ID, lease_ttl_ms=option_lake_module.LEASE_TTL_MS,
        data_contract_hash=option_minute_trade_dch(), file_path=RELATIVE_PATH,
    )
    assert claimed is not None
    return claimed


async def test_fail_row_hands_the_catalog_the_cleaned_text_and_answers_with_it(
    fake_catalog: FakeCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = refuse_what_postgres_refuses(monkeypatch)
    artifact_id = await _claim_the_contract_day()

    result = await fail_row(
        artifact_id=artifact_id, lease_generation=catalog_client.INITIAL_LEASE_GENERATION, reason="provider_api_error",
        detail="bad\x00\ud800" + "y" * 5000, symbol=SYMBOL, trading_date=TRADING_DAY,
    )

    assert result.failure is not None
    assert stored == [result.failure.detail] and stored[0].startswith("bad?y")
    assert len(stored[0]) == MAX_FAILURE_TEXT_CHARS


async def test_a_failure_whose_lease_was_taken_over_is_not_recorded_and_is_answered_as_lease_lost(
    fake_catalog: FakeCatalog,
) -> None:
    artifact_id = await _claim_the_contract_day()
    fake_catalog.rows[artifact_id].update(lease_generation=2, lease_owner="the-winner")

    result = await fail_row(
        artifact_id=artifact_id, lease_generation=catalog_client.INITIAL_LEASE_GENERATION, reason="provider_auth_error",
        detail="no key", symbol=SYMBOL, trading_date=TRADING_DAY,
    )

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "lease_timeout"
    row = fake_catalog.rows[artifact_id]
    assert (row["status"], row["lease_owner"], row["lease_generation"], row["last_error"]) == ("fetching", "the-winner", 2, None)


#: What a writer that lost its lease can meet while it fetches or validates: the answer, and the reason it would have been failed with.
_STALE_WRITER_MEETS = [
    ("a 401", lambda: httpx.Response(401, text="no key"), "provider_auth_error"),
    ("a 403", lambda: httpx.Response(403, text="not entitled"), "provider_entitlement_error"),
    ("a 429", lambda: httpx.Response(429), "provider_rate_limited"),
    ("a 404", lambda: httpx.Response(404), "unknown_symbol"),
    ("a 500", lambda: httpx.Response(500, text="boom"), "provider_api_error"),
    ("the plan's 403", outside_plan, "outside_data_plan"),
    ("a malformed answer", lambda: bad_bars_answers(SYMBOL)["bare object"](), "provider_api_error"),
    ("corrupt bars", lambda: _ok([_bar(0, h=1.0)]), "validation_failed"),
]


@respx.mock
@pytest.mark.parametrize(
    ("answer", "would_have_been"), [(a, r) for _, a, r in _STALE_WRITER_MEETS], ids=[name for name, _, _ in _STALE_WRITER_MEETS]
)
async def test_a_stale_writers_vendor_failure_comes_back_as_lease_lost_and_the_row_is_whatever_the_new_owner_makes_it(
    answer: Any, would_have_been: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """A writer whose lease was taken over while it fetched met a failure: it must not report it as the row's."""

    def another_worker_takes_the_row_while_the_vendor_answers(request: httpx.Request) -> httpx.Response:
        [row] = _bar_rows(fake_catalog)
        row.update(lease_generation=row["lease_generation"] + 1, lease_owner="the-winner")
        return answer()

    _mock_vendor_with(another_worker_takes_the_row_while_the_vendor_answers)

    result = await _ensure(now_ms=NOW_2026_10_12_MS)

    assert result.record is None
    assert result.failure is not None
    assert result.failure.reason == "lease_timeout" != would_have_been
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["lease_owner"], row["lease_generation"], row["last_error"]) == ("fetching", "the-winner", 2, None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    # the new owner's own lease is intact: it can still finish the row
    assert await catalog_client.fail_artifact(
        row["id"], "provider_api_error", "its own", worker_id="the-winner", lease_generation=2
    )


@respx.mock
@pytest.mark.parametrize(
    ("results", "fragment"),
    [
        ([_bar(0), _bar(0)], "duplicate timestamp"),
        ([_bar(0), _bar(1, t=SESSION_OPEN_MS + 24 * 60 * MINUTE_MS)], "outside requested session 2025-06-13"),
        ([_bar(0, h=1.0)], "high 1.0 < open 1.27"),
        ([_bar(0, v=-1)], "volume=-1 is negative"),
    ],
)
async def test_corrupt_bars_fail_the_row_naming_the_offender_and_publish_nothing(
    results: list[dict[str, Any]], fragment: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_vendor(_ok(results))

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert fragment in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"]) == ("failed", "validation_failed")
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
@pytest.mark.parametrize("price", ["1e30", "1e400", "1e999999999"])
async def test_a_price_the_file_cannot_hold_fails_the_row_and_raises_nothing(
    price: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The publication gate passes any finite positive price; encoding to ten-thousandths is where 1e30 cannot go on."""
    _mock_vendor(raw_json(
        '{"status":"OK","ticker":"O:' + SYMBOL + '","resultsCount":1,"results":[{"t":' + str(SESSION_OPEN_MS)
        + f',"o":{price},"h":{price},"l":{price},"c":{price},"v":3}}]}}'
    ))

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "validation_failed", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
@pytest.mark.parametrize("name", list(bad_bars_answers(SYMBOL)))
async def test_a_vendor_answer_that_is_not_well_formed_fails_the_row_and_raises_nothing(
    name: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Every body that is not well formed: a caller looping over contracts must get a result for each, and no row may stay fetching."""
    route = _mock_vendor(bad_bars_answers(SYMBOL)[name]())

    result = await _ensure()  # returns; nothing escapes

    assert result.record is None and result.reused is False
    assert result.failure is not None and result.failure.reason == "provider_api_error"
    assert route.call_count == 1
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "provider_api_error", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    assert await _read() is None  # no day, empty or not, was stored


@respx.mock
@pytest.mark.parametrize("name", ["bare object", "status alone", "count 5 and results empty", "count 5 and no results key", "results null"])
async def test_an_answer_that_proved_nothing_pins_nothing_and_a_later_good_answer_is_captured(
    name: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The failure this guards: a bare 200 once became a complete zero-row day that no later ask could correct."""
    route = _mock_vendor(bad_bars_answers(SYMBOL)[name](), _ok([_bar(0), _bar(1)]))
    first = await _ensure()

    second = await _ensure()

    assert first.failure is not None and second.failure is None and second.record is not None
    assert route.call_count == 2
    day = await _read()
    assert day is not None and len(day.bars) == 2


@respx.mock
async def test_a_price_the_float_route_would_round_the_other_way_is_stored_by_its_exact_digits(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """1.0000499999999999999 is one part in 10^19 under the half-way point 1.00005: the exact digits store 1.0000.

    Through a float it becomes 1.00005 and stores 1.0001, a different integer.
    """
    digits = "1.0000499999999999999"
    _mock_vendor(raw_json(
        '{"status":"OK","ticker":"O:' + SYMBOL + '","resultsCount":1,"results":[{"t":' + str(SESSION_OPEN_MS)
        + f',"o":{digits},"h":{digits},"l":{digits},"c":{digits},"v":3}}]}}'
    ))
    result = await _ensure()

    day = await _read()

    assert result.failure is None
    assert day is not None
    assert (day.bars[0].open, day.bars[0].close) == (Decimal("1.0000"), Decimal("1.0000"))
    _, rows = _zip_rows((_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes())
    assert rows == ["34200000,10000,10000,10000,10000,3"]


@respx.mock
@pytest.mark.parametrize(
    "configured_key",
    [SENTINEL_KEY, f"{SENTINEL_KEY}\n", f"  {SENTINEL_KEY}\r\n"],
    ids=["as typed", "with a trailing newline", "with spaces in front and a crlf behind"],
)
async def test_the_key_is_in_no_url_no_log_line_no_error_text_and_no_stored_failure(
    configured_key: str, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The key is sent as itself, stripped of the whitespace an ``.env`` line or a copy-paste leaves on it."""
    monkeypatch.setattr(settings, "POLYGON_API_KEY", configured_key)
    caplog.set_level(logging.INFO)  # the level the app logs at; httpx logs each request's URL there
    failed_with: list[Any] = []
    stored_fail = catalog_client.fail_artifact

    async def record_then_fail(*args: Any, **kwargs: Any) -> bool:
        failed_with.append((args, kwargs))
        return await stored_fail(*args, **kwargs)

    monkeypatch.setattr(catalog_client, "fail_artifact", record_then_fail)
    contracts = [OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=s) for s in (600, 601, 602, 603)]
    await _list(*contracts)
    answers = [_ok([_bar(0)], contracts[0].occ_symbol), httpx.Response(429), outside_plan(), bad_bars_answers(contracts[3].occ_symbol)["bare object"]()]
    for contract, answer in zip(contracts, answers, strict=True):
        _mock_vendor(answer, contract=contract.occ_symbol)
    results = [await ensure_option_day_bars(c, TRADING_DAY, listed_under="SPY") for c in contracts]

    assert [r.failure.reason if r.failure else "captured" for r in results] == [
        "captured", "provider_rate_limited", "outside_data_plan", "provider_api_error",
    ]
    calls = list(respx.calls)
    assert len(calls) == 5  # the listing, then one ask for each contract
    for call in calls:
        assert SENTINEL_KEY not in str(call.request.url)
        assert call.request.headers["Authorization"] == f"Bearer {SENTINEL_KEY}"
    assert any(r.name == "httpx" for r in caplog.records)  # the guard is not vacuous
    for record in caplog.records:
        assert SENTINEL_KEY not in record.getMessage() and SENTINEL_KEY not in repr(record.__dict__)
    assert len(failed_with) == 3
    assert SENTINEL_KEY not in repr(failed_with) + repr(results) + repr([row["last_error"] for row in fake_catalog.rows.values()])


@respx.mock
async def test_publishing_again_and_again_does_not_leave_a_new_set_of_staging_folders_each_time(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Nothing sweeps the staging tree, so a fresh folder name per publish would leave 8 empty folders each time."""
    staging = tmp_lake / "staging"

    def folders() -> set[Path]:
        return {p.relative_to(staging) for p in staging.rglob("*") if p.is_dir()}

    sessions = [date(2025, 6, 9), date(2025, 6, 10), date(2025, 6, 11), date(2025, 6, 12), date(2025, 6, 13)]
    after: list[set[Path]] = []
    for day in sessions:
        _mock_vendor(_ok([_bar(t=session_open_ms_utc(day))]))
        result = await _ensure(CONTRACT, day)
        assert result.failure is None and result.record is not None
        after.append(folders())

    assert after[0]  # staging was used: the first publish made the folders
    assert after[1] == after[2] == after[3] == after[4] == after[0]  # the fifth publish needed no new one
    assert _staged_files(tmp_lake) == []


# ── Reading is only through admission ────────────────────────────────────


async def test_a_day_with_no_file_reads_as_not_captured(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    assert await _read() is None


async def test_a_file_the_catalog_never_admitted_is_refused_not_read(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    path = _raw_root(tmp_lake) / RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan: renamed into place but never given a receipt")

    with pytest.raises(LakeAdmissionError):
        await _read()


@respx.mock
async def test_bytes_that_changed_after_their_receipt_are_refused_not_read(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor()
    await _ensure()
    (_raw_root(tmp_lake) / RELATIVE_PATH).write_bytes(b"tampered")

    with pytest.raises(LakeAdmissionError):
        await _read()


@respx.mock
async def test_a_reader_called_on_the_event_loop_thread_answers_none_for_a_day_with_no_file_and_raises_for_a_captured_one(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The reader's docstring says so: the thread error first shows on a captured day."""
    _mock_vendor()
    await _ensure()

    assert read_option_day_bars(CONTRACT, date(2025, 6, 12)) is None  # called here, on the loop's own thread
    with pytest.raises(RuntimeError, match="worker thread"):
        read_option_day_bars(CONTRACT, TRADING_DAY)


@respx.mock
async def test_a_reader_whose_catalog_is_unreachable_raises_the_outage_and_not_a_missing_day(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_vendor()
    await _ensure()

    async def down(*args: Any, **kwargs: Any) -> bool:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", down)

    with pytest.raises(catalog_client.CatalogUnavailableError):
        await _read()


@respx.mock
async def test_a_lake_root_without_its_identity_marker_refuses_the_bytes(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor()
    await _ensure()
    (tmp_lake / "lake" / ".data-root.json").unlink()

    with pytest.raises(LakeAdmissionError):
        await _read()


@respx.mock
async def test_the_reader_takes_the_admission_it_is_given(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """A worker that owns no catalog connection hands in a callable that asks its parent."""
    _mock_vendor()
    await _ensure()
    asked: list[Path] = []

    def admit(path: Path) -> bytes:
        asked.append(path)
        return path.read_bytes()

    day = read_option_day_bars(CONTRACT, TRADING_DAY, admission=admit)

    assert day is not None and len(day.bars) == 1
    assert asked == [_raw_root(tmp_lake) / RELATIVE_PATH]


@respx.mock
async def test_a_day_is_read_from_the_lake_root_it_is_handed_and_the_configured_root_is_not_touched(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path
) -> None:
    """A snapshot or an alternate mount of the lake: the same relative path, the same receipt."""
    _mock_vendor()
    result = await _ensure()
    mount = tmp_path / "mount"
    shutil.copytree(tmp_lake / "lake", mount / "lake")
    shutil.rmtree(_raw_root(tmp_lake) / "option")  # the configured root no longer holds the day

    configured = await _read()
    from_the_mount = await asyncio.to_thread(read_option_day_bars, CONTRACT, TRADING_DAY, lake_root=mount / "lake" / "raw")

    assert result.record is not None
    assert configured is None
    assert from_the_mount is not None and len(from_the_mount.bars) == 1
    assert from_the_mount.sha256 == result.record.file_sha256


@pytest.mark.parametrize("where", ["somewhere", "raw", "lake", "lake/polygon_split_adjusted", "lake/raw/option"])
async def test_a_lake_root_that_is_not_a_raw_lake_root_is_refused_rather_than_read_without_admission(
    where: str, fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path
) -> None:
    """The default admission lets a path outside the lake through unchecked, so such a root must never reach it."""
    loose = tmp_path / "loose" / where
    day_file = loose / RELATIVE_PATH
    day_file.parent.mkdir(parents=True)
    day_file.write_bytes(_zip(MEMBER, "34200000,12700,13100,12500,12700,12\n"))

    with pytest.raises(ValueError, match="lake_root"):
        await asyncio.to_thread(read_option_day_bars, CONTRACT, TRADING_DAY, lake_root=loose)


# ── Which contract-days are captured ─────────────────────────────────────

THIRD_CONTRACT = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=602)
UNASKED_CONTRACT = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=603)


@respx.mock
async def test_the_captured_contract_days_of_a_date_come_from_one_catalog_query(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracts = {c.occ_symbol: c for c in (CONTRACT, OTHER_CONTRACT, THIRD_CONTRACT, UNASKED_CONTRACT)}
    await _list(*contracts.values())
    await _list(CONTRACT, day=date(2025, 6, 12))
    for day, answer in ((TRADING_DAY, _ok([_bar(0)])), (date(2025, 6, 12), _ok([_bar(t=session_open_ms_utc(date(2025, 6, 12)))]))):
        respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/O:{SYMBOL}/range/1/minute/{day.isoformat()}/.*").mock(
            return_value=answer
        )
    _mock_vendor(no_trades(OTHER_CONTRACT.occ_symbol), contract=OTHER_CONTRACT.occ_symbol)
    _mock_vendor(httpx.Response(500, text="boom"), contract=THIRD_CONTRACT.occ_symbol)
    captured = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")
    quiet = await ensure_option_day_bars(OTHER_CONTRACT, TRADING_DAY, listed_under="SPY")
    failed = await ensure_option_day_bars(THIRD_CONTRACT, TRADING_DAY, listed_under="SPY")
    still_fetching = await catalog_client.claim_minute_bar(
        identity=ArtifactIdentity(
            artifact_kind="time_series_bars", market="usa_options", symbol=UNASKED_CONTRACT.occ_symbol,
            trading_date=TRADING_DAY, resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw",
        ),
        worker_id="w", lease_ttl_ms=300_000, data_contract_hash=option_minute_trade_dch(), file_path="x.zip",
    )
    earlier_day_only = await ensure_option_day_bars(CONTRACT, date(2025, 6, 12), listed_under="SPY")
    assert captured.failure is None and quiet.failure is None and failed.failure is not None
    assert still_fetching is not None and earlier_day_only.failure is None
    asked: list[tuple] = []
    real = fake_catalog.select_complete_minute_bar_symbols

    async def counting(*args: Any, **kwargs: Any):
        asked.append((args, kwargs))
        return await real(*args, **kwargs)

    monkeypatch.setattr(catalog_client, "select_complete_minute_bar_symbols", counting)

    named = await captured_option_days(list(contracts), TRADING_DAY)
    named_a_day_earlier = await captured_option_days(list(contracts), date(2025, 6, 12))

    # captured with trades and captured with none; not the failed, the one still being fetched, or the one not asked for
    assert named == frozenset({CONTRACT.occ_symbol, OTHER_CONTRACT.occ_symbol})
    assert named_a_day_earlier == frozenset({CONTRACT.occ_symbol})
    assert len(asked) == 2  # one question per date, however many contracts


@respx.mock
async def test_only_the_symbols_asked_about_are_named(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor()
    await _ensure()

    assert await captured_option_days([OTHER_CONTRACT.occ_symbol], TRADING_DAY) == frozenset()
    assert await captured_option_days({CONTRACT.occ_symbol}, TRADING_DAY) == frozenset({CONTRACT.occ_symbol})


async def test_one_symbol_given_as_a_string_is_a_mistake_and_not_an_answer_of_none(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    with pytest.raises(TypeError, match="not one string"):
        await captured_option_days(SYMBOL, TRADING_DAY)  # type: ignore[arg-type]


async def test_naming_no_symbols_asks_the_catalog_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_connection() -> None:
        raise AssertionError("an empty question reached the catalog")

    monkeypatch.setattr(catalog_client, "connection", no_connection)

    assert await captured_option_days([], TRADING_DAY) == frozenset()


@respx.mock
async def test_a_stock_row_with_a_contracts_symbol_is_not_a_captured_option_day(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    stock = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa", symbol=SYMBOL, trading_date=TRADING_DAY, resolution="minute",
        data_type="trade", provider="polygon", price_adjustment_mode="raw",
    )
    stock_id = await catalog_client.claim_minute_bar(
        identity=stock, worker_id="w", lease_ttl_ms=300_000, data_contract_hash="a" * 64, file_path="equity/x.zip"
    )
    assert stock_id is not None
    assert await catalog_client.complete_artifact(
        stock_id, 1, SESSION_OPEN_MS, SESSION_OPEN_MS, 10, "b" * 64, lease_generation=catalog_client.INITIAL_LEASE_GENERATION
    )

    assert await captured_option_days([SYMBOL], TRADING_DAY) == frozenset()


@respx.mock
async def test_a_complete_row_whose_file_has_since_gone_is_still_named_and_the_reader_says_it_is_not_there(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The catalog question is not the judge of "readable": the reader is."""
    _mock_vendor()
    await _ensure()
    (_raw_root(tmp_lake) / RELATIVE_PATH).unlink()

    assert await captured_option_days([SYMBOL], TRADING_DAY) == frozenset({SYMBOL})
    assert await _read() is None


# ── A listing already read is not read again ─────────────────────────────


def _count_listing_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    reads: list[tuple] = []
    real = option_bars_module.read_option_listing

    def counting(*args: Any, **kwargs: Any):
        reads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(option_bars_module, "read_option_listing", counting)
    return reads


@respx.mock
async def test_a_listing_handed_in_is_used_in_place_of_a_read_for_every_contract_of_the_date(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracts = [CONTRACT, OTHER_CONTRACT, THIRD_CONTRACT]
    await _list(*contracts)
    for contract in contracts:
        _mock_vendor(_ok([_bar(0)], contract.occ_symbol), contract=contract.occ_symbol)
    listing = await asyncio.to_thread(read_option_listing, "SPY", TRADING_DAY)
    reads = _count_listing_reads(monkeypatch)

    results = [await ensure_option_day_bars(c, TRADING_DAY, listed_under="SPY", listing=listing) for c in contracts]

    assert [r.failure for r in results] == [None, None, None]
    assert reads == []


@respx.mock
async def test_without_a_listing_handed_in_each_capture_reads_the_stored_one_as_before(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contracts = [CONTRACT, OTHER_CONTRACT]
    await _list(*contracts)
    for contract in contracts:
        _mock_vendor(_ok([_bar(0)], contract.occ_symbol), contract=contract.occ_symbol)
    reads = _count_listing_reads(monkeypatch)

    results = [await ensure_option_day_bars(c, TRADING_DAY, listed_under="SPY") for c in contracts]

    assert [r.failure for r in results] == [None, None]
    assert reads == [("SPY", TRADING_DAY)] * 2


@respx.mock
async def test_a_listing_handed_in_that_does_not_hold_the_contract_refuses_it_with_no_row_and_no_vendor_call(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor(no_trades(SYMBOL))
    await _list(OTHER_CONTRACT)
    listing = await asyncio.to_thread(read_option_listing, "SPY", TRADING_DAY)

    result = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY", listing=listing)

    assert result.failure is not None and result.failure.reason == "not_listed"
    assert route.call_count == 0 and _bar_rows(fake_catalog) == []


@respx.mock
@pytest.mark.parametrize(
    ("underlying", "day"),
    [("QQQ", TRADING_DAY), ("SPY", date(2025, 6, 12))],
    ids=["another underlying", "another date"],
)
async def test_a_listing_handed_in_for_another_underlying_or_date_is_a_callers_mistake(
    underlying: str, day: date, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor(no_trades(SYMBOL))
    await _list(CONTRACT, day=day, underlying=underlying)
    wrong = await asyncio.to_thread(read_option_listing, underlying, day)

    with pytest.raises(ValueError, match="listing handed in"):
        await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY", listing=wrong)

    assert route.call_count == 0 and _bar_rows(fake_catalog) == []


# ── Option rows stay out of every stock answer ───────────────────────────


@respx.mock
async def test_an_option_row_does_not_appear_in_the_stock_coverage_answer(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor()
    await _ensure()

    # The query a stock capture and the coverage endpoint make: market "usa", this symbol or its underlying.
    for symbol in (SYMBOL, "SPY"):
        stock_view = await catalog_client.select_coverage_minute_bars(
            "usa", symbol, "trade", TRADING_DAY, TRADING_DAY, price_adjustment_mode="raw"
        )
        assert stock_view == []
    option_view = await catalog_client.select_coverage_minute_bars(
        "usa_options", SYMBOL, "trade", TRADING_DAY, TRADING_DAY, price_adjustment_mode="raw"
    )
    assert [r.symbol for r in option_view] == [SYMBOL]


# ── The recipe, the price grid and the strict reader ─────────────────────


def test_an_option_files_recipe_is_not_either_of_the_stock_recipes() -> None:
    """Observed option data and stock data never share an identity."""
    option = option_minute_trade_dch()

    assert option not in {
        _minute_trade_dch("raw", trading_date=date(2025, 6, 13)),
        _minute_trade_dch("raw", trading_date=date(2026, 9, 21)),  # fractional-volume era
        _minute_trade_dch("polygon_split_adjusted", trading_date=date(2025, 6, 13)),
    }
    assert option == option_minute_trade_dch()
    assert len(option) == 64


@pytest.mark.parametrize("price", ["0.0001", "0.01", "1.27", "4.35", "99.9999", "1234.5678"])
def test_every_price_on_the_ten_thousandth_grid_survives_the_round_trip_exactly(price: str) -> None:
    assert from_deci_cent(to_deci_cent(Decimal(price))) == Decimal(price)


def _zip(member: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, body)
    return buf.getvalue()


MEMBER = "20250613_spy250613c00600000_minute_trade.csv"


def test_a_well_formed_file_reads_to_its_bars() -> None:
    bars = parse_option_day_zip(_zip(MEMBER, "34200000,12700,13100,12500,12700,12\n"), SYMBOL, TRADING_DAY)

    assert bars == (
        OptionBar(
            start_ms=SESSION_OPEN_MS, open=Decimal("1.27"), high=Decimal("1.31"), low=Decimal("1.25"),
            close=Decimal("1.27"), volume=Decimal("12"),
        ),
    )


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ("34200000,12700,13100,12500,12700\n", 1),  # five columns
        ("34200000,1.27,13100,12500,12700,12\n", 1),  # a price that is not an integer count of ten-thousandths
        ("09:30,12700,13100,12500,12700,12\n", 1),  # a time that is not milliseconds
        ("34200000,12700,13100,12500,12700,1e3\n", 1),  # a volume the writer never writes
        ("34200000,12700,13100,12500,12700,12\n\n", 2),  # a blank line after the last row
        ("\n34200000,12700,13100,12500,12700,12\n", 1),  # a blank line before the first
        ("34200000,12700,13100,12500,12700,12\n\n34260000,12700,13100,12500,12700,12\n", 2),  # and between
        (" 34200000,12700,13100,12500,12700,12\n", 1),  # padding the writer never writes
        ("34200000,+12700,13100,12500,12700,12\n", 1),  # a sign
        ("34200000,1_2700,13100,12500,12700,12\n", 1),  # an underscore Python would accept
        ("34200000,-12700,13100,12500,12700,12\n", 1),  # a negative price
        ("-60000,12700,13100,12500,12700,12\n", 1),  # a negative start
        ("34200000," + "9" * 5000 + ",13100,12500,12700,12\n", 1),  # more digits than an integer will be made of
        ("9" * 30 + ",12700,13100,12500,12700,12\n", 1),  # a start past the end of the calendar
    ],
    ids=[
        "five columns", "decimal point", "clock time", "exponent volume", "blank after", "blank before", "blank between",
        "padding", "plus sign", "underscore", "negative price", "negative start", "5000 digits", "start past the calendar",
    ],
)
def test_a_malformed_row_is_an_error_not_a_skipped_bar(body: str, line: int) -> None:
    with pytest.raises(OptionBarsFileError, match=f"line {line}"):
        parse_option_day_zip(_zip(MEMBER, body), SYMBOL, TRADING_DAY)


def test_a_file_made_for_another_contract_is_refused() -> None:
    other = "20250613_spy250613c00601000_minute_trade.csv"

    with pytest.raises(OptionBarsFileError, match="expected one member"):
        parse_option_day_zip(_zip(other, "34200000,12700,13100,12500,12700,12\n"), SYMBOL, TRADING_DAY)


@pytest.mark.parametrize(
    ("ticks", "price"),
    [
        ("9007199254740993", "900719925474.0993"),  # 2**53 + 1: the first integer a float cannot hold
        ("12345678901234567", "1234567890123.4567"),  # seventeen digits
    ],
)
def test_a_stored_price_with_more_digits_than_a_float_holds_reads_back_exact(ticks: str, price: str) -> None:
    """Dividing through a float would give 900719925474.0992 and 1234567890123.4568: a price is never decided by one."""
    [bar] = parse_option_day_zip(_zip(MEMBER, f"34200000,{ticks},{ticks},{ticks},{ticks},1\n"), SYMBOL, TRADING_DAY)

    assert (bar.open, bar.high, bar.low, bar.close) == (Decimal(price),) * 4
    assert str(bar.open) == price


@pytest.mark.parametrize("extra_first", [False, True], ids=["extra last", "extra first"])
def test_a_file_with_a_member_besides_the_one_it_should_hold_is_refused(extra_first: bool) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        members = [(MEMBER, "34200000,12700,13100,12500,12700,12\n"), ("20250613_notes.txt", "anything")]
        for name, body in reversed(members) if extra_first else members:
            zf.writestr(name, body)

    with pytest.raises(OptionBarsFileError, match="expected one member"):
        parse_option_day_zip(buf.getvalue(), SYMBOL, TRADING_DAY)


def _written_file() -> bytes:
    """A real two-bar file, as the lake writes one."""
    et = ZoneInfo("America/New_York")
    bars = [
        MinuteTradeBar(
            bar_start_et=datetime(2025, 6, 13, 9, 30 + i, tzinfo=et), open=Decimal("1.27"), high=Decimal("1.31"),
            low=Decimal("1.25"), close=Decimal("1.27"), volume=Decimal(12),
        )
        for i in range(2)
    ]
    return build_minute_trade_zip_bytes(SYMBOL, "20250613", bars)


def _empty_zip() -> bytes:
    buf = io.BytesIO()
    zipfile.ZipFile(buf, "w").close()
    return buf.getvalue()


def _zip_of_bytes(body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(MEMBER, body)
    return buf.getvalue()


def _flipped(payload: bytes, index: int) -> bytes:
    return payload[:index] + bytes([payload[index] ^ 0xFF]) + payload[index + 1:]


@pytest.mark.parametrize(
    ("payload", "cause"),
    [
        (b"not a zip at all", zipfile.BadZipFile),
        (b"", zipfile.BadZipFile),
        (_written_file()[: len(_written_file()) // 2], zipfile.BadZipFile),
        (_flipped(_written_file(), 60), (zipfile.BadZipFile, zlib.error)),  # a byte of the compressed rows
        (_zip_of_bytes(b"34200000,12700,13100,12500,12700,12\n\xff\xfe"), UnicodeDecodeError),
        (_zip_of_bytes("34200000,12700,13100,12500,12700,12\n\u00e9".encode()), UnicodeDecodeError),
        (_empty_zip(), None),
    ],
    ids=["not a zip", "no bytes", "truncated", "damaged data", "bytes that are not text", "letters beyond ASCII", "no member"],
)
def test_a_stored_file_that_is_not_what_the_lake_writes_is_one_named_error_with_its_cause_chained(
    payload: bytes, cause: type[Exception] | tuple[type[Exception], ...] | None
) -> None:
    """Never a raw ``BadZipFile`` or ``UnicodeDecodeError``: a caller catches one thing."""
    with pytest.raises(OptionBarsFileError) as caught:
        parse_option_day_zip(payload, SYMBOL, TRADING_DAY)

    if cause is not None:
        assert isinstance(caught.value.__cause__, cause)


def test_no_single_damaged_byte_or_cut_of_a_written_file_escapes_as_anything_but_the_named_error() -> None:
    """Every cut, every byte zeroed or set, every bit flipped: the zip module has raised four other kinds of error for these."""
    written = _written_file()
    damaged = [written[:n] for n in range(len(written))]
    damaged += [written[:i] + bytes([b]) + written[i + 1:] for i in range(len(written)) for b in (0x00, 0xFF)]
    damaged += [written[:i] + bytes([written[i] ^ (1 << bit)]) + written[i + 1:] for i in range(len(written)) for bit in range(8)]

    for payload in damaged:
        with contextlib.suppress(OptionBarsFileError):  # a damaged byte may leave a file that still reads; nothing else may happen
            parse_option_day_zip(payload, SYMBOL, TRADING_DAY)


@pytest.mark.parametrize(
    "body",
    [
        "34200000,0,0,0,0,1\n",  # a price of zero
        "34200000,12700,13100,12500,0,12\n",  # a close of zero
        "34200000,12700,12500,13100,12700,12\n",  # the low above the high
        "34200000,12700,12600,12500,12700,12\n",  # the high below the open
        "34200000,12700,13100,12800,12700,12\n",  # the low above the open
        "34200000,12700,13100,12500,12700,12\n34200000,12700,13100,12500,12700,12\n",  # a start repeated
        "34260000,12700,13100,12500,12700,12\n34200000,12700,13100,12500,12700,12\n",  # starts out of order
        "86400000,12700,13100,12500,12700,12\n",  # the first millisecond of the next date
    ],
    ids=["zero price", "zero close", "low above high", "high below open", "low above open", "repeated start", "out of order", "next date"],
)
def test_a_stored_day_whose_bars_break_the_rules_of_a_stored_day_is_refused(body: str) -> None:
    with pytest.raises(OptionBarsFileError) as caught:
        parse_option_day_zip(_zip(MEMBER, body), SYMBOL, TRADING_DAY)

    assert isinstance(caught.value.__cause__, ValueError)


def _replace_the_stored_file(fake_catalog: FakeCatalog, tmp_lake: Path, record_id: int, payload: bytes) -> None:
    """Put other bytes in the day's file and give them the receipt, so the catalog admits them."""
    row = fake_catalog.rows[record_id]
    (_raw_root(tmp_lake) / row["file_path"]).write_bytes(payload)
    row["file_sha256"] = hashlib.sha256(payload).hexdigest()
    row["file_size_bytes"] = len(payload)


@respx.mock
async def test_an_admitted_file_that_is_no_zip_or_breaks_the_rules_of_a_stored_day_is_the_named_error_on_read_and_is_fetched_again(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_vendor()
    first = await _ensure()
    assert first.record is not None
    mangled = {
        "not a zip": b"admitted, but not a zip",
        "a zero price": _zip(MEMBER, "34200000,0,0,0,0,1\n"),
    }

    for name, payload in mangled.items():
        _replace_the_stored_file(fake_catalog, tmp_lake, first.record.id, payload)
        with pytest.raises(OptionBarsFileError):
            await _read()
        again = await _ensure()  # the reuse gate reads the file too: it does not hold, so the day is captured again

        assert again.failure is None and again.reused is False, name
        healed = await _read()
        assert healed is not None and len(healed.bars) == 1, name

    assert route.call_count == 1 + len(mangled)


# ── A price that would be stored as zero fails the day ───────────────────


def _one_price_bars(**prices: str) -> httpx.Response:
    """A two-bar answer: the second bar's prices are the given ones (any left out are 1.27 and the second bar's own)."""
    first = f'{{"t":{SESSION_OPEN_MS},"o":1.27,"h":1.31,"l":1.25,"c":1.27,"v":3}}'
    fields = {"o": "1.27", "h": "1.31", "l": "1.25", "c": "1.27", **prices}
    cells = ",".join(f'"{name}":{value}' for name, value in fields.items())
    second = f'{{"t":{SESSION_OPEN_MS + MINUTE_MS},{cells},"v":3}}'
    return raw_json(f'{{"status":"OK","ticker":"O:{SYMBOL}","resultsCount":2,"results":[{first},{second}]}}')


@respx.mock
@pytest.mark.parametrize(
    ("prices", "named"),
    [
        ({"o": "0.00004", "h": "0.00004", "l": "0.00004", "c": "0.00004"}, "open=0.00004"),
        ({"l": "0.00004"}, "low=0.00004"),
        ({"o": "0.00004", "l": "0.00004"}, "open=0.00004"),
        ({"c": "0.00004", "l": "0.00004"}, "low=0.00004"),  # the first of the offenders, in open, high, low, close order
    ],
    ids=["every price", "the low alone", "open and low", "close and low"],
)
async def test_a_vendor_price_so_small_it_rounds_to_a_stored_zero_fails_the_day_naming_the_bar(
    prices: dict[str, str], named: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The file holds ten-thousandths: 0.00004 is stored as 0, and every reader is told every price is above zero."""
    _mock_vendor(_one_price_bars(**prices))

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert "bar[1]" in result.failure.detail and named in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "validation_failed", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    assert await _read() is None


@respx.mock
async def test_the_smallest_price_that_rounds_up_to_one_ten_thousandth_is_stored_and_reads_back_as_that(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """0.00005 is half a ten-thousandth, which rounds up (half-up) to 1 tick: the edge of the refusal above."""
    _mock_vendor(_one_price_bars(o="0.00005", h="0.00005", l="0.00005", c="0.00005"))

    result = await _ensure()
    day = await _read()

    assert result.failure is None
    assert day is not None
    assert (day.bars[1].open, day.bars[1].low) == (Decimal("0.0001"), Decimal("0.0001"))



@respx.mock
@pytest.mark.parametrize(
    ("prices", "named", "as_floats"),
    [
        ({"o": "1.0", "h": "1.5", "l": "1.0000000000000001", "c": "1.2"}, "low 1.0000000000000001 > open 1.0", ("1.0", "1.0000000000000001")),
        ({"o": "1.5", "h": "2.0", "l": "1.20000000000000001", "c": "1.2"}, "low 1.20000000000000001 > close 1.2", ("1.2", "1.20000000000000001")),
        ({"o": "1.5", "h": "1.49999999999999999", "l": "1.0", "c": "1.2"}, "high 1.49999999999999999 < open 1.5", ("1.5", "1.49999999999999999")),
        ({"o": "1.0", "h": "1.49999999999999999", "l": "0.9", "c": "1.5"}, "high 1.49999999999999999 < close 1.5", ("1.5", "1.49999999999999999")),
    ],
    ids=["low above open", "low above close", "high below open", "high below close"],
)
async def test_a_bar_whose_low_or_high_is_out_of_place_by_less_than_a_float_holds_fails_the_day_naming_the_relation(
    prices: dict[str, str], named: str, as_floats: tuple[str, str], fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Open 1.0 and low 1.0000000000000001 are one number as floats, so the float gate alone passed such a bar and it was published."""
    assert float(as_floats[0]) == float(as_floats[1])  # the premise: the hairline is gone once the prices are floats
    _mock_vendor(_one_price_bars(**prices))

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert "bar[1]" in result.failure.detail and named in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "validation_failed", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    assert await _read() is None


@respx.mock
async def test_a_low_below_the_open_by_a_hairline_is_in_place_and_the_day_is_stored(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_vendor(_one_price_bars(o="1.0", h="1.5", l="0.99999999999999999", c="1.2"))

    result = await _ensure()

    assert result.failure is None and result.record is not None


# ── A bar that does not start on a whole minute fails the day ─────────────


@respx.mock
@pytest.mark.parametrize(
    "offset_ms",
    [1, 30_000, 59_999],
    ids=["a millisecond past the minute", "half a minute past it", "a millisecond short of the next"],
)
async def test_a_vendor_bar_that_does_not_start_on_a_whole_minute_fails_the_day_naming_the_bar(
    offset_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """A bar is named by its close, its start plus a minute, so a start between two minutes closes on no minute.

    The second bar starts offset_ms past 09:31, which is 1749821400000 + 60 000 = 1749821460000.
    """
    off_the_minute = SESSION_OPEN_MS + MINUTE_MS + offset_ms
    _mock_vendor(_ok([_bar(0), _bar(1, t=off_the_minute)]))

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert "bar[1]" in result.failure.detail and f"t_ms={off_the_minute}" in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "validation_failed", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    assert await _read() is None


@respx.mock
async def test_the_first_of_several_bars_off_the_minute_is_the_one_named(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """Bar 1 starts a millisecond past 09:31 (1749821460001) and bar 2 half a minute past 09:32 (1749821550000)."""
    _mock_vendor(
        _ok([_bar(0), _bar(1, t=SESSION_OPEN_MS + MINUTE_MS + 1), _bar(2, t=SESSION_OPEN_MS + 2 * MINUTE_MS + 30_000)])
    )

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert "bar[1] t_ms=1749821460001" in result.failure.detail and "bar[2]" not in result.failure.detail


@respx.mock
async def test_a_day_failed_for_a_bar_off_the_minute_is_asked_again_and_a_corrected_answer_is_stored(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The failure is the row's like any corrupt answer: within its attempts the vendor is asked again."""
    route = _mock_vendor(_ok([_bar(0), _bar(1, t=SESSION_OPEN_MS + MINUTE_MS + 30_000)]), _ok([_bar(0), _bar(1)]))

    first = await _ensure()
    second = await _ensure()

    assert first.failure is not None and first.failure.reason == "validation_failed"
    assert second.failure is None and second.record is not None and second.record.row_count == 2
    assert route.call_count == 2
    day = await _read()
    # Every bar on a whole minute is stored as it was sent: 09:30 and 09:31.
    assert day is not None and [bar.start_ms for bar in day.bars] == [SESSION_OPEN_MS, SESSION_OPEN_MS + MINUTE_MS]
    assert [row["status"] for row in _bar_rows(fake_catalog)] == ["complete"]


@respx.mock
@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "0", "-0.0", "-1.27"])
async def test_a_bar_with_a_price_that_is_not_finite_and_above_zero_fails_the_day_and_raises_nothing(
    price: str, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``Decimal`` NaN cannot be compared, so the price relations must not be asked of it."""

    async def fetch(*args: Any, **kwargs: Any) -> list[PolygonOptionBar]:
        return [
            PolygonOptionBar(
                t_ms=SESSION_OPEN_MS, open=Decimal("1.27"), high=Decimal("1.31"), low=Decimal(price), close=Decimal("1.27"),
                volume=Decimal(12),
            )
        ]

    monkeypatch.setattr(option_bars_module, "fetch_option_minute_bars", fetch)

    result = await _ensure()

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert f"low={Decimal(price)} is not finite and positive" in result.failure.detail
    [row] = _bar_rows(fake_catalog)
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "validation_failed", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
