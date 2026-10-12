"""Option bars and option listings against the real catalog table (#3030).

Live-Postgres tests, in the style of ``test_catalog_write_ops.py``: they skip
when ``POSTGRES_URL`` is unset, and the ``clean_artifacts`` fixture truncates
``DataLakeArtifacts`` (so ``POSTGRES_URL_IS_EPHEMERAL=1`` must attest the
database is disposable). The table is created by a .NET migration the machine
this was written on cannot run; it was loaded from a schema-only dump of the
dev table into a throwaway ``postgres:16``.

They run the real capture and read functions with the vendor faked by
``respx`` (with the answers probed on the wire, ``tests/_helpers/option_vendor.py``),
so a row is claimed, published under its lease and completed by the real SQL, and
every CHECK constraint on the table sees an option row and a listing row. A day's
bars are captured only for a contract the stored listing of its date holds, so each
capture here stores that listing first. The in-memory twins are
``test_option_bars.py`` and ``test_option_listing.py``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake import option_bars as option_bars_module
from app.data_lake import option_lake as option_lake_module
from app.data_lake import option_listing as option_listing_module
from app.data_lake.option_bars import ensure_option_day_bars, option_minute_trade_dch, read_option_day_bars
from app.data_lake.option_listing import (
    captured_listing_dates,
    ensure_option_listing,
    option_listing_dch,
    read_option_listing,
)
from app.data_lake.polygon_option_bars import PolygonOptionBar
from app.data_lake.types import ArtifactIdentity
from app.options.contract import OptionContract
from tests._helpers.fake_lake_catalog import point_lake_writer_at_tmp
from tests._helpers.option_vendor import bad_bars_answers, no_trades, outside_plan, traded

pytestmark = pytest.mark.asyncio

TRADING_DAY = date(2025, 6, 13)
SESSION_OPEN_MS = 1749821400000
MINUTE_MS = 60_000
NOW_2026_10_12_MS = 1791828000000
CONTRACT = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=600)
SYMBOL = "SPY250613C00600000"
CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture(autouse=True)
def _the_clock_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every capture without its own ``now_ms`` runs on 2026-10-12, the day the plan's floor was probed.

    The plan floor moves forward with the calendar, so a test that asked for 2025-06-13 against the
    real clock would be refused as outside the plan from 2027-06-12 on.
    """
    for module in (option_lake_module, option_bars_module, option_listing_module):
        monkeypatch.setattr(module, "current_ms", lambda: NOW_2026_10_12_MS)


@pytest.fixture
async def clean_artifacts():
    """Truncate DataLakeArtifacts before+after each test."""
    for after in (False, True):
        conn = await asyncpg.connect(_postgres_url())
        try:
            await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
        finally:
            await conn.close()
        if not after:
            yield


@pytest.fixture
async def pool():
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


async def _rows() -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_postgres_url())
    try:
        return await conn.fetch('SELECT * FROM "DataLakeArtifacts" ORDER BY "Id"')
    finally:
        await conn.close()


async def _sql(statement: str, *args: Any) -> None:
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute(statement, *args)
    finally:
        await conn.close()


def _bar(index: int = 0, **overrides: Any) -> dict[str, Any]:
    bar = {"v": 12, "vw": 1.27, "o": 1.27, "h": 1.31, "l": 1.25, "c": 1.27, "t": SESSION_OPEN_MS + index * MINUTE_MS, "n": 3}
    bar.update(overrides)
    return bar


def _bars_ok(results: list[dict[str, Any]], symbol: str = SYMBOL) -> httpx.Response:
    return traded(symbol, results)


def _mock_bars(response: httpx.Response, *, contract: str = SYMBOL) -> respx.Route:
    return respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/O:{contract}/range/1/minute/.*").mock(
        return_value=response
    )


def _listing_row(ticker: str, strike: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "cfi": "OCASPS", "contract_type": "call", "exercise_style": "american", "expiration_date": "2025-06-13",
        "primary_exchange": "BATO", "shares_per_contract": 100, "strike_price": strike, "ticker": ticker,
        "underlying_ticker": "SPY",
    }
    row.update(overrides)
    return row


