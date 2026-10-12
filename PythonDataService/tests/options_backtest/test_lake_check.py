"""What the lake wrote down when it failed to fetch a thing (#3030): found through the real capture functions.

Each failure here is written by the lake's own capture (the vendor faked at the HTTP layer), and read back by
``LakeCheckData``, so the identities this file asks about are held to the ones the lake writes. A row's attempts are
the asks made of it: the first ask claims the row at attempt 1 and each ask after a failure spends one more. The lake
asks again until three are spent (``MAX_CLAIM_RETRIES``), except for an answer that the plan does not reach, and
whatever the count for a row recorded under another recipe. Whether the lake asks again is not restated by the check:
the matrix tests below put a failed row in every state (each reason, one to four attempts, the recipe the capture
runs under and another) and hold the check's answer to what the real capture then does with the vendor.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.option_bars import ensure_option_day_bars
from app.data_lake.option_listing import ensure_option_listing
from app.data_lake.run_materialization import LakeMaterializationError, materialize_engine_run
from app.options.contract import OptionContract
from app.options_backtest.data_check import (
    DataCheck,
    DataKind,
    DataThing,
    ProblemCode,
    RecordedFailure,
    SessionVerdict,
    check_data,
)
from app.options_backtest.fetching import DownloadFailed, ensure_run_data
from app.options_backtest.lake_check import LakeCheckData
from app.utils.background_loop import run_on_background_loop
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import NOW_2026_10_12_MS, raw_root, replace_the_stored_file
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import THU, WED, Day, flat, steady
from tests.options_backtest._run_lake import Vendor, ladder_day
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


# ── Whether the lake asks again is the lake's to say ─────────────────────
# A failed row is put in every state the lake can leave one in, and the check's answer is held to what the real
# capture then does: it asks the vendor again, or it answers without asking. The state is written straight into the
# fake catalog's row, and into the real table by SQL.

THE_REASONS = (
    "provider_auth_error",
    "provider_entitlement_error",
    "outside_data_plan",
    "provider_rate_limited",
    "unknown_symbol",
    "provider_api_error",
    "provider_no_data",
    "validation_failed",
)
ATTEMPTS_SPENT = (1, 2, 3, 4)
A_RECIPE_THE_LAKE_HAS_SINCE_REPLACED = "a-recipe-the-lake-has-since-replaced"
THE_KINDS = ("a contract's day", "a listing", "the underlying's day")
CALL_500 = OptionContract.of(root="SPY", expiry_date=WED, right="call", strike=500)


class _FakeRows:
    def __init__(self, fake_catalog: FakeCatalog) -> None:
        self._rows = fake_catalog.rows

    async def put(self, where: dict[str, Any], *, reason: str, attempts: int, other_recipe: bool) -> None:
        [row] = [row for row in self._rows.values() if all(row[key] == value for key, value in where.items())]
        row.update(status="failed", attempt_count=attempts, last_error=reason, lease_owner=None, lease_expires_at_ms=None)
        if other_recipe:
            row["data_contract_hash"] = A_RECIPE_THE_LAKE_HAS_SINCE_REPLACED


class _RealRows:
    _COLUMNS = {"artifact_kind": "ArtifactKind", "market": "Market", "symbol": "Symbol"}

    async def put(self, where: dict[str, Any], *, reason: str, attempts: int, other_recipe: bool) -> None:
        conditions = " AND ".join(f'"{self._COLUMNS[key]}" = ${n}' for n, key in enumerate(where, start=4))
        connection = await asyncpg.connect(settings.POSTGRES_URL)
        try:
            changed = await connection.execute(
                f"""
                UPDATE "DataLakeArtifacts"
                   SET "Status" = 'failed', "AttemptCount" = $1, "LastError" = $2,
                       "DataContractHash" = CASE WHEN $3 THEN '{A_RECIPE_THE_LAKE_HAS_SINCE_REPLACED}' ELSE "DataContractHash" END,
                       "LeaseOwner" = NULL, "LeaseExpiresAtMs" = NULL
                 WHERE {conditions}
                """,
                attempts,
                reason,
                other_recipe,
                *where.values(),
            )
        finally:
            await connection.close()
        assert changed == "UPDATE 1", changed


async def _the_check_and_the_lake_agree(
    kind: str, rows: _FakeRows | _RealRows, vendor: Vendor, *, reason: str, attempts: int, other_recipe: bool
) -> None:
    """Fail ``kind`` once for real, put its row in the state, and compare the check's answer with a real ask."""
    if kind == "a contract's day":
        assert (await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)).failure is None
        thing, where = DataThing(DataKind.CONTRACTS, CALL_500), {
            "artifact_kind": "time_series_bars",
            "market": "usa_options",
            "symbol": CALL_500.occ_symbol,
        }

        async def fail() -> Any:
            return await ensure_option_day_bars(CALL_500, WED, listed_under="SPY", now_ms=NOW_2026_10_12_MS)

        asked = lambda: len(vendor.option_requests)  # noqa: E731
    elif kind == "a listing":
        thing, where = LISTING, {"artifact_kind": "metadata", "market": "usa_options", "symbol": "SPY"}

        async def fail() -> Any:
            return await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)

        asked = lambda: len(vendor.listing_requests)  # noqa: E731
    else:
        thing, where = DataThing(DataKind.UNDERLYING_BARS), {
            "artifact_kind": "time_series_bars",
            "market": "usa",
            "symbol": "SPY",
        }

        async def fail() -> Any:
            try:
                await asyncio.to_thread(
                    materialize_engine_run,
                    symbol="SPY",
                    start=WED,
                    end=WED,
                    resolution="minute",
                    price_adjustment_mode="raw",
                )
            except LakeMaterializationError:
                return None

        asked = lambda: len(vendor.stock_requests)  # noqa: E731

    await fail()
    assert asked() == 1, "the first ask failed at the vendor"
    await rows.put(where, reason=reason, attempts=attempts, other_recipe=other_recipe)

    recorded = await _recorded(thing, WED)
    await fail()

    asked_again = asked() > 1
    assert recorded == RecordedFailure(reason=reason, will_be_asked_again=asked_again), (
        f"the check says {recorded}; the lake {'asked' if asked_again else 'did not ask'} the vendor again"
    )


