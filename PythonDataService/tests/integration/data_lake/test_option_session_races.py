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