def _mock_listing(rows: list[dict[str, Any]]) -> respx.Route:
    return respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(200, json={"status": "OK", "request_id": "r", "results": rows}))


async def _list(*contracts: OptionContract, day: date = TRADING_DAY, now_ms: int | None = None) -> None:
    """Store the SPY listing of ``day`` holding these contracts, through the real capture."""
    _mock_listing([
        _listing_row(f"O:{c.occ_symbol}", int(c.strike), contract_type=c.right, expiration_date=c.expiry_date.isoformat())
        for c in contracts
    ])
    assert (await ensure_option_listing("SPY", day, now_ms=now_ms)).failure is None


async def _ensure(contract: OptionContract = CONTRACT, day: date = TRADING_DAY, **kwargs: Any):
    await _list(contract, day=day, now_ms=kwargs.get("now_ms"))
    return await ensure_option_day_bars(contract, day, listed_under="SPY", **kwargs)


async def _bar_rows() -> list[asyncpg.Record]:
    return [row for row in await _rows() if row["ArtifactKind"] == "time_series_bars"]


# ── An option row on the real table ──────────────────────────────────────


@respx.mock
async def test_an_option_row_is_claimed_published_and_completed_by_the_real_sql(clean_artifacts, pool, tmp_lake) -> None:
    _mock_bars(_bars_ok([_bar(0), _bar(1, o=1.30, h=1.40, l=1.28, c=1.35, v=40)]))

    result = await _ensure()

    assert result.failure is None and result.record is not None
    [row] = await _bar_rows()
    assert (row["ArtifactKind"], row["Market"], row["Symbol"], row["TradingDate"]) == (
        "time_series_bars", "usa_options", SYMBOL, TRADING_DAY,
    )
    assert (row["Resolution"], row["DataType"], row["Provider"], row["PriceAdjustmentMode"]) == (
        "minute", "trade", "polygon", "raw",
    )
    assert row["Status"] == "complete"
    assert (row["RowCount"], row["FirstBarStartMs"], row["LastBarStartMs"]) == (2, SESSION_OPEN_MS, SESSION_OPEN_MS + MINUTE_MS)
    assert row["FileSha256"] == result.record.file_sha256
    assert row["DataContractHash"].strip() == option_minute_trade_dch()
    assert row["LeaseOwner"] is None and row["LeaseGeneration"] == 1
    assert row["FilePath"] == "option/usa/minute/spy/spy250613c00600000/20250613_trade.zip"


@respx.mock
async def test_a_completed_day_reads_back_through_the_real_admission(clean_artifacts, pool, tmp_lake) -> None:
    _mock_bars(_bars_ok([_bar(0), _bar(1, o=0.29, h=0.29, l=0.29, c=0.29, v=1)]))
    result = await _ensure()

    day = await asyncio.to_thread(read_option_day_bars, CONTRACT, TRADING_DAY)

    assert day is not None and result.record is not None
    assert day.sha256 == result.record.file_sha256
    assert [bar.close for bar in day.bars] == [Decimal("1.27"), Decimal("0.29")]
    assert [bar.start_ms for bar in day.bars] == [SESSION_OPEN_MS, SESSION_OPEN_MS + MINUTE_MS]


@respx.mock
async def test_a_second_claim_for_the_same_contract_and_day_is_refused_but_other_keys_are_not(
    clean_artifacts, pool, tmp_lake
) -> None:
    def identity(symbol: str = SYMBOL, day: date = TRADING_DAY, market: str = "usa_options") -> ArtifactIdentity:
        return ArtifactIdentity(
            artifact_kind="time_series_bars", market=market, symbol=symbol, trading_date=day, resolution="minute",
            data_type="trade", provider="polygon", price_adjustment_mode="raw",
        )

    async def claim(who: str, ident: ArtifactIdentity) -> int | None:
        return await catalog_client.claim_minute_bar(
            identity=ident, worker_id=who, lease_ttl_ms=300_000, data_contract_hash=option_minute_trade_dch(),
            file_path="x.zip",
        )

    first = await claim("w-1", identity())
    second = await claim("w-2", identity())
    another_day = await claim("w-1", identity(day=date(2025, 6, 12)))
    another_contract = await claim("w-1", identity(symbol="SPY250613C00601000"))
    same_symbol_in_the_stock_market = await claim("w-1", identity(market="usa"))

    assert first is not None and second is None
    assert another_day is not None and another_contract is not None
    # The market is part of the key: the stock tree and the option tree never collide.
    assert same_symbol_in_the_stock_market is not None


