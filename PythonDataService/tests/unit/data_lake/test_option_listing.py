"""The contracts listed for an underlying as of a date, in the data lake (#3030).

Drives the real capture and read functions against the in-memory catalog and a
fake vendor. The Postgres-backed twin is ``test_option_lake_catalog.py``.

Hand-worked figures used below:

* A strike in dollars is stored in thousandths: 375 -> 375000, 452.5 -> 452500.
* The expiry is stored as ``YYYYMMDD``: 2025-06-13 -> ``20250613``.
* ``O:SPY250613C00375000`` is the SPY call struck at 375 expiring 2025-06-13.
* 2025-06-13 closes at 20:00 UTC = 1749844800000 ms; a listing is taken 30
  minutes after that, like a session's bars.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

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
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, point_lake_writer_at_tmp

AS_OF = date(2025, 6, 13)
SESSION_CLOSE_MS = 1749844800000
MINUTE_MS = 60_000
CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"
SECOND_PAGE = f"{CONTRACTS_URL}?cursor=page2"
RELATIVE_PATH = "option/usa/listing/spy/20250613_contracts.zip"
MEMBER = "20250613_spy_contracts.csv"


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
    from decimal import Decimal

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
        ([_row("O:SPY250613C00375000", 375.0005)], "thousandths"),  # finer than the symbol can write
        ([_row("O:SPY250613C00375000", 375, expiration_date="2025-13-45")], "expiration_date"),
        ([_row("O:SPY250613C00375000", 375, shares_per_contract=100.5)], "shares_per_contract"),
    ],
)
async def test_a_row_the_file_cannot_hold_fails_the_capture_naming_it(
    rows: list[dict[str, Any]], fragment: str, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    _mock_one_page(rows)

    result = await ensure_option_listing("SPY", AS_OF)

    assert result.record is None
    assert result.failure is not None and result.failure.reason == "validation_failed"
    assert fragment in result.failure.detail
    [row] = fake_catalog.rows.values()
    assert (row["status"], row["last_error"]) == ("failed", "validation_failed")
    assert _lake_files(tmp_lake) == []


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
async def test_reference_data_has_no_rolling_window_so_an_old_403_is_a_real_entitlement_problem(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """Listings reach back before the price floor (probed: as of 2024-06-14 answered)."""
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(403, text="not entitled"))

    result = await ensure_option_listing("SPY", date(2024, 6, 14), now_ms=1791828000000)

    assert result.failure is not None and result.failure.reason == "provider_entitlement_error"


# ── Refusals cost no claim ──────────────────────────────────────────────


@respx.mock
async def test_a_listing_for_a_session_that_closed_under_thirty_minutes_ago_is_refused(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    route = _mock_one_page(STANDARD_ROWS)

    refused = await ensure_option_listing("SPY", AS_OF, now_ms=SESSION_CLOSE_MS + 30 * MINUTE_MS - 1)
    allowed = await ensure_option_listing("SPY", AS_OF, now_ms=SESSION_CLOSE_MS + 30 * MINUTE_MS)

    assert refused.failure is not None and refused.failure.reason == "session_not_over"
    assert allowed.failure is None and allowed.record is not None
    assert route.call_count == 1
    assert len(fake_catalog.rows) == 1  # only the allowed ask made a row


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


# ── The file itself ─────────────────────────────────────────────────────


def test_building_the_file_refuses_a_duplicate_ticker_rather_than_dropping_one() -> None:
    contract = _vendor_contract("O:SPY250613C00375000", "375")

    with pytest.raises(OptionListingFileError, match="duplicate"):
        build_option_listing_zip_bytes("SPY", AS_OF, [contract, contract])


def test_a_strike_finer_than_a_thousandth_is_refused_not_rounded() -> None:
    with pytest.raises(OptionListingFileError, match="thousandths"):
        build_option_listing_zip_bytes("SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375.0005")])


def test_a_listing_file_that_is_not_the_one_written_is_refused_on_read() -> None:
    wrong_header = io.BytesIO()
    with zipfile.ZipFile(wrong_header, "w") as zf:
        zf.writestr(MEMBER, "ticker,strike\nO:SPY250613C00375000,375\n")
    wrong_member = build_option_listing_zip_bytes("SPY", AS_OF, [_vendor_contract("O:SPY250613C00375000", "375")])

    with pytest.raises(OptionListingFileError, match="header"):
        parse_option_listing_zip(wrong_header.getvalue(), "SPY", AS_OF)
    with pytest.raises(OptionListingFileError, match="expected one member"):
        parse_option_listing_zip(wrong_member, "QQQ", AS_OF)


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
