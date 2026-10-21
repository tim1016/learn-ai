"""Daily real-catalog races; requires an explicitly disposable migrated database."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.option_bars import ensure_option_day_bars, read_option_day_bars
from app.data_lake.option_lake import LEASE_TTL_MS, WORKER_ID, HeldLease
from app.data_lake.option_listing import read_option_listing
from app.data_lake.option_session import (
    build_option_session_bytes,
    option_session_dch,
    option_session_publication_lock,
    read_option_session,
)
from app.data_lake.option_session_repack import _publish, admitted_legacy_members, repack_option_session
from app.data_lake.path_policy import OptionSessionPath
from app.data_lake.types import ArtifactIdentity
from app.options.contract import OptionContract
from app.utils.background_loop import background_loop
from tests._helpers.fake_lake_catalog import point_lake_writer_at_tmp
from tests.options_backtest._lake import FRIDAY, store_listing, store_option_day, vendor_listing_row
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed

pytestmark = pytest.mark.slow
CALL = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
PUT = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)


@pytest.fixture
async def storage_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    if not settings.POSTGRES_URL or os.environ.get("POSTGRES_URL_IS_EPHEMERAL") != "1":
        pytest.skip("requires an explicitly disposable migrated Postgres database")
    root_id = uuid4()
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(root_id))
    root = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    try:
        yield root
    finally:
        async with catalog_client.connection() as conn:
            await conn.execute('DELETE FROM "DataLakeArtifacts" WHERE "DataRootId" = $1', root_id)
        await catalog_client.close_pool()
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(catalog_client.close_pool(), background_loop()))


@respx.mock
async def test_concurrent_contract_publication_cannot_be_lost_by_partial_repack(storage_catalog: Path) -> None:
    await store_listing([vendor_listing_row(CALL), vendor_listing_row(PUT)])
    await store_option_day(CALL, [])
    # A repack snapshot owns the short publication lock. The source capture
    # cannot publish into its snapshot interval or silently disappear from it.
    with option_session_publication_lock("SPY", FRIDAY) as owns:
        assert owns
        respx.get(url__regex=f"https://api.polygon.io/v2/aggs/ticker/O:{PUT.occ_symbol}/.*").respond(
            200, json={"status": "OK", "ticker": f"O:{PUT.occ_symbol}", "resultsCount": 0})
        blocked = await ensure_option_day_bars(PUT, FRIDAY, listed_under="SPY")
        assert blocked.failure.reason == "lease_timeout"
    partial = await repack_option_session("SPY", FRIDAY)
    assert partial.failure is None
    await store_option_day(PUT, [])
    # During the migration interval partial containers resolve unknown tickers
    # through admitted legacy files, so the concurrent successful capture is usable.
    assert await asyncio.to_thread(read_option_day_bars, PUT, FRIDAY) is not None
    complete = await repack_option_session("SPY", FRIDAY)
    assert complete.failure is None
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.complete and session.confirmed_empty == {CALL.occ_symbol, PUT.occ_symbol}


@respx.mock
async def test_stale_container_publisher_cannot_replace_or_clean_winners_bytes(storage_catalog: Path) -> None:
    await store_listing([vendor_listing_row(CALL)])
    await store_option_day(CALL, [])
    listing = await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)
    members = await asyncio.to_thread(admitted_legacy_members, listing)
    payload = build_option_session_bytes(listing, members)
    identity = ArtifactIdentity(artifact_kind="time_series_bars", market="usa_options", symbol="SPY", trading_date=FRIDAY,
                                resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw")
    artifact_id = await catalog_client.claim_minute_bar(identity=identity, worker_id=WORKER_ID,
        lease_ttl_ms=LEASE_TTL_MS, data_contract_hash=option_session_dch(), file_path=str(OptionSessionPath("SPY", FRIDAY).relative_path()))
    async with catalog_client.connection() as conn:
        await conn.execute('UPDATE "DataLakeArtifacts" SET "LeaseExpiresAtMs" = 1 WHERE "Id" = $1', artifact_id)
    generation = await catalog_client.steal_or_retry_minute_bar(artifact_id=artifact_id, worker_id=WORKER_ID,
        lease_ttl_ms=LEASE_TTL_MS, max_retries=3)
    winner = await _publish(HeldLease(artifact_id, generation), identity, listing, payload)
    assert winner.failure is None
    stale = await _publish(HeldLease(artifact_id, 1), identity, listing, payload)
    assert stale.failure.reason == "lease_timeout"
    retained = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert retained.sha256 == winner.record.file_sha256 and retained.legacy_bytes(CALL.occ_symbol) == members[CALL.occ_symbol]


@respx.mock
async def test_repack_recovers_after_rename_without_catalog_commit(storage_catalog: Path) -> None:
    await store_listing([vendor_listing_row(CALL)])
    await store_option_day(CALL, [])
    first = await repack_option_session("SPY", FRIDAY)
    async with catalog_client.connection() as conn:
        # The state a crash between rename and receipt commit leaves: bytes
        # exist, but no committed complete receipt admits them.
        await conn.execute('UPDATE "DataLakeArtifacts" SET "Status" = \'fetching\', "LeaseOwner" = $2, '
                           '"LeaseExpiresAtMs" = 1 WHERE "Id" = $1', first.record.id, WORKER_ID)
    second = await repack_option_session("SPY", FRIDAY)
    assert second.failure is None and second.record.file_sha256 == first.record.file_sha256
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.complete


@respx.mock
async def test_listing_publish_cannot_cross_container_final_check_and_rebind_is_explicit(
    storage_catalog: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    import app.data_lake.option_listing as listing_module
    from app.data_lake.option_listing import ensure_option_listing
    from app.data_lake.option_session import OptionSessionFileError

    await store_listing([vendor_listing_row(CALL)])
    await store_option_day(CALL, [])
    listing = await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)
    members = await asyncio.to_thread(admitted_legacy_members, listing)
    payload = build_option_session_bytes(listing, members)
    identity = ArtifactIdentity(artifact_kind="time_series_bars", market="usa_options", symbol="SPY", trading_date=FRIDAY,
        resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw")
    artifact_id = await catalog_client.claim_minute_bar(identity=identity, worker_id=WORKER_ID,
        lease_ttl_ms=LEASE_TTL_MS, data_contract_hash=option_session_dch(),
        file_path=str(OptionSessionPath("SPY", FRIDAY).relative_path()))
    blocker = await asyncpg.connect(settings.POSTGRES_URL)
    transaction = blocker.transaction()
    await transaction.start()
    await blocker.fetchrow('SELECT "Id" FROM "DataLakeArtifacts" WHERE "Id"=$1 FOR UPDATE', artifact_id)
    async def finish_container():
        with option_session_publication_lock("SPY", FRIDAY) as owns:
            assert owns
            return await _publish(HeldLease(artifact_id, 1), identity, listing, payload)
    task = asyncio.create_task(finish_container())
    try:
        # Real staging files appear only after the final listing admission check.
        for _ in range(500):
            staged = list((storage_catalog / "staging").rglob("*.zip.tmp"))
            if staged:
                break
            await asyncio.sleep(0.002)
        assert staged and not task.done()
        monkeypatch.setattr(listing_module, "OPTION_LISTING_FILE_FORMAT", 3)
        respx.get("https://api.polygon.io/v3/reference/options/contracts").respond(200,
            json={"status": "OK", "results": [vendor_listing_row(CALL), vendor_listing_row(PUT)]})
        refused = await ensure_option_listing("SPY", FRIDAY)
        assert refused.failure.reason == "lease_timeout"
        assert (await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)).sha256 == listing.sha256
    finally:
        await transaction.commit()
        await blocker.close()
    assert (await task).failure is None
    refreshed = await ensure_option_listing("SPY", FRIDAY)
    assert refreshed.failure is None
    with pytest.raises(OptionSessionFileError):
        await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    with pytest.raises(OptionSessionFileError):
        await repack_option_session("SPY", FRIDAY)
    rebound = await repack_option_session("SPY", FRIDAY, rebind_listing=True)
    assert rebound.failure is None
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.confirmed_empty == {CALL.occ_symbol} and session.unresolved == {PUT.occ_symbol}


@respx.mock
async def test_killed_collector_releases_ownership_and_resumes_original_staged_outcomes(storage_catalog: Path) -> None:
    import sys

    from app.data_lake.option_lake import publish_option_artifact
    from app.data_lake.option_session_capture import capture_option_session
    from app.data_lake.path_policy import LeanMinuteBarPath
    from tests._helpers.option_vendor import no_trades
    from tests.options_backtest._lake import FRIDAY_OPEN_MS, NOW_2026_10_12_MS, stock_bar, stock_day_zip

    await store_listing([vendor_listing_row(CALL), vendor_listing_row(PUT)])
    stock = ArtifactIdentity(artifact_kind="time_series_bars", market="usa", symbol="SPY", trading_date=FRIDAY,
        resolution="minute", data_type="trade", provider="polygon", price_adjustment_mode="raw")
    relative = LeanMinuteBarPath("usa", "SPY", FRIDAY, "trade").relative_path()
    artifact_id = await catalog_client.claim_minute_bar(identity=stock, worker_id=WORKER_ID,
        lease_ttl_ms=LEASE_TTL_MS, data_contract_hash="a" * 64, file_path=str(relative))
    await publish_option_artifact(payload=stock_day_zip([stock_bar(FRIDAY_OPEN_MS,"500","500","500","500")]),
        rel_path=relative, lease=HeldLease(artifact_id,1), row_count=1, first_bar_start_ms=FRIDAY_OPEN_MS,
        last_bar_start_ms=FRIDAY_OPEN_MS, data_contract_hash="a" * 64, symbol="SPY", trading_date=FRIDAY)
    entered = storage_catalog.parent / "collector-entered"
    program = '''
import asyncio, os
from pathlib import Path
from datetime import date
import httpx, respx
from app.data_lake.option_session_capture import capture_option_session
async def main():
    async def pause(request):
        Path(os.environ['STORAGE_TEST_ENTERED']).write_bytes(b'ready')
        await asyncio.Event().wait()
    with respx.mock:
        respx.get('https://api.polygon.io/v2/aggs/ticker/O:SPY250613C00500000/range/1/minute/2025-06-13/2025-06-13').respond(200,json={'status':'OK','ticker':'O:SPY250613C00500000','resultsCount':0})
        respx.get('https://api.polygon.io/v2/aggs/ticker/O:SPY250613P00500000/range/1/minute/2025-06-13/2025-06-13').mock(side_effect=pause)
        await capture_option_session('SPY',date(2025,6,13),now_ms=1791828000000)
asyncio.run(main())
'''
    env = {**os.environ, "LEAN_DATA_WRITE_ROOT": str(storage_catalog), "DATA_LAKE_ROOT_ID": settings.DATA_LAKE_ROOT_ID,
           "POLYGON_API_KEY": "test-key", "STORAGE_TEST_ENTERED": str(entered)}
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", program, env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        for _ in range(500):
            if entered.is_file() or process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        if not entered.is_file():
            if process.returncode is None:
                process.kill()
            _, error = await process.communicate()
            pytest.fail(f"collector did not reach durable progress: {error.decode()}")
        process.kill()
        await process.wait()
        respx.get(f"https://api.polygon.io/v2/aggs/ticker/O:{PUT.occ_symbol}/range/1/minute/{FRIDAY}/{FRIDAY}").mock(
            return_value=no_trades(PUT.occ_symbol))
        outcome = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
        assert outcome.failure is None
        assert (await asyncio.to_thread(read_option_session, "SPY", FRIDAY)).complete
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