@respx.mock
async def test_a_contract_that_never_traded_completes_with_zero_rows_and_reads_back_as_captured(
    clean_artifacts, pool, tmp_lake
) -> None:
    route = _mock_bars(no_trades(SYMBOL))

    result = await _ensure()
    again = await _ensure()

    assert result.failure is None and result.record is not None
    [row] = await _bar_rows()
    assert row["Status"] == "complete"
    assert (row["RowCount"], row["FirstBarStartMs"], row["LastBarStartMs"]) == (0, 0, 0)
    assert row["FileSha256"] == result.record.file_sha256 and row["FileSizeBytes"] == result.record.file_size_bytes
    assert again.reused is True and route.call_count == 1
    captured = await asyncio.to_thread(read_option_day_bars, CONTRACT, TRADING_DAY)
    never = await asyncio.to_thread(read_option_day_bars, CONTRACT, date(2025, 6, 12))
    assert captured is not None and captured.bars == ()
    assert never is None


@respx.mock
async def test_a_contract_symbol_of_21_characters_is_refused_before_the_table_could_object(
    clean_artifacts, pool, tmp_lake
) -> None:
    long_contract = OptionContract.of(root="ABCDEF", expiry_date=date(2025, 6, 13), right="call", strike=600)
    ident = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa_options", symbol=long_contract.occ_symbol, trading_date=TRADING_DAY,
        resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw",
    )

    result = await ensure_option_day_bars(long_contract, TRADING_DAY, listed_under="SPY")

    assert result.failure is not None and result.failure.reason == "symbol_too_long"
    assert await _rows() == []
    # The refusal is not caution for its own sake: the column really is varchar(20).
    with pytest.raises(asyncpg.StringDataRightTruncationError):
        await catalog_client.claim_minute_bar(
            identity=ident, worker_id="w", lease_ttl_ms=1000, data_contract_hash=option_minute_trade_dch(), file_path="x.zip"
        )


@respx.mock
async def test_a_contract_the_stored_listing_does_not_hold_makes_no_row_and_no_vendor_call(clean_artifacts, pool, tmp_lake) -> None:
    route = _mock_bars(no_trades(SYMBOL))

    without_a_listing = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")
    await _list(OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=601))
    not_in_it = await ensure_option_day_bars(CONTRACT, TRADING_DAY, listed_under="SPY")

    assert without_a_listing.failure is not None and without_a_listing.failure.reason == "listing_not_captured"
    assert not_in_it.failure is not None and not_in_it.failure.reason == "not_listed"
    assert route.call_count == 0
    assert await _bar_rows() == []


@respx.mock
async def test_a_session_older_than_the_plan_floor_makes_no_row_and_no_vendor_call(clean_artifacts, pool, tmp_lake) -> None:
    old_contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    route = _mock_bars(outside_plan(), contract="SPY241018C00580000")

    results = [
        await ensure_option_day_bars(old_contract, date(2024, 10, 11), listed_under="SPY", now_ms=NOW_2026_10_12_MS)
        for _ in range(5)
    ]

    assert [r.failure.reason for r in results if r.failure is not None] == ["outside_data_plan"] * 5
    assert route.call_count == 0
    assert await _rows() == []


@respx.mock
async def test_a_day_at_the_edge_the_vendor_refuses_is_asked_again_each_time_on_the_real_retry_sql(
    clean_artifacts, pool, tmp_lake
) -> None:
    edge_contract = OptionContract.of(root="SPY", expiry_date=date(2024, 10, 18), right="call", strike=580)
    route = _mock_bars(outside_plan(), contract="SPY241018C00580000")

    reasons = [(await _ensure(edge_contract, date(2024, 10, 14), now_ms=NOW_2026_10_12_MS)).failure for _ in range(5)]

    assert [f.reason for f in reasons if f is not None] == ["outside_data_plan"] * 5
    [row] = await _bar_rows()
    assert (row["Status"], row["LastError"], row["AttemptCount"]) == ("failed", "outside_data_plan", 5)
    assert route.call_count == 5


