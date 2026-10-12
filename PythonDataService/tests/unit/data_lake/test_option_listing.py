"""The contracts listed for an underlying as of a date, in the data lake (#3030).

Drives the real capture and read functions against the in-memory catalog and a
fake vendor. The Postgres-backed twin is ``test_option_lake_catalog.py``.

Hand-worked figures used below:

* A strike in dollars is stored in thousandths: 375 -> 375000, 452.5 -> 452500.
* The expiry is stored as ``YYYYMMDD``: 2025-06-13 -> ``20250613``.
* ``O:SPY250613C00375000`` is the SPY call struck at 375 expiring 2025-06-13.
* 2025-06-13 closes at 20:00 UTC = 1749844800000 ms. A listing is taken, like a session's
  bars, only from New York midnight after its date: 00:00 EDT on the 14th = 04:00 UTC =
  1749873600000 ms (``tests/_helpers/option_lake_cases.py`` holds the other dates, by hand).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import logging
import shutil
import zipfile
import zlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake import option_listing as option_listing_module
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_listing import (
    OptionListing,
    OptionListingFileError,
    build_option_listing_zip_bytes,
    captured_listing_dates,
    ensure_option_listing,
    option_listing_dch,
    parse_option_listing_zip,
    read_option_listing,
)
from app.data_lake.path_policy import lake_subpath
from app.data_lake.polygon_option_contracts import PolygonOptionContract
from app.data_lake.types import ArtifactIdentity
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, point_lake_writer_at_tmp
from tests._helpers.option_lake_cases import (
    CAPTURE_OPENS,
    HALF_DAY_CLOSE_MS,
    pieces_of_the_key_in,
    refuse_what_postgres_refuses,
)

AS_OF = date(2025, 6, 13)
SESSION_CLOSE_MS = 1749844800000
MINUTE_MS = 60_000
NOW_2026_10_12_MS = 1791828000000
CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"
SECOND_PAGE = f"{CONTRACTS_URL}?cursor=page2"
RELATIVE_PATH = "option/usa/listing/spy/20250613_contracts.zip"
MEMBER = "20250613_spy_contracts.csv"


@pytest.fixture(autouse=True)
def _the_clock_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every capture without its own ``now_ms`` runs on 2026-10-12, so no test here reads the wall clock."""
    monkeypatch.setattr(option_listing_module, "current_ms", lambda: NOW_2026_10_12_MS)


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def _row(ticker: str, strike: float | int, **overrides: Any) -> dict[str, Any]:
    row = {
        "ticker": ticker,
        "underlying_ticker": "SPY",
        "contract_type": "call",
        "strike_price": strike,
        "expiration_date": "2025-06-13",
        "exercise_style": "american",
        "shares_per_contract": 100,
        "primary_exchange": "BATO",
        "cfi": "OCASPS",
    }
    row.update(overrides)
    return row


STANDARD_ROWS = [
    _row("O:SPY250613C00375000", 375),
    _row("O:SPY250613C00376000", 376),
    _row("O:SPY250613P00400000", 400, contract_type="put"),
]


def _page(results: list[dict[str, Any]], next_url: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"status": "OK", "request_id": "r", "results": results}
    if next_url is not None:
        body["next_url"] = next_url
    return httpx.Response(200, json=body)


def _mock_one_page(results: list[dict[str, Any]]) -> respx.Route:
    return respx.get(CONTRACTS_URL).mock(return_value=_page(results))


def _mock_two_pages(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> tuple[respx.Route, respx.Route]:
    return (
        respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(return_value=_page(first, next_url=SECOND_PAGE)),
        respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page(second)),
    )


def _raw_root(tmp_lake: Path) -> Path:
    return tmp_lake / lake_subpath("raw")


def _lake_files(tmp_lake: Path) -> list[Path]:
    root = _raw_root(tmp_lake)
    return [p for p in root.rglob("*") if p.is_file() and p.name != ".data-root.json"] if root.exists() else []


def _staged_files(tmp_lake: Path) -> list[Path]:
    return [p for p in (tmp_lake / "staging").rglob("*") if p.is_file()]


async def _read(underlying: str = "SPY", as_of: date = AS_OF) -> OptionListing | None:
    return await asyncio.to_thread(read_option_listing, underlying, as_of)


