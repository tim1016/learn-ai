"""Real catalog retirement/rollback, exclusively against an attested disposable DB."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.option_bars import read_option_day_bars
from app.data_lake.option_retirement import RetirementRefusedError, apply_retirement, legacy_sources, plan_retirement
from app.data_lake.option_session import read_option_session
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.root_identity import read_marker
from app.option_archive.backup import (
    BackupRefusedError,
    RestoreReceipt,
    create_generation,
    manifest_digest,
    prune_generations,
    restore_generation,
    verify_generation,
)
from app.utils.timestamps import now_ms_utc
from tests.options_backtest._lake import FRIDAY, raw_root, store_listing, store_option_day, vendor_listing_row
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest.test_option_retirement import CALL, UNKNOWN, approval
from tests.unit.data_lake.test_catalog_write_ops import clean_artifacts as clean_artifacts
from tests.unit.data_lake.test_catalog_write_ops import pool as pool

pytestmark = [pytest.mark.slow, pytest.mark.asyncio]


@pytest.mark.parametrize("fault", ["none", "refresh", "unlink_before_rollback", "longer_pin"])
@respx.mock
async def test_approved_retirement_is_fenced_restartable_and_restorable(
    clean_artifacts, pool, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
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
    if fault == "longer_pin":
        await create_generation(destination=destination, roots={str(root_id): tmp_lake},
                                clock=lambda: 2000, should_pause=lambda: False)
        pin = destination / "retention-pins" / f"{manifest.generation_id}.json"
        pin.parent.mkdir()
        pin.write_text(json.dumps({"manifest_sha256": manifest_digest(generation),
                                   "retain_until_ms": plan.rollback_until_ms + 10_000}))
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
    if fault == "longer_pin":
        assert json.loads(pin.read_bytes())["retain_until_ms"] == plan.rollback_until_ms + 10_000
        import app.option_archive.backup as backup

        monkeypatch.setattr(backup.time, "time_ns", lambda: (plan.rollback_until_ms + 1) * 1_000_000)
        await asyncio.to_thread(prune_generations, destination, daily_keep=1, weekly_keep=0)
        assert verify_generation(generation).generation_id == manifest.generation_id
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


@pytest.mark.parametrize("first", ["prune", "retirement"])
@respx.mock
async def test_retirement_and_pruning_order_the_exact_rollback_generation(
    clean_artifacts, pool, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str,
) -> None:
    import app.option_archive.backup as backup

    await store_listing([vendor_listing_row(CALL), vendor_listing_row(UNKNOWN)])
    source = (await store_option_day(CALL, [])).record
    assert (await repack_option_session("SPY", FRIDAY)).failure is None
    root = raw_root(tmp_lake)
    root_id = read_marker(tmp_lake).data_root_id
    destination = tmp_path / "backup"
    destination.mkdir()
    now = now_ms_utc()
    older = await create_generation(destination=destination, roots={str(root_id): tmp_lake},
        clock=lambda: now - 2000, should_pause=lambda: False)
    await create_generation(destination=destination, roots={str(root_id): tmp_lake},
        clock=lambda: now - 1000, should_pause=lambda: False)
    generation = destination / "generations" / older.generation_id
    receipt = tmp_path / "restore.json"
    receipt.write_text(RestoreReceipt(generation_id=older.generation_id, manifest_sha256=manifest_digest(generation),
        checked_at_ms=now, passed=True, decoded_counts={"legacy": 1, "session": 1}).model_dump_json())
    plan = await asyncio.to_thread(plan_retirement, mode_root=root, sources=await legacy_sources(root_id),
        generation=generation, restore_receipt=receipt, now_ms=now)
    accepted = approval(plan.digest).model_copy(update={"approved_at_ms": now})
    args = dict(plan=plan, approval=accepted, mode_root=root, generation=generation,
                restore_receipt=receipt, journal=tmp_path / "journal.json", now_ms=now)
    original = root / source.file_path
    reached, release = threading.Event(), threading.Event()
    if first == "prune":
        remove = backup.shutil.rmtree

        def held_remove(path, *positional, **keywords):
            if Path(path) == generation:
                reached.set()
                assert release.wait(5)
            return remove(path, *positional, **keywords)

        monkeypatch.setattr(backup.shutil, "rmtree", held_remove)
        pruning = asyncio.create_task(asyncio.to_thread(prune_generations, destination, daily_keep=1, weekly_keep=0))
        try:
            assert await asyncio.to_thread(reached.wait, 5)
            with pytest.raises(RetirementRefusedError, match="retention"):
                await apply_retirement(**args)
            assert original.exists()
        finally:
            release.set()
            await pruning
        assert not generation.exists()
        with pytest.raises(RetirementRefusedError, match="verification"):
            await apply_retirement(**args)
        assert original.exists() and not args["journal"].exists()
    else:
        unlink = Path.unlink

        def held_unlink(path, *positional, **keywords):
            if path == original:
                reached.set()
                assert release.wait(5)
            return unlink(path, *positional, **keywords)

        monkeypatch.setattr(Path, "unlink", held_unlink)
        retiring = asyncio.create_task(apply_retirement(**args))
        try:
            assert await asyncio.to_thread(reached.wait, 5)
            with pytest.raises(BackupRefusedError, match="retention"):
                await asyncio.to_thread(prune_generations, destination, daily_keep=1, weekly_keep=0)
            assert generation.exists()
        finally:
            release.set()
            assert await retiring == 1
        await asyncio.to_thread(prune_generations, destination, daily_keep=1, weekly_keep=0)
        assert not original.exists()
        assert verify_generation(generation).generation_id == older.generation_id
        assert (destination / "objects" / source.file_sha256).exists()