@respx.mock
async def test_the_vendors_failure_text_is_stored_without_the_key(clean_artifacts, pool, tmp_lake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "sentinel-key-4f9a1c")
    _mock_bars(bad_bars_answers(SYMBOL)["count 5 and results empty"]())

    result = await _ensure()

    assert result.failure is not None and result.failure.reason == "provider_api_error"
    [row] = await _bar_rows()
    assert (row["Status"], row["LastError"], row["LeaseOwner"]) == ("failed", "provider_api_error", None)
    assert "disagrees" in row["ErrorMessage"]
    assert "sentinel-key-4f9a1c" not in repr(dict(row))
    assert all("sentinel-key-4f9a1c" not in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_a_complete_row_whose_file_has_gone_is_refreshed_under_a_new_generation(clean_artifacts, pool, tmp_lake) -> None:
    route = _mock_bars(_bars_ok([_bar(0)]))
    first = await _ensure()
    assert first.record is not None
    (tmp_lake / "lake" / "raw" / first.record.file_path).unlink()

    again = await _ensure()

    assert again.failure is None and again.reused is False and route.call_count == 2
    [row] = await _bar_rows()
    assert (row["Status"], row["LeaseGeneration"], row["AttemptCount"]) == ("complete", 2, 2)


@respx.mock
async def test_a_writer_whose_lease_was_taken_while_it_fetched_publishes_nothing_under_the_real_row_lock(
    clean_artifacts, pool, tmp_lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch_then_lose_the_lease(*args: Any, **kwargs: Any) -> list[PolygonOptionBar]:
        await _sql(
            'UPDATE "DataLakeArtifacts" SET "LeaseGeneration" = "LeaseGeneration" + 1, "LeaseOwner" = \'the-winner\''
            ' WHERE "ArtifactKind" = \'time_series_bars\''
        )
        return [
            PolygonOptionBar(
                t_ms=SESSION_OPEN_MS, open=Decimal("1.27"), high=Decimal("1.31"), low=Decimal("1.25"), close=Decimal("1.27"),
                volume=Decimal(12),
            )
        ]

    monkeypatch.setattr(option_bars_module, "fetch_option_minute_bars", fetch_then_lose_the_lease)

    result = await _ensure()

    assert result.record is None and result.failure is not None and result.failure.reason == "lease_timeout"
    [row] = await _bar_rows()
    assert (row["Status"], row["LeaseOwner"], row["LeaseGeneration"]) == ("fetching", "the-winner", 2)
    assert row["FileSha256"] is None
    assert [p for p in (tmp_lake / "lake" / "raw").rglob("*.zip") if "minute" in p.parts] == []  # no bars file; the listing's is there


# ── A listing row on the real table ──────────────────────────────────────


@respx.mock
async def test_a_listing_row_is_claimed_published_and_completed_by_the_real_sql(clean_artifacts, pool, tmp_lake) -> None:
    _mock_listing([_listing_row("O:SPY250613C00375000", 375), _listing_row("O:SPY250613P00400000", 400, contract_type="put")])

    result = await ensure_option_listing("SPY", TRADING_DAY)

    assert result.failure is None and result.record is not None
    [row] = await _rows()
    assert (row["ArtifactKind"], row["Market"], row["Symbol"], row["Provider"], row["PriceAdjustmentMode"]) == (
        "metadata", "usa_options", "SPY", "polygon", "raw",
    )
    assert (row["TradingDate"], row["Resolution"], row["DataType"]) == (None, None, None)
    assert (row["Status"], row["RowCount"]) == ("complete", 2)
    assert row["DataContractHash"].strip() == option_listing_dch("SPY", TRADING_DAY)
    assert row["FileSha256"] == result.record.file_sha256
    listing = await asyncio.to_thread(read_option_listing, "SPY", TRADING_DAY)
    assert listing is not None and listing.sha256 == result.record.file_sha256
    assert [r.standard for r in listing.rows] == [True, True]


@respx.mock
async def test_a_second_claim_for_the_same_listing_is_refused(clean_artifacts, pool, tmp_lake) -> None:
    _mock_listing([_listing_row("O:SPY250613C00375000", 375)])
    captured = await ensure_option_listing("SPY", TRADING_DAY)
    identity = ArtifactIdentity(
        artifact_kind="metadata", market="usa_options", symbol="SPY", provider="polygon", price_adjustment_mode="raw"
    )

    second = await catalog_client.claim_metadata_artifact(
        identity=identity, worker_id="w-2", lease_ttl_ms=300_000,
        data_contract_hash=option_listing_dch("SPY", TRADING_DAY), file_path="x.zip",
    )
    other_day = await catalog_client.claim_metadata_artifact(
        identity=identity, worker_id="w-2", lease_ttl_ms=300_000,
        data_contract_hash=option_listing_dch("SPY", date(2025, 6, 12)), file_path="y.zip",
    )

    assert captured.record is not None
    assert second is None
    assert other_day is not None


@respx.mock
async def test_the_captured_listing_dates_come_from_the_real_table_in_one_select(clean_artifacts, pool, tmp_lake) -> None:
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-12"}).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": [_listing_row("O:SPY250613C00375000", 375)]})
    )
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-13"}).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": [_listing_row("O:SPY250613C00375000", 375)]})
    )
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-16"}).mock(return_value=httpx.Response(500, text="boom"))
    for day in (date(2025, 6, 12), date(2025, 6, 13), date(2025, 6, 16)):
        await ensure_option_listing("SPY", day)

    captured = await captured_listing_dates("SPY", date(2025, 6, 11), date(2025, 6, 20))
    nothing_for_another_underlying = await captured_listing_dates("QQQ", date(2025, 6, 11), date(2025, 6, 20))

    assert captured == (date(2025, 6, 12), date(2025, 6, 13))
    assert nothing_for_another_underlying == ()