def _vendor_that_fails_everything() -> Vendor:
    call = CALL_500.occ_symbol
    return Vendor(_scene(WED), option_errors={call: 500}, listing_errors={}, stock_errors={WED: 500})


@pytest.mark.parametrize("other_recipe", [False, True], ids=["same recipe", "another recipe"])
@pytest.mark.parametrize("attempts", ATTEMPTS_SPENT)
@pytest.mark.parametrize("reason", THE_REASONS)
@respx.mock
async def test_a_contract_day_is_called_a_try_again_exactly_when_the_real_capture_asks_the_vendor_again(
    reason: str, attempts: int, other_recipe: bool, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = _vendor_that_fails_everything()
    vendor.mount()

    await _the_check_and_the_lake_agree(
        "a contract's day", _FakeRows(fake_catalog), vendor, reason=reason, attempts=attempts, other_recipe=other_recipe
    )


@pytest.mark.parametrize("attempts", ATTEMPTS_SPENT)
@pytest.mark.parametrize("reason", THE_REASONS)
@respx.mock
async def test_a_listing_is_called_a_try_again_exactly_when_the_real_capture_asks_the_vendor_again(
    reason: str, attempts: int, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # A listing's row is found by its recipe, so there is no row of another recipe to be left behind.
    vendor = Vendor(_scene(WED), listing_errors={WED: 503})
    vendor.mount()

    await _the_check_and_the_lake_agree(
        "a listing", _FakeRows(fake_catalog), vendor, reason=reason, attempts=attempts, other_recipe=False
    )


@pytest.mark.parametrize("other_recipe", [False, True], ids=["same recipe", "another recipe"])
@pytest.mark.parametrize("attempts", (2, 3))
@pytest.mark.parametrize("reason", ("provider_api_error", "outside_data_plan"))
@respx.mock
async def test_the_underlyings_day_is_called_a_try_again_exactly_when_the_real_capture_asks_the_vendor_again(
    reason: str, attempts: int, other_recipe: bool, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The stock capture lifts the retry ceiling for no error, "outside the plan" included: three attempts is spent.
    vendor = Vendor(_scene(WED), stock_errors={WED: 500})
    vendor.mount()

    await _the_check_and_the_lake_agree(
        "the underlying's day",
        _FakeRows(fake_catalog),
        vendor,
        reason=reason,
        attempts=attempts,
        other_recipe=other_recipe,
    )


@pytest.mark.parametrize(
    ("kind", "other_recipe"),
    [(kind, other) for kind in THE_KINDS for other in (False, True) if not (kind == "a listing" and other)],
)
@pytest.mark.parametrize("attempts", (2, 3))
@pytest.mark.parametrize("reason", ("provider_api_error", "outside_data_plan"))
@respx.mock
async def test_on_the_real_table_each_kind_of_row_is_called_a_try_again_exactly_when_the_real_capture_asks_again(
    kind: str, reason: str, attempts: int, other_recipe: bool, clean_artifacts: None, pool: None, tmp_lake: Path
) -> None:
    vendor = Vendor(_scene(WED), option_errors={CALL_500.occ_symbol: 500}, listing_errors={}, stock_errors={WED: 500})
    if kind == "a listing":
        vendor.listing_errors = {WED: 503}
    vendor.mount()

    await _the_check_and_the_lake_agree(
        kind, _RealRows(), vendor, reason=reason, attempts=attempts, other_recipe=other_recipe
    )


def _sessions_check(day: date = WED) -> DataCheck:
    return check_data(
        setup(legs=[leg(expiry={"kind": "same_day"})]), [day], now_ms=NOW_2026_10_12_MS, data=LakeCheckData("SPY")
    )


@respx.mock
async def test_a_spent_row_cannot_be_tested_until_the_recipe_changes_and_then_it_is_a_download(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The review's scenario: a contract day fails three times and the check refuses it; a recipe bump leaves the row
    # behind, the lake would capture it, and the check must no longer say it will not try again.
    Vendor(_scene(WED), option_errors={CALL_500.occ_symbol: 500}).mount()
    await _store_the_underlying_and_the_listing()
    for _ in range(3):
        assert (await ensure_option_day_bars(CALL_500, WED, listed_under="SPY", now_ms=NOW_2026_10_12_MS)).failure

    spent = (await asyncio.to_thread(_sessions_check)).sessions[0]
    await _FakeRows(fake_catalog).put(
        {"artifact_kind": "time_series_bars", "market": "usa_options", "symbol": CALL_500.occ_symbol},
        reason="provider_api_error",
        attempts=3,
        other_recipe=True,
    )
    bumped = (await asyncio.to_thread(_sessions_check)).sessions[0]

    assert spent.verdict is SessionVerdict.CANNOT_BE_TESTED
    assert [problem.code for problem in spent.problems] == [ProblemCode.LAKE_HOLDS_A_FAILURE]
    assert bumped.verdict is SessionVerdict.NEEDS_DOWNLOAD and bumped.problems == ()
    assert any("asked for again" in note for note in bumped.notes)


async def _store_the_underlying_and_the_listing() -> None:
    await asyncio.to_thread(
        materialize_engine_run, symbol="SPY", start=WED, end=WED, resolution="minute", price_adjustment_mode="raw"
    )
    assert (await ensure_option_listing("SPY", WED, now_ms=NOW_2026_10_12_MS)).failure is None


# ── The first check after a start ────────────────────────────────────────


@respx.mock
async def test_the_first_check_after_a_start_with_nothing_stored_reaches_the_catalog(
    clean_artifacts: None, pool: None, tmp_lake: Path
) -> None:
    # The catalog's pool belongs to the loop that opens it, and a read for the check runs on the process's background
    # loop. A data plane that has not downloaded anything has not opened that loop's pool: the first check made after
    # a start must open it itself, and did not (the answer was "catalog unavailable", for ever).
    await asyncio.to_thread(lambda: run_on_background_loop(catalog_client.close_pool(), timeout=30))

    checked = await asyncio.to_thread(_sessions_check)

    [session] = checked.sessions
    assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD
    assert session.missing == (DataKind.UNDERLYING_BARS, DataKind.LISTING)


# ── A stored file the catalog does not admit ─────────────────────────────


@respx.mock
async def test_a_stored_file_the_catalog_does_not_admit_is_a_download_with_a_note_and_the_download_repairs_it(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    vendor = Vendor({WED: ladder_day()})
    vendor.mount()
    written = setup(legs=[leg(expiry={"kind": "same_day"})])
    await asyncio.to_thread(
        ensure_run_data, written, [WED], now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
    )
    assert (await asyncio.to_thread(_sessions_check)).sessions[0].verdict is SessionVerdict.READY
    [day_row] = [row for row in fake_catalog.rows.values() if row["symbol"] == CALL_500.occ_symbol]
    [listing_row] = [
        row for row in fake_catalog.rows.values() if row["artifact_kind"] == "metadata" and row["market"] == "usa_options"
    ]
    asked = vendor.vendor_requests

    for row in (day_row, listing_row):
        # Bytes the catalog holds no receipt for: the lake reads them as an error, not as a file that is not there.
        replace_the_stored_file(fake_catalog, raw_root(tmp_lake), row["file_path"], b"not what was stored", admit=False)
        [session] = (await asyncio.to_thread(_sessions_check)).sessions

        assert session.verdict is SessionVerdict.NEEDS_DOWNLOAD, row["file_path"]
        assert any("will be fetched again" in note for note in session.notes)

        await asyncio.to_thread(
            ensure_run_data, written, [WED], now_ms=NOW_2026_10_12_MS, should_stop=lambda: False
        )
        assert (await asyncio.to_thread(_sessions_check)).sessions[0].verdict is SessionVerdict.READY
    assert vendor.vendor_requests == asked + 2, "one request for the contract day, one for the listing"
