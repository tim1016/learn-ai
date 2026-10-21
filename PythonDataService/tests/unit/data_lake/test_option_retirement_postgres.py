"""Real catalog retirement/rollback, exclusively against an attested disposable DB."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.option_bars import read_option_day_bars
from app.data_lake.option_retirement import RetirementRefusedError, apply_retirement, legacy_sources, plan_retirement
from app.data_lake.option_session import read_option_session
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.root_identity import read_marker
from app.option_archive.backup import RestoreReceipt, create_generation, manifest_digest, restore_generation
from tests.options_backtest._lake import FRIDAY, raw_root, store_listing, store_option_day, vendor_listing_row
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest.test_option_retirement import CALL, UNKNOWN, approval
from tests.unit.data_lake.test_catalog_write_ops import clean_artifacts as clean_artifacts
from tests.unit.data_lake.test_catalog_write_ops import pool as pool

pytestmark = [pytest.mark.slow, pytest.mark.asyncio]


@pytest.mark.parametrize("fault", ["none", "refresh", "unlink_before_rollback"])
@respx.mock
async def test_approved_retirement_is_fenced_restartable_and_restorable(
    clean_artifacts, pool, tmp_lake: Path, tmp_path: Path, fault: str,
) -> None:
    await store_listing([vendor_listing_row(CALL), vendor_listing_row(UNKNOWN)])
    original = (await store_option_day(CALL, [])).record
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    root = raw_root(tmp_lake)
    root_id = read_marker(tmp_lake).data_root_id
    sources = await legacy_sources(root_id)
    destination = tmp_path / "backup"
    destination.mkdir()
    manifest = await create_generation(destination=destination, roots={str(root_id): tmp_lake},
                                       clock=lambda: 1000, should_pause=lambda: False)
    generation = destination / "generations" / manifest.generation_id
    # A real isolated test restore establishes the gate, before source deletion.
    async with catalog_client.connection() as connection:
        await connection.execute('CREATE SCHEMA retirement_restore')
        await connection.execute('CREATE TABLE retirement_restore."DataLakeArtifacts" (LIKE public."DataLakeArtifacts" INCLUDING ALL)')
        try:
            await restore_generation(generation=generation, roots={str(root_id): tmp_path / "restored"},
                                     connection=connection, schema="retirement_restore")
            assert await connection.fetchval('SELECT COUNT(*) FROM retirement_restore."DataLakeArtifacts"') == manifest.catalog_rows
        finally:
            await connection.execute('DROP SCHEMA retirement_restore CASCADE')
    receipt = tmp_path / "restore.json"
    receipt.write_text(RestoreReceipt(generation_id=manifest.generation_id, manifest_sha256=manifest_digest(generation),
        checked_at_ms=1100, passed=True, decoded_counts={"legacy": 1, "session": 1}).model_dump_json())
    plan = await asyncio.to_thread(plan_retirement, mode_root=root, sources=sources, generation=generation,
                                  restore_receipt=receipt, now_ms=1200)
    before = len(respx.calls)
    args = dict(plan=plan, approval=approval(plan.digest), mode_root=root, generation=generation,
                restore_receipt=receipt, journal=tmp_path / "journal.json", now_ms=1300)
    if fault == "refresh":
        async with catalog_client.connection() as connection:
            await connection.execute('UPDATE "DataLakeArtifacts" SET "Status"=\'fetching\' WHERE "Id"=$1', original.id)
        with pytest.raises(RetirementRefusedError, match="refreshed"):
            await apply_retirement(**args)
        assert (root / original.file_path).exists()
        return
    if fault == "unlink_before_rollback":
        async with catalog_client.connection() as connection:
            await connection.execute('''CREATE FUNCTION refuse_option_delete() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'simulated transaction failure after unlink'; END $$;
                CREATE TRIGGER refuse_option_delete BEFORE DELETE ON "DataLakeArtifacts"
                FOR EACH ROW EXECUTE FUNCTION refuse_option_delete();''')
        try:
            with pytest.raises(Exception, match="simulated transaction failure"):
                await apply_retirement(**args)
            assert not (root / original.file_path).exists()
            async with catalog_client.connection() as connection:
                assert await connection.fetchval('SELECT COUNT(*) FROM "DataLakeArtifacts" WHERE "Id"=$1', original.id) == 1
        finally:
            async with catalog_client.connection() as connection:
                await connection.execute('DROP TRIGGER refuse_option_delete ON "DataLakeArtifacts"; DROP FUNCTION refuse_option_delete()')
    assert await apply_retirement(**args) == 1
    assert await apply_retirement(**args) == 1
    assert await legacy_sources(root_id) == ()
    assert not (root / original.file_path).exists()
    kept = await asyncio.to_thread(read_option_day_bars, CALL, FRIDAY, lake_root=root)
    assert kept.sha256 == original.file_sha256 and kept.bars == ()
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY, lake_root=root)
    assert UNKNOWN.occ_symbol in session.unresolved
    assert len(respx.calls) == before
    # Rollback restores exact original bytes without provider access after retirement.
    async with catalog_client.connection() as connection:
        await connection.execute('CREATE SCHEMA retirement_rollback')
        await connection.execute('CREATE TABLE retirement_rollback."DataLakeArtifacts" (LIKE public."DataLakeArtifacts" INCLUDING ALL)')
        try:
            await restore_generation(generation=generation, roots={str(root_id): tmp_path / "rollback"},
                                     connection=connection, schema="retirement_rollback")
            restored = tmp_path / "rollback" / "lake" / "raw" / original.file_path
            import hashlib
            assert hashlib.sha256(restored.read_bytes()).hexdigest() == original.file_sha256
        finally:
            await connection.execute('DROP SCHEMA retirement_rollback CASCADE')