@respx.mock
async def test_the_captured_dates_include_both_ends_of_the_range_and_only_complete_listings_on_the_real_table(
    clean_artifacts, pool, tmp_lake
) -> None:
    """06-10 and 06-18 are captured and sit one day outside the range; 06-12 failed and 06-16 is still being fetched."""
    one_listing = [_listing_row("O:SPY250613C00375000", 375)]
    for day in (date(2025, 6, 10), date(2025, 6, 11), date(2025, 6, 13), date(2025, 6, 17), date(2025, 6, 18)):
        respx.get(CONTRACTS_URL, params={"as_of": day.isoformat()}).mock(
            return_value=httpx.Response(200, json={"status": "OK", "results": one_listing})
        )
        assert (await ensure_option_listing("SPY", day)).failure is None
    respx.get(CONTRACTS_URL, params={"as_of": "2025-06-12"}).mock(return_value=httpx.Response(500, text="boom"))
    assert (await ensure_option_listing("SPY", date(2025, 6, 12))).failure is not None
    leaving_fetching = await catalog_client.claim_metadata_artifact(
        identity=ArtifactIdentity(
            artifact_kind="metadata", market="usa_options", symbol="SPY", provider="polygon", price_adjustment_mode="raw"
        ),
        worker_id="w", lease_ttl_ms=300_000, data_contract_hash=option_listing_dch("SPY", date(2025, 6, 16)), file_path="x.zip",
    )
    assert leaving_fetching is not None

    captured = await captured_listing_dates("SPY", date(2025, 6, 11), date(2025, 6, 17))
    one_day = await captured_listing_dates("SPY", date(2025, 6, 18), date(2025, 6, 18))

    assert captured == (date(2025, 6, 11), date(2025, 6, 13), date(2025, 6, 17))
    assert one_day == (date(2025, 6, 18),)


@respx.mock
async def test_a_row_with_nothing_but_a_ticker_is_stored_on_the_real_table_with_empty_cells(clean_artifacts, pool, tmp_lake) -> None:
    _mock_listing([{"ticker": "O:SPY250613C00375000"}, _listing_row("O:SPY250613C00376000", 376)])

    result = await ensure_option_listing("SPY", TRADING_DAY)

    assert result.failure is None and result.record is not None
    [row] = await _rows()
    assert (row["Status"], row["RowCount"]) == ("complete", 2)
    listing = await asyncio.to_thread(read_option_listing, "SPY", TRADING_DAY)
    assert listing is not None
    assert [(r.ticker, r.standard) for r in listing.rows] == [("O:SPY250613C00375000", False), ("O:SPY250613C00376000", True)]