def _csv_rows(payload: bytes) -> tuple[list[str], list[list[str]]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        rows = list(csv.reader(io.StringIO(zf.read(names[0]).decode())))
    return names, rows


def _vendor_contract(ticker: str, strike: str, **overrides: Any) -> PolygonOptionContract:
    fields: dict[str, Any] = {
        "ticker": ticker,
        "contract_type": "call",
        "strike": Decimal(strike),
        "expiration_date": "2025-06-13",
        "exercise_style": "american",
        "shares_per_contract": Decimal(100),
        "has_additional_underlyings": False,
    }
    fields.update(overrides)
    return PolygonOptionContract(**fields)


# ── The happy path ─────────────────────────────────────────────────────


@respx.mock
async def test_a_listing_over_two_pages_is_captured_whole_into_the_lake_and_the_catalog(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_two_pages(STANDARD_ROWS[:1], STANDARD_ROWS[1:])

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is None and result.reused is False
    record = result.record
    assert record is not None
    assert (record.artifact_kind, record.market, record.symbol, record.provider, record.price_adjustment_mode) == (
        "metadata", "usa_options", "SPY", "polygon", "raw",
    )
    assert (record.trading_date, record.resolution, record.data_type) == (None, None, None)
    assert record.row_count == 3
    assert record.file_path == RELATIVE_PATH
    assert record.data_contract_hash == option_listing_dch("SPY", AS_OF)
    assert fake_catalog.rows[record.id]["status"] == "complete"
    [file] = _lake_files(tmp_lake)
    assert file == _raw_root(tmp_lake) / RELATIVE_PATH
    assert hashlib.sha256(file.read_bytes()).hexdigest() == record.file_sha256
    assert _staged_files(tmp_lake) == []


@respx.mock
async def test_the_file_is_one_sorted_csv_of_the_vendors_facts(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    shuffled = [
        STANDARD_ROWS[2],
        _row("O:SPY250613C00452500", 452.5),
        STANDARD_ROWS[0],
        _row("O:SPY250613C00380000", 380, exercise_style="european", shares_per_contract=None),
        _row("O:SPY1250613C00375000", 375, additional_underlyings=[{"amount": 50, "type": "equity", "underlying": "XYZ"}]),
    ]
    _mock_one_page(shuffled)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is not None
    names, rows = _csv_rows((_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes())
    assert names == [MEMBER]
    assert rows[0] == [
        "ticker", "contract_type", "strike_thousandths", "expiry", "exercise_style", "shares_per_contract",
        "additional_underlyings",
    ]
    assert rows[1:] == [
        ["O:SPY1250613C00375000", "call", "375000", "20250613", "american", "100", "1"],
        ["O:SPY250613C00375000", "call", "375000", "20250613", "american", "100", "0"],
        ["O:SPY250613C00380000", "call", "380000", "20250613", "european", "", "0"],
        ["O:SPY250613C00452500", "call", "452500", "20250613", "american", "100", "0"],
        ["O:SPY250613P00400000", "put", "400000", "20250613", "american", "100", "0"],
    ]


@respx.mock
async def test_the_same_listing_in_any_order_makes_the_same_bytes(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_one_page(STANDARD_ROWS)
    first = await ensure_option_listing("SPY", AS_OF)
    first_bytes = (_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes()

    (tmp_path / "again").mkdir()
    second_lake = point_lake_writer_at_tmp(tmp_path / "again", monkeypatch)
    install_fake_catalog(monkeypatch)
    _mock_one_page(list(reversed(STANDARD_ROWS)))
    second = await ensure_option_listing("SPY", AS_OF)

    assert first.record is not None and second.record is not None
    assert (_raw_root(second_lake) / RELATIVE_PATH).read_bytes() == first_bytes
    assert second.record.file_sha256 == first.record.file_sha256


@respx.mock
async def test_a_listing_reads_back_with_its_rows_and_its_sha256(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page([*STANDARD_ROWS, _row("O:SPY250613C00452500", 452.5)])
    result = await ensure_option_listing("SPY", AS_OF)

    listing = await _read()

    assert listing is not None and result.record is not None
    assert (listing.underlying, listing.as_of) == ("SPY", AS_OF)
    assert listing.sha256 == result.record.file_sha256
    assert [row.ticker for row in listing.rows] == [
        "O:SPY250613C00375000", "O:SPY250613C00376000", "O:SPY250613C00452500", "O:SPY250613P00400000",
    ]
    put = listing.rows[3]
    assert (put.contract_type, put.strike_thousandths, put.exercise_style, put.shares_per_contract) == (
        "put", 400000, "american", 100,
    )
    assert put.has_additional_underlyings is False
    assert put.expiry_ms == OptionContract.from_occ_symbol("SPY250613P00400000").expiry_ms
    assert listing.rows[2].strike_thousandths == 452500  # 452.5 dollars, exact


async def test_a_date_never_captured_reads_as_not_captured(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    assert await _read() is None


# ── Which rows are standard ─────────────────────────────────────────────


@respx.mock
async def test_a_row_is_standard_only_if_american_100_shares_no_other_underlyings_and_a_symbol_that_names_it(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    rows = [
        _row("O:SPY250613C00375000", 375),  # standard
        _row("O:SPY250613C00376000", 376, exercise_style="european"),
        _row("O:SPY250613C00377000", 377, shares_per_contract=150),
        _row("O:SPY250613C00378000", 378, additional_underlyings=[{"amount": 1, "type": "equity", "underlying": "XYZ"}]),
        _row("O:SPY1250613C00379000", 379),  # an adjusted root carries a digit: not a symbol this system names
        _row("O:QQQ250613C00380000", 380),  # another underlying's contract in this underlying's listing
        _row("O:SPY250613C00381000", 381, contract_type="put"),  # symbol says call, the row says put
        _row("O:SPY250613C00382000", 383),  # symbol says 382, the row says 383
        _row("O:SPY250613C00384000", 384, expiration_date="2025-06-20"),  # symbol says 06-13
        {k: v for k, v in _row("O:SPY250613C00385000", 385).items() if k not in ("exercise_style", "shares_per_contract")},
    ]  # the last one: the vendor left both out
    _mock_one_page(rows)
    await ensure_option_listing("SPY", AS_OF)

    listing = await _read()

    assert listing is not None
    by_ticker = {row.ticker: row for row in listing.rows}
    standard = {t for t, row in by_ticker.items() if row.standard}
    assert standard == {"O:SPY250613C00375000"}
    # Non-standard rows are kept in the file and marked, not dropped.
    assert len(listing.rows) == 10
    assert by_ticker["O:SPY250613C00375000"].contract == OptionContract.from_occ_symbol("SPY250613C00375000")
    assert by_ticker["O:SPY1250613C00379000"].contract is None
    assert by_ticker["O:SPY250613C00381000"].contract is None
    assert by_ticker["O:SPY250613C00385000"].exercise_style is None
    assert by_ticker["O:SPY250613C00385000"].shares_per_contract is None


@respx.mock
async def test_a_dotted_underlying_is_compared_by_its_occ_root(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """``BRK.B`` options are written ``BRKB...``: the root is the letters of the underlying."""
    respx.get(CONTRACTS_URL).mock(
        return_value=_page([_row("O:BRKB250613C00500000", 500, underlying_ticker="BRK.B")])
    )
    result = await ensure_option_listing("BRK.B", AS_OF)
    assert result.record is not None and result.record.file_path == "option/usa/listing/brk.b/20250613_contracts.zip"

    listing = await _read("BRK.B")

    assert listing is not None and [row.standard for row in listing.rows] == [True]


# ── A listing is whole, or it is nothing ────────────────────────────────


@respx.mock
async def test_paging_that_breaks_part_way_admits_no_listing_and_a_later_capture_succeeds(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page(STANDARD_ROWS[:1], next_url=SECOND_PAGE)
    )
    broken = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=httpx.Response(502, text="bad"))

    failed = await ensure_option_listing("SPY", AS_OF)

    assert failed.record is None
    assert failed.failure is not None and failed.failure.reason == "provider_api_error"
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"]) == ("failed", "provider_api_error")
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    assert await _read() is None

    broken.mock(return_value=_page(STANDARD_ROWS[1:]))
    retried = await ensure_option_listing("SPY", AS_OF)

    assert retried.failure is None and retried.record is not None and retried.record.row_count == 3


@respx.mock
async def test_a_cursor_that_loops_admits_no_listing(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page(STANDARD_ROWS[:1], next_url=SECOND_PAGE)
    )
    respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page(STANDARD_ROWS[1:], next_url=SECOND_PAGE))

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is not None and result.failure.reason == "provider_api_error"
    assert "looped" in result.failure.detail
    assert _lake_files(tmp_lake) == []


@respx.mock
async def test_a_listing_with_no_contracts_is_not_pinned_as_a_fact(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_one_page([])

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "provider_no_data"
    assert _lake_files(tmp_lake) == []
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    ("rows", "fragment"),
    [
        ([STANDARD_ROWS[0], STANDARD_ROWS[0]], "duplicate"),
        ([_row("O:SPY250613C00375000", 375), _row("SPY250613C00375000", 375)], "duplicate"),  # O:X and X are one ticker
        ([_row("SPY250613C00375000", 375), _row("O:SPY250613C00375000", 375)], "duplicate"),
        ([STANDARD_ROWS[1], None], "not a JSON object"),
        ([STANDARD_ROWS[1], "O:SPY250613C00375000"], "not a JSON object"),
        ([STANDARD_ROWS[1], ["O:SPY250613C00375000"]], "not a JSON object"),
        ([STANDARD_ROWS[1], 375], "not a JSON object"),
        ([STANDARD_ROWS[1], {k: v for k, v in STANDARD_ROWS[0].items() if k != "ticker"}], "no usable ticker"),
        ([STANDARD_ROWS[1], _row("", 375)], "no usable ticker"),
        ([STANDARD_ROWS[1], _row("   ", 375)], "no usable ticker"),
        ([STANDARD_ROWS[1], _row(None, 375)], "no usable ticker"),  # type: ignore[arg-type]
        ([STANDARD_ROWS[1], _row(375, 375)], "no usable ticker"),  # type: ignore[arg-type]
    ],
)
async def test_a_row_that_is_no_object_or_has_no_ticker_or_repeats_one_fails_the_whole_listing_as_invalid(
    rows: list[dict[str, Any]], fragment: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_one_page(rows)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert fragment in result.failure.detail
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"]) == ("failed", "validation_failed")
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
async def test_a_ticker_listed_once_on_each_of_two_pages_is_still_listed_twice(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_two_pages([STANDARD_ROWS[0]], [_row("SPY250613C00375000", 375)])

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert "duplicate" in result.failure.detail


def _raw_body(text: str) -> httpx.Response:
    return httpx.Response(200, content=text.encode(), headers={"content-type": "application/json"})


@respx.mock
@pytest.mark.parametrize(
    "ticker_json",
    ['"O:SPY250613C00375000\\ud800"', '"O:SPY\\ud800250613C00375000"', '"O:SPY250613C00375000\u00e9"', '"O:SPY250613C00375000\U0001f4c8"'],
    ids=["a lone surrogate at the end", "a lone surrogate inside", "an accented letter", "an emoji"],
)
async def test_a_ticker_that_is_not_ascii_fails_the_listing_as_invalid_and_the_row_is_not_left_fetching(
    ticker_json: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """A lone surrogate (a JSON escape no UTF-8 holds) once escaped the file writer and left the row ``fetching``."""
    good = json.dumps(STANDARD_ROWS[1])
    respx.get(CONTRACTS_URL).mock(
        return_value=_raw_body('{"status":"OK","results":[' + good + ',{"ticker":' + ticker_json + ',"contract_type":"call"}]}')
    )

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert "no usable ticker" in result.failure.detail
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "validation_failed", None)
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


@respx.mock
@pytest.mark.parametrize(
    "second_page_results",
    ["", '"results":null,', '"results":{},', '"results":"x",', '"results":0,', '"results":false,'],
    ids=["missing", "null", "an object", "a string", "zero", "false"],
)
async def test_a_second_page_with_no_list_of_rows_admits_no_listing_so_a_cut_short_one_is_never_stored_as_whole(
    second_page_results: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Read as an empty last page, the first page alone became the day's listing and every other contract was "not listed" for good."""
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(
        return_value=_page(STANDARD_ROWS[:2], next_url=SECOND_PAGE)
    )
    second = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(
        return_value=_raw_body('{"status":"OK",' + second_page_results + '"request_id":"r"}')
    )

    failed = await ensure_option_listing("SPY", AS_OF)

    assert failed.record is None
    assert failed.failure is not None and failed.failure.reason == "provider_api_error"
    assert "not a list" in failed.failure.detail
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"]) == ("failed", "provider_api_error")
    assert _lake_files(tmp_lake) == [] and await _read() is None

    second.mock(return_value=_page(STANDARD_ROWS[2:]))
    retried = await ensure_option_listing("SPY", AS_OF)

    assert retried.failure is None and retried.record is not None and retried.record.row_count == 3


@respx.mock
async def test_a_first_page_with_no_rows_and_a_next_page_admits_no_listing(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "SPY"}).mock(return_value=_page([], next_url=SECOND_PAGE))
    second = respx.get(CONTRACTS_URL, params={"cursor": "page2"}).mock(return_value=_page(STANDARD_ROWS))

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "provider_api_error"
    assert "empty page with a next page" in result.failure.detail
    assert second.call_count == 0
    assert _lake_files(tmp_lake) == []


def _raw_page(rows: list[dict[str, Any]]) -> httpx.Response:
    """A page whose ``@@<number>@@`` strings are written as bare JSON numbers, and whose floats may be ``Infinity``/``NaN``."""
    body = json.dumps({"status": "OK", "request_id": "r", "results": rows})
    for token in ("1e400", "1e999999999", "1e-9"):
        body = body.replace(f'"@@{token}@@"', token)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _junk(field: str, value: Any) -> dict[str, Any]:
    """The probed standard row with one field changed; ``_MISSING`` takes the key out."""
    row = _row("O:SPY250613C00375000", 375)
    if value is _MISSING:
        del row[field]
    else:
        row[field] = value
    return row


_MISSING = object()
#: field -> (the file's column, the attribute a read-back row has it in, what an unreadable one reads back as)
_COLUMNS = {
    "contract_type": (1, "contract_type", ""),
    "strike_price": (2, "strike_thousandths", None),
    "expiration_date": (3, "expiry_ms", None),
    "exercise_style": (4, "exercise_style", None),
    "shares_per_contract": (5, "shares_per_contract", None),
}
_UNREADABLE = [
    ("strike_price", _MISSING), ("strike_price", None), ("strike_price", 0), ("strike_price", -5), ("strike_price", 0.0),
    ("strike_price", "375"), ("strike_price", True), ("strike_price", [375]), ("strike_price", float("inf")),
    ("strike_price", float("-inf")), ("strike_price", float("nan")), ("strike_price", 375.0005),
    ("strike_price", "@@1e400@@"), ("strike_price", "@@1e999999999@@"), ("strike_price", "@@1e-9@@"),
    ("shares_per_contract", _MISSING), ("shares_per_contract", None), ("shares_per_contract", 0), ("shares_per_contract", -100),
    ("shares_per_contract", 100.5), ("shares_per_contract", "100"), ("shares_per_contract", True),
    ("shares_per_contract", float("inf")), ("shares_per_contract", float("nan")),
    ("shares_per_contract", "@@1e400@@"), ("shares_per_contract", "@@1e999999999@@"),
    ("contract_type", _MISSING), ("contract_type", None), ("contract_type", ""), ("contract_type", "other"), ("contract_type", 7),
    ("exercise_style", _MISSING), ("exercise_style", None), ("exercise_style", ""), ("exercise_style", "weird"), ("exercise_style", 7),
    ("expiration_date", _MISSING), ("expiration_date", None), ("expiration_date", ""), ("expiration_date", "2025-13-45"),
    ("expiration_date", "20250613"), ("expiration_date", "2025-6-13"), ("expiration_date", 20250613),
    ("expiration_date", "2025-06-13T00:00:00"), ("expiration_date", "٢٠٢٥-٠٦-١٣"),
]


@respx.mock
@pytest.mark.parametrize(("field", "value"), _UNREADABLE, ids=[f"{f}={'missing' if v is _MISSING else v!r}" for f, v in _UNREADABLE])
async def test_a_field_the_file_cannot_hold_is_stored_empty_and_the_row_is_kept_and_not_standard(
    field: str, value: Any, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    other = _row("O:SPY250613C00376000", 376)
    _mock_one_page_raw([_junk(field, value), other])

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is None and result.record is not None  # it never fails the listing, and nothing escapes
    assert result.record.row_count == 2
    column, attribute, empty = _COLUMNS[field]
    _, cells = _csv_rows((_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes())
    kept = next(row for row in cells[1:] if row[0] == "O:SPY250613C00375000")
    assert kept[column] == ""
    listing = await _read()
    assert listing is not None
    by_ticker = {row.ticker: row for row in listing.rows}
    assert getattr(by_ticker["O:SPY250613C00375000"], attribute) == empty
    assert by_ticker["O:SPY250613C00375000"].standard is False
    assert by_ticker["O:SPY250613C00376000"].standard is True  # its neighbour is untouched


def _mock_one_page_raw(rows: list[dict[str, Any]]) -> respx.Route:
    return respx.get(CONTRACTS_URL).mock(return_value=_raw_page(rows))


@respx.mock
async def test_a_row_with_nothing_but_a_ticker_is_kept_with_every_other_cell_empty(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page([{"ticker": "O:SPY250613C00375000"}])

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is None and result.record is not None
    _, cells = _csv_rows((_raw_root(tmp_lake) / RELATIVE_PATH).read_bytes())
    assert cells[1] == ["O:SPY250613C00375000", "", "", "", "", "", "0"]
    listing = await _read()
    assert listing is not None
    [row] = listing.rows
    assert (row.contract_type, row.strike_thousandths, row.expiry_ms, row.exercise_style, row.shares_per_contract) == (
        "", None, None, None, None,
    )
    assert row.standard is False and row.contract is None


@respx.mock
async def test_a_standard_row_exactly_as_the_vendor_sends_it_is_stored_whole_and_standard(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    """The probed row: nine keys, and no ``additional_underlyings``. Absent means none."""
    probed = {
        "cfi": "OCASPS", "contract_type": "call", "exercise_style": "american", "expiration_date": "2025-06-13",
        "primary_exchange": "BATO", "shares_per_contract": 100, "strike_price": 375, "ticker": "O:SPY250613C00375000",
        "underlying_ticker": "SPY",
    }
    assert "additional_underlyings" not in probed
    _mock_one_page([probed])

    await ensure_option_listing("SPY", AS_OF)

    listing = await _read()
    assert listing is not None
    [row] = listing.rows
    assert row.has_additional_underlyings is False and row.standard is True
    assert (row.strike_thousandths, row.exercise_style, row.shares_per_contract) == (375000, "american", 100)


@respx.mock
async def test_a_value_in_another_case_is_read_as_the_value_it_names(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page([_row("O:SPY250613C00375000", 375, contract_type="CALL", exercise_style="American")])

    await ensure_option_listing("SPY", AS_OF)

    listing = await _read()
    assert listing is not None
    [row] = listing.rows
    assert (row.contract_type, row.exercise_style, row.standard) == ("call", "american", True)


@respx.mock
@pytest.mark.parametrize("written", ["call", "CALL", "Call", "put", "PUT", "Put"])
async def test_a_call_or_put_reads_back_right_whatever_case_the_vendor_wrote_it_in(
    written: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    right = written.lower()
    ticker = "O:SPY250613C00375000" if right == "call" else "O:SPY250613P00375000"
    _mock_one_page([_row(ticker, 375, contract_type=written)])
    await ensure_option_listing("SPY", AS_OF)

    listing = await _read()

    assert listing is not None
    [row] = listing.rows
    assert (row.contract_type, row.standard) == (right, True)
    assert row.contract is not None and row.contract.right == right


@pytest.mark.parametrize("stored", ["CALL", "Call", "call"])
def test_a_call_or_put_stored_in_any_case_is_read_as_the_right_it_names(stored: str) -> None:
    """The reader says what the cell names, so a file made by hand or by another version reads the same."""
    csv_text = (
        "ticker,contract_type,strike_thousandths,expiry,exercise_style,shares_per_contract,additional_underlyings\n"
        f"O:SPY250613C00375000,{stored},375000,20250613,AMERICAN,100,0\n"
    )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr(MEMBER, csv_text)

    [row] = parse_option_listing_zip(payload.getvalue(), "SPY", AS_OF)

    assert (row.contract_type, row.exercise_style, row.standard) == ("call", "american", True)


@respx.mock
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(401, text="no key"), "provider_auth_error"),
        (httpx.Response(403, text="not entitled"), "provider_entitlement_error"),
        (httpx.Response(429), "provider_rate_limited"),
        (httpx.Response(500, text="boom"), "provider_api_error"),
    ],
)
async def test_each_vendor_error_fails_the_row_with_its_own_reason(
    response: httpx.Response, reason: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    respx.get(CONTRACTS_URL).mock(return_value=response)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is not None and result.failure.reason == reason
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"]) == ("failed", reason)


@respx.mock
@pytest.mark.parametrize(
    "key",
    ["", "  \n", "Zq9xV7wK\u00e9", "Zq9xV7wK Zq9xV7wK", "Zq9xV7wK\x00", "Zq9xV7wK\ud800"],
    ids=["empty", "only whitespace", "accented letter", "inner space", "nul", "lone surrogate"],
)
async def test_a_key_that_cannot_be_sent_fails_the_listing_row_as_an_auth_error_and_leaves_none_fetching(
    key: str, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    route = _mock_one_page(STANDARD_ROWS)
    stored = refuse_what_postgres_refuses(monkeypatch)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", key)
    caplog.set_level(logging.DEBUG)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "provider_auth_error"
    assert "malformed" in result.failure.detail
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "provider_auth_error", None)
    assert route.call_count == 0
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []
    shown = [result.failure.detail, *stored, *(r.getMessage() + repr(r.__dict__) for r in caplog.records)]
    assert pieces_of_the_key_in(key.strip(), *shown) == set()


@respx.mock
async def test_a_key_with_a_trailing_newline_captures_a_listing_sent_as_its_stripped_self(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = _mock_one_page(STANDARD_ROWS)
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "sentinel-key-4f9a1c\n")

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is None and result.record is not None
    assert route.calls[0].request.headers["Authorization"] == "Bearer sentinel-key-4f9a1c"


@respx.mock
async def test_a_vendor_error_text_with_a_nul_is_stored_without_it_and_the_listing_row_is_failed_not_left_fetching(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = refuse_what_postgres_refuses(monkeypatch)
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(500, content=b"boom\x00boom"))

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is not None and result.failure.reason == "provider_api_error"
    assert "boomboom" in result.failure.detail and "\x00" not in result.failure.detail
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"], row["lease_owner"]) == ("failed", "provider_api_error", None)
    assert stored == [result.failure.detail]


@respx.mock
async def test_reference_data_has_no_rolling_window_so_an_old_403_is_a_real_entitlement_problem(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Listings reach back before the price floor (probed: as of 2024-06-14 answered)."""
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(403, text="not entitled"))

    result = await ensure_option_listing("SPY", date(2024, 6, 14), now_ms=NOW_2026_10_12_MS)

    assert result.failure is not None and result.failure.reason == "provider_entitlement_error"


@respx.mock
async def test_a_403_that_names_the_data_timeframe_is_outside_the_plan_for_a_listing_too_and_is_asked_again(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The vendor's own word, whatever the date; and, like a session's bars, never latched into a timeout."""
    route = respx.get(CONTRACTS_URL).mock(
        return_value=httpx.Response(
            403,
            json={"status": "NOT_AUTHORIZED", "request_id": "r", "message": "Your plan doesn't include this data timeframe. Please upgrade your plan at https://polygon.io/pricing"},
        )
    )

    results = [await ensure_option_listing("SPY", AS_OF) for _ in range(5)]

    assert [r.failure.reason for r in results if r.failure is not None] == ["outside_data_plan"] * 5
    assert route.call_count == 5


# ── Refusals cost no claim ──────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize(("session", "opens_ms"), [(day, ms) for _, day, ms in CAPTURE_OPENS], ids=[label for label, _, _ in CAPTURE_OPENS])
async def test_a_listing_is_refused_until_new_york_midnight_after_its_session_and_taken_from_that_midnight(
    session: date, opens_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """The rule a session's bars follow: one millisecond before the midnight is refused, the midnight is taken."""
    route = _mock_one_page(STANDARD_ROWS)

    refused = await ensure_option_listing("SPY", session, now_ms=opens_ms - 1)

    assert refused.record is None
    assert refused.failure is not None and refused.failure.reason == "session_not_over"
    assert route.call_count == 0 and fake_catalog.rows == {}  # refused before any claim

    allowed = await ensure_option_listing("SPY", session, now_ms=opens_ms)

    assert allowed.failure is None and allowed.record is not None
    assert route.call_count == 1
    assert len(fake_catalog.rows) == 1  # only the allowed ask made a row


@respx.mock
@pytest.mark.parametrize(
    ("session", "close_ms"),
    [(AS_OF, SESSION_CLOSE_MS), *HALF_DAY_CLOSE_MS.items()],
    ids=["a full day", "a half day in november", "a half day in july"],
)
async def test_thirty_minutes_after_the_close_is_no_longer_a_time_to_capture_a_listing(
    session: date, close_ms: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_one_page(STANDARD_ROWS)

    at_the_old_time = await ensure_option_listing("SPY", session, now_ms=close_ms + 30 * MINUTE_MS)
    that_evening = await ensure_option_listing("SPY", session, now_ms=close_ms + 6 * 60 * MINUTE_MS)

    assert at_the_old_time.failure is not None and at_the_old_time.failure.reason == "session_not_over"
    assert that_evening.failure is not None and that_evening.failure.reason == "session_not_over"
    assert route.call_count == 0 and fake_catalog.rows == {}


@respx.mock
async def test_a_listing_for_a_day_the_exchange_was_closed_is_refused(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_one_page(STANDARD_ROWS)

    result = await ensure_option_listing("SPY", date(2025, 6, 14))

    assert result.failure is not None and result.failure.reason == "not_a_session"
    assert route.call_count == 0 and fake_catalog.rows == {}


async def test_an_underlying_longer_than_the_catalogs_twenty_characters_is_refused_before_any_claim(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await ensure_option_listing("A" * 21, AS_OF)

    assert result.failure is not None and result.failure.reason == "symbol_too_long"
    assert fake_catalog.rows == {}


@pytest.mark.parametrize("bad", ["spy", "", "SPY/QQQ", "1SPY"])
async def test_an_underlying_that_is_not_a_ticker_is_a_caller_mistake(bad: str, fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    with pytest.raises(ValueError, match="underlying"):
        await ensure_option_listing(bad, AS_OF)
    assert fake_catalog.rows == {}


@respx.mock
@pytest.mark.parametrize("bad", ["spy", "Spy", "", "SPY/QQQ", "1SPY", "../SPY"])
async def test_an_underlying_must_be_spelled_as_the_lake_spells_it_on_read_as_on_write(
    bad: str, fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``spy`` finds SPY's folder, and every row of it would read as non-standard; it is a mistake, said loudly."""
    _mock_one_page(STANDARD_ROWS)
    await ensure_option_listing("SPY", AS_OF)
    asked: list[tuple] = []
    real = fake_catalog.select_complete_metadata_artifacts

    async def counting(*args: Any, **kwargs: Any):
        asked.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(catalog_client, "select_complete_metadata_artifacts", counting)

    with pytest.raises(ValueError, match="underlying"):
        await _read(bad)
    with pytest.raises(ValueError, match="underlying"):
        await captured_listing_dates(bad, AS_OF, AS_OF)

    assert asked == []  # a mistake costs no catalog question


# ── A lost claim ─────────────────────────────────────────────────────────


@respx.mock
async def test_a_second_capture_reuses_the_complete_row_without_asking_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_one_page(STANDARD_ROWS)
    first = await ensure_option_listing("SPY", AS_OF)

    second = await ensure_option_listing("SPY", AS_OF)

    assert route.call_count == 1
    assert second.reused is True and second.record == first.record
    assert len(fake_catalog.rows) == 1


@respx.mock
async def test_a_complete_row_whose_file_has_gone_is_fetched_again(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_one_page(STANDARD_ROWS)
    first = await ensure_option_listing("SPY", AS_OF)
    (_raw_root(tmp_lake) / RELATIVE_PATH).unlink()

    again = await ensure_option_listing("SPY", AS_OF)

    assert route.call_count == 2
    assert again.failure is None and again.reused is False
    assert first.record is not None and again.record is not None and again.record.id == first.record.id
    assert (_raw_root(tmp_lake) / RELATIVE_PATH).is_file()


@respx.mock
async def test_a_row_another_worker_is_still_fetching_is_left_alone(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = _mock_one_page(STANDARD_ROWS)
    first = await ensure_option_listing("SPY", AS_OF)
    assert first.record is not None
    fake_catalog.rows[first.record.id].update(status="fetching", lease_owner="someone-else", lease_expires_at_ms=10**15)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.failure is not None and result.failure.reason == "lease_timeout"
    assert route.call_count == 1


@respx.mock
async def test_a_failed_row_that_spent_its_retries_is_a_timeout(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    route = respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(500, text="boom"))
    for _ in range(3):
        await ensure_option_listing("SPY", AS_OF)

    exhausted = await ensure_option_listing("SPY", AS_OF)

    assert exhausted.failure is not None and exhausted.failure.reason == "fetch_timeout"
    assert route.call_count == 3


@respx.mock
async def test_a_writer_that_lost_its_lease_publishes_nothing_and_does_not_fail_the_row(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch_then_lose_the_lease(*args: Any, **kwargs: Any) -> list[PolygonOptionContract]:
        [row] = fake_catalog.rows.values()
        row["lease_generation"] += 1
        row["lease_owner"] = "the-winner"
        return [_vendor_contract("O:SPY250613C00375000", "375")]

    monkeypatch.setattr(option_listing_module, "fetch_option_contracts", fetch_then_lose_the_lease)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "lease_timeout"
    [row] = fake_catalog.rows.values()
    assert row["status"] == "fetching" and row["lease_owner"] == "the-winner"
    assert _lake_files(tmp_lake) == [] and _staged_files(tmp_lake) == []


def test_a_listings_identity_is_its_provider_endpoint_underlying_and_date() -> None:
    base = option_listing_dch("SPY", AS_OF)

    assert option_listing_dch("SPY", AS_OF) == base
    assert option_listing_dch("QQQ", AS_OF) != base
    assert option_listing_dch("SPY", date(2025, 6, 12)) != base
    assert len(base) == 64


# ── A new file format stales the old format's row ───────────────────────


@respx.mock
async def test_capturing_a_date_again_under_a_new_file_format_leaves_one_complete_row_for_its_file(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-12"}).mock(return_value=_page(STANDARD_ROWS))
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-13"}).mock(return_value=_page(STANDARD_ROWS))
    other_date = await ensure_option_listing("SPY", date(2025, 6, 12))
    old = await ensure_option_listing("SPY", AS_OF)
    monkeypatch.setattr(option_listing_module, "OPTION_LISTING_FILE_FORMAT", option_listing_module.OPTION_LISTING_FILE_FORMAT + 1)

    new = await ensure_option_listing("SPY", AS_OF)

    assert old.record is not None and new.record is not None and other_date.record is not None
    assert new.record.id != old.record.id  # a new recipe is a new row, not the old one rebuilt
    assert fake_catalog.rows[old.record.id]["status"] == "stale"
    assert fake_catalog.rows[new.record.id]["status"] == "complete"
    complete_for_the_file = [
        row for row in fake_catalog.rows.values() if row["file_path"] == RELATIVE_PATH and row["status"] == "complete"
    ]
    assert len(complete_for_the_file) == 1
    assert fake_catalog.rows[other_date.record.id]["status"] == "complete"  # another date's file is another file


@respx.mock
async def test_a_listing_asked_for_again_under_the_same_format_keeps_its_row_complete(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page(STANDARD_ROWS)
    first = await ensure_option_listing("SPY", AS_OF)
    (_raw_root(tmp_lake) / RELATIVE_PATH).unlink()

    again = await ensure_option_listing("SPY", AS_OF)  # the file went, so the same row is refreshed and published again

    assert first.record is not None and again.record is not None and again.record.id == first.record.id
    assert fake_catalog.rows[again.record.id]["status"] == "complete"


# ── Reading is only through admission ───────────────────────────────────


async def test_a_file_the_catalog_never_admitted_is_refused_not_read(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    path = _raw_root(tmp_lake) / RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")

    with pytest.raises(LakeAdmissionError):
        await _read()


@respx.mock
async def test_bytes_that_changed_after_their_receipt_are_refused_not_read(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page(STANDARD_ROWS)
    await ensure_option_listing("SPY", AS_OF)
    (_raw_root(tmp_lake) / RELATIVE_PATH).write_bytes(b"tampered")

    with pytest.raises(LakeAdmissionError):
        await _read()


@respx.mock
async def test_what_a_caller_must_have_to_read_a_listing_is_what_the_reader_says(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread with no event loop, the catalog reachable, the root's marker: each missing is its own error."""
    _mock_one_page(STANDARD_ROWS)
    await ensure_option_listing("SPY", AS_OF)

    assert read_option_listing("SPY", date(2025, 6, 12)) is None  # on the loop's thread: no file, so nothing to admit
    with pytest.raises(RuntimeError, match="worker thread"):
        read_option_listing("SPY", AS_OF)

    async def down(*args: Any, **kwargs: Any) -> bool:
        raise catalog_client.CatalogUnavailableError("down")

    with monkeypatch.context() as outage:
        outage.setattr(catalog_client, "has_committed_file_receipt", down)
        with pytest.raises(catalog_client.CatalogUnavailableError):
            await _read()

    (tmp_lake / "lake" / ".data-root.json").unlink()
    with pytest.raises(LakeAdmissionError):
        await _read()


@respx.mock
async def test_a_listing_is_read_from_the_lake_root_it_is_handed_and_the_configured_root_is_not_touched(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path
) -> None:
    """A snapshot or an alternate mount of the lake: the same relative path, the same receipt."""
    _mock_one_page(STANDARD_ROWS)
    result = await ensure_option_listing("SPY", AS_OF)
    mount = tmp_path / "mount"
    shutil.copytree(tmp_lake / "lake", mount / "lake")
    shutil.rmtree(_raw_root(tmp_lake) / "option")  # the configured root no longer holds the listing

    configured = await _read()
    from_the_mount = await asyncio.to_thread(read_option_listing, "SPY", AS_OF, lake_root=mount / "lake" / "raw")

    assert result.record is not None
    assert configured is None
    assert from_the_mount is not None and len(from_the_mount.rows) == 3
    assert from_the_mount.sha256 == result.record.file_sha256


@pytest.mark.parametrize("where", ["somewhere", "raw", "lake", "lake/polygon_split_adjusted", "lake/raw/option"])
async def test_a_lake_root_that_is_not_a_raw_lake_root_is_refused_rather_than_read_without_admission(
    where: str, fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path
) -> None:
    """The default admission lets a path outside the lake through unchecked, so such a root must never reach it."""
    loose = tmp_path / "loose" / where
    listing_file = loose / RELATIVE_PATH
    listing_file.parent.mkdir(parents=True)
    listing_file.write_bytes(build_option_listing_zip_bytes("SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375")]))

    with pytest.raises(ValueError, match="lake_root"):
        await asyncio.to_thread(read_option_listing, "SPY", AS_OF, lake_root=loose)


# ── The file itself ─────────────────────────────────────────────────────


def test_building_the_file_refuses_a_duplicate_ticker_rather_than_dropping_one() -> None:
    contract = _vendor_contract("O:SPY250613C00375000", "375")

    with pytest.raises(OptionListingFileError, match="duplicate"):
        build_option_listing_zip_bytes("SPY", AS_OF, [contract, contract])


def test_building_the_file_treats_o_x_and_x_as_one_ticker() -> None:
    with pytest.raises(OptionListingFileError, match="duplicate"):
        build_option_listing_zip_bytes(
            "SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375"), _vendor_contract("SPY250613C00375000", "375")]
        )


def test_a_strike_finer_than_a_thousandth_is_stored_empty_not_rounded() -> None:
    payload = build_option_listing_zip_bytes("SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375.0005")])

    _, rows = _csv_rows(payload)
    assert rows[1][2] == ""


def test_a_listing_file_that_is_not_the_one_written_is_refused_on_read() -> None:
    wrong_header = io.BytesIO()
    with zipfile.ZipFile(wrong_header, "w") as zf:
        zf.writestr(MEMBER, "ticker,strike\nO:SPY250613C00375000,375\n")
    wrong_member = build_option_listing_zip_bytes("SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375")])

    with pytest.raises(OptionListingFileError, match="header"):
        parse_option_listing_zip(wrong_header.getvalue(), "SPY", AS_OF)
    with pytest.raises(OptionListingFileError, match="expected one member"):
        parse_option_listing_zip(wrong_member, "QQQ", AS_OF)


def _written_listing() -> bytes:
    return build_option_listing_zip_bytes(
        "SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375"), _vendor_contract("O:SPY250613C00376000", "376")]
    )


def _listing_zip_of(body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(MEMBER, body)
    return buf.getvalue()


_LISTING_HEADER = b"ticker,contract_type,strike_thousandths,expiry,exercise_style,shares_per_contract,additional_underlyings\n"


@pytest.mark.parametrize(
    ("payload", "cause"),
    [
        (b"not a zip at all", zipfile.BadZipFile),
        (b"", zipfile.BadZipFile),
        (_written_listing()[: len(_written_listing()) // 2], zipfile.BadZipFile),
        (_written_listing()[:30 + len(MEMBER)] + b"\xff" * 10 + _written_listing()[40 + len(MEMBER):], zlib.error),
        (_listing_zip_of(_LISTING_HEADER + b"O:SPY250613C00375000,call,375000,20250613,american,100,0\n\xff\xfe"), UnicodeDecodeError),
        (_listing_zip_of(_LISTING_HEADER + b"O:SPY250613C00375000,call," + b"9" * 200_000 + b",20250613,american,100,0\n"), None),
        (_listing_zip_of(_LISTING_HEADER + b"O:SPY250613C00375000,call,375000,20251345,american,100,0\n"), ValueError),
    ],
    ids=["not a zip", "no bytes", "truncated", "damaged data", "bytes that are not text", "a cell too large for the csv reader", "a date that is no date"],
)
def test_a_stored_listing_that_is_not_what_the_lake_writes_is_one_named_error(
    payload: bytes, cause: type[Exception] | None
) -> None:
    """Never a raw ``BadZipFile``, ``zlib.error`` or ``UnicodeDecodeError``: a caller catches one thing."""
    with pytest.raises(OptionListingFileError) as caught:
        parse_option_listing_zip(payload, "SPY", AS_OF)

    if cause is not None:
        assert isinstance(caught.value.__cause__, cause)


def test_no_single_damaged_byte_or_cut_of_a_written_listing_escapes_as_anything_but_the_named_error() -> None:
    written = _written_listing()
    damaged = [written[:n] for n in range(len(written))]
    damaged += [written[:i] + bytes([b]) + written[i + 1:] for i in range(len(written)) for b in (0x00, 0xFF)]
    damaged += [written[:i] + bytes([written[i] ^ (1 << bit)]) + written[i + 1:] for i in range(len(written)) for bit in range(8)]

    for payload in damaged:
        with contextlib.suppress(OptionListingFileError):  # a damaged byte may leave a listing that still reads; nothing else may happen
            parse_option_listing_zip(payload, "SPY", AS_OF)


# ── Which as-of dates are captured ─────────────────────────────────────


@respx.mock
async def test_the_captured_as_of_dates_in_a_range_come_from_one_catalog_query(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-12"}).mock(return_value=_page(STANDARD_ROWS))
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-13"}).mock(return_value=_page(STANDARD_ROWS))
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-16"}).mock(return_value=httpx.Response(500, text="boom"))
    for day in (date(2025, 6, 12), date(2025, 6, 13), date(2025, 6, 16)):
        await ensure_option_listing("SPY", day)
    respx.get(CONTRACTS_URL, params={"underlying_ticker": "QQQ"}).mock(return_value=_page([_row("O:QQQ250613C00500000", 500)]))
    await ensure_option_listing("QQQ", date(2025, 6, 17))  # another underlying, a date in the range
    asked: list[tuple] = []
    real = fake_catalog.select_complete_metadata_artifacts

    async def counting(*args: Any, **kwargs: Any):
        asked.append((args, kwargs))
        return await real(*args, **kwargs)

    monkeypatch.setattr(catalog_client, "select_complete_metadata_artifacts", counting)

    captured = await captured_listing_dates("SPY", date(2025, 6, 11), date(2025, 6, 20))

    # 06-12 and 06-13 are held; 06-16 failed; 06-14/15 are a weekend; 06-19 is Juneteenth; QQQ's date is not SPY's.
    assert captured == (date(2025, 6, 12), date(2025, 6, 13))
    assert len(asked) == 1


async def _claim_and_leave_fetching(underlying: str, day: date) -> None:
    """A listing row some worker is still fetching: claimed, never completed."""
    identity = ArtifactIdentity(
        artifact_kind="metadata", market="usa_options", symbol=underlying, provider="polygon", price_adjustment_mode="raw"
    )
    claimed = await catalog_client.claim_metadata_artifact(
        identity=identity, worker_id="w", lease_ttl_ms=300_000, data_contract_hash=option_listing_dch(underlying, day),
        file_path="x.zip",
    )
    assert claimed is not None


@respx.mock
async def test_the_captured_dates_include_both_ends_of_the_range_and_nothing_outside_it_and_only_complete_listings(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """06-10 and 06-18 are captured and sit one day outside the range; 06-12 failed and 06-16 is still being fetched."""
    for day in (date(2025, 6, 10), date(2025, 6, 11), date(2025, 6, 13), date(2025, 6, 17), date(2025, 6, 18)):
        respx.get(CONTRACTS_URL, params={"as_of": day.isoformat()}).mock(return_value=_page(STANDARD_ROWS))
        assert (await ensure_option_listing("SPY", day)).failure is None
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-12"}).mock(return_value=httpx.Response(500, text="boom"))
    assert (await ensure_option_listing("SPY", date(2025, 6, 12))).failure is not None
    await _claim_and_leave_fetching("SPY", date(2025, 6, 16))

    captured = await captured_listing_dates("SPY", date(2025, 6, 11), date(2025, 6, 17))

    assert captured == (date(2025, 6, 11), date(2025, 6, 13), date(2025, 6, 17))


@respx.mock
async def test_a_range_of_one_day_answers_for_that_day_alone(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page(STANDARD_ROWS)
    await ensure_option_listing("SPY", AS_OF)

    assert await captured_listing_dates("SPY", AS_OF, AS_OF) == (AS_OF,)
    assert await captured_listing_dates("SPY", date(2025, 6, 12), date(2025, 6, 12)) == ()
    assert await captured_listing_dates("SPY", date(2025, 6, 16), date(2025, 6, 16)) == ()


@respx.mock
async def test_a_range_outside_what_was_captured_is_empty(fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
    _mock_one_page(STANDARD_ROWS)
    await ensure_option_listing("SPY", AS_OF)

    assert await captured_listing_dates("SPY", date(2025, 7, 1), date(2025, 7, 31)) == ()


# ── Listing rows stay out of every stock answer ─────────────────────────


@respx.mock
async def test_a_listing_row_does_not_appear_in_a_stock_symbols_metadata_or_coverage(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_one_page(STANDARD_ROWS)
    await ensure_option_listing("SPY", AS_OF)

    stock_view = await catalog_client.select_complete_metadata_artifacts("usa", "SPY", "polygon", "raw")
    option_view = await catalog_client.select_complete_metadata_artifacts("usa_options", "SPY", "polygon", "raw")
    coverage = await catalog_client.select_coverage_minute_bars("usa", "SPY", "trade", AS_OF, AS_OF, price_adjustment_mode="raw")

    assert stock_view == [] and coverage == []
    assert [r.symbol for r in option_view] == ["SPY"]