@respx.mock
async def test_a_new_file_format_stales_the_old_formats_row_on_the_real_table(
    clean_artifacts, pool, tmp_lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_listing([_listing_row("O:SPY250613C00375000", 375)])
    old = await ensure_option_listing("SPY", TRADING_DAY)
    monkeypatch.setattr(option_listing_module, "OPTION_LISTING_FILE_FORMAT", option_listing_module.OPTION_LISTING_FILE_FORMAT + 1)

    new = await ensure_option_listing("SPY", TRADING_DAY)

    assert old.record is not None and new.record is not None and old.record.id != new.record.id
    rows = {row["Id"]: row for row in await _rows()}
    assert (rows[old.record.id]["Status"], rows[new.record.id]["Status"]) == ("stale", "complete")
    assert rows[old.record.id]["FilePath"] == rows[new.record.id]["FilePath"]
    assert await captured_listing_dates("SPY", TRADING_DAY, TRADING_DAY) == (TRADING_DAY,)


# ── Neither kind of row shows up in a stock answer ───────────────────────


@respx.mock
async def test_option_and_listing_rows_stay_out_of_every_stock_answer_the_catalog_gives(clean_artifacts, pool, tmp_lake) -> None:
    # One real stock row, so the stock answers are not empty for the wrong reason.
    stock = ArtifactIdentity(
        artifact_kind="time_series_bars", market="usa", symbol="SPY", trading_date=TRADING_DAY, resolution="minute",
        data_type="trade", provider="polygon", price_adjustment_mode="raw",
    )
    stock_id = await catalog_client.claim_minute_bar(
        identity=stock, worker_id="w", lease_ttl_ms=300_000, data_contract_hash="a" * 64, file_path="equity/x.zip"
    )
    assert stock_id is not None
    assert await catalog_client.complete_artifact(
        stock_id, 390, SESSION_OPEN_MS, SESSION_OPEN_MS + 389 * MINUTE_MS, 1000, "b" * 64,
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )
    _mock_bars(_bars_ok([_bar(0)]))
    _mock_listing([_listing_row("O:SPY250613C00375000", 375)])
    assert (await _ensure()).record is not None
    assert (await ensure_option_listing("SPY", TRADING_DAY)).record is not None

    stock_totals = await catalog_client.select_storage_totals_by_kind("usa")
    stock_spans = await catalog_client.select_symbol_coverage_spans("usa")
    stock_revisions = await catalog_client.select_symbol_revision_counts("usa", "raw", "trade", date(2020, 1, 1))
    stock_coverage_of_the_contract = await catalog_client.select_artifact_coverage(
        "usa", SYMBOL, "trade", "polygon", "raw", TRADING_DAY, TRADING_DAY
    )
    stock_minute_bars = await catalog_client.select_coverage_minute_bars(
        "usa", SYMBOL, "trade", TRADING_DAY, TRADING_DAY, price_adjustment_mode="raw"
    )

    assert [(t.artifact_kind, t.resolution, t.artifact_count) for t in stock_totals] == [("time_series_bars", "minute", 1)]
    assert [(s.symbol, s.artifact_count) for s in stock_spans] == [("SPY", 1)]
    assert [(r.symbol, r.session_count) for r in stock_revisions] == [("SPY", 1)]
    assert stock_coverage_of_the_contract == [] and stock_minute_bars == []
    # The same questions about the option market see the option rows, and only those.
    option_totals = await catalog_client.select_storage_totals_by_kind("usa_options")
    option_spans = await catalog_client.select_symbol_coverage_spans("usa_options")
    assert sorted((t.artifact_kind, t.resolution, t.artifact_count) for t in option_totals) == [
        ("metadata", None, 1), ("time_series_bars", "minute", 1),
    ]
    assert [(s.symbol, s.artifact_count) for s in option_spans] == [(SYMBOL, 1)]
