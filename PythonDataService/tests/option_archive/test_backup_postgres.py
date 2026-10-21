"""Daily-only paired snapshot/restore tests in a disposable dedicated database."""
from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.root_identity import init_empty_root
from app.option_archive import service as service_module
from app.option_archive.backup import (
    BackupRefusedError,
    CatalogSnapshot,
    create_generation,
    dump_catalog,
    restore_generation,
    verify_generation,
)
from app.option_archive.restored_reads import decode_restored_lake, read_restored_catalog
from app.option_archive.service import OptionArchiveService
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveState, JobReceipt
from app.services.session_authority import et_minute_of_day_ms

pytestmark = pytest.mark.slow


@pytest.fixture
async def isolated_catalog(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[asyncpg.Connection]:
    url = settings.POSTGRES_URL or os.environ.get("POSTGRES_URL", "")
    if not url or os.environ.get("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("requires POSTGRES_URL attested disposable by POSTGRES_URL_IS_EPHEMERAL=1")
    database = "archive_preservation_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE DATABASE "{database}"')
    target_url = url.rsplit("/", 1)[0] + "/" + database
    connection = await asyncpg.connect(target_url)
    try:
        ddl = Path(__file__).resolve().parents[1] / "fixtures/option_archive/catalog.sql"
        await connection.execute(ddl.read_text())
        await connection.execute("INSERT INTO schema_migrations VALUES ('test-schema', 'test', 0, false)")
        monkeypatch.setattr(settings, "POSTGRES_URL", target_url)
        await catalog_client.close_pool()
        await catalog_client.init_pool()
        yield connection
    finally:
        await catalog_client.close_pool()
        await connection.close()
        await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        await admin.close()


async def insert(connection: asyncpg.Connection, root_id: str, path: str, raw: bytes, *, status: str = "complete", schema: str = "public", symbol: str | None = None, market: str | None = None) -> int:
    return await connection.fetchval(f'''INSERT INTO "{schema}"."DataLakeArtifacts"
        ("DataRootId","ArtifactKind","Provider","ProviderParams","PriceAdjustmentMode","DataContractHash",
         "FilePath","FileSha256","FileSizeBytes","Status","LeaseOwner","LeaseExpiresAtMs","LeaseGeneration",
         "AttemptCount","FetchedAtMs","Symbol","Market") VALUES ($1::uuid,'metadata','test','{{}}','raw',$2,$3,$4,$5,$6,'old-worker',9999999999999,7,1,1000,$7,$8)
        RETURNING "Id"''', root_id, hashlib.sha256(path.encode()).hexdigest(), path, hashlib.sha256(raw).hexdigest(), len(raw), status, symbol, market)


async def test_repeatable_snapshot_matches_restore_during_concurrent_writes_and_fences_leases(
    isolated_catalog: asyncpg.Connection, tmp_path: Path,
) -> None:
    root_id = str(uuid4())
    root = tmp_path / "source"
    init_empty_root(root, UUID(root_id))
    from app.option_archive.snapshot import ChainObservation, snapshot_path
    observation = ChainObservation(underlying="SPY", session_ms=1749821400000, observation_id="test-observation",
        started_ms=1749846600000, ended_ms=1749846600000, complete=True, reason=None,
        page_receipts=[], contracts=[], field_gaps={})
    relative = str(snapshot_path(observation))
    source = root / "lake" / "raw" / relative
    source.parent.mkdir(parents=True)
    source.write_text(observation.model_dump_json())
    await insert(isolated_catalog, root_id, relative, source.read_bytes(), symbol="SPY", market="usa_options")
    destination = tmp_path / "backup"
    destination.mkdir()

    async def write_while_backing_up() -> None:
        connection = await asyncpg.connect(settings.POSTGRES_URL)
        try:
            for index in range(25):
                await insert(connection, root_id, f"pending-{index}.json", b"pending", status="fetching")
                await asyncio.sleep(0)
        finally:
            await connection.close()
    writer = asyncio.create_task(write_while_backing_up())
    manifest = await create_generation(destination=destination, roots={root_id: root}, clock=lambda: 2000, should_pause=lambda: False)
    await writer
    generation = destination / "generations" / manifest.generation_id
    assert verify_generation(generation).catalog_rows >= 1
    await isolated_catalog.execute('CREATE SCHEMA restored')
    await isolated_catalog.execute('CREATE TABLE restored."DataLakeArtifacts" (LIKE public."DataLakeArtifacts" INCLUDING ALL)')
    target = tmp_path / "restored"
    await restore_generation(generation=generation, roots={root_id: target}, connection=isolated_catalog, schema="restored")
    assert await isolated_catalog.fetchval('SELECT COUNT(*) FROM restored."DataLakeArtifacts"') == manifest.catalog_rows
    assert await isolated_catalog.fetchval('SELECT COUNT(*) FROM restored."DataLakeArtifacts" WHERE "LeaseOwner" IS NOT NULL OR "LeaseExpiresAtMs" IS NOT NULL') == 0
    assert await isolated_catalog.fetchval('SELECT COUNT(*) FROM restored."DataLakeArtifacts" WHERE "Status"=\'fetching\'') == 0
    assert await isolated_catalog.fetchval('SELECT MIN("LeaseGeneration") FROM restored."DataLakeArtifacts"') == 8
    assert decode_restored_lake(await read_restored_catalog(isolated_catalog, "restored"), {root_id: target}) == {"metadata": 1}
    maximum = await isolated_catalog.fetchval('SELECT MAX("Id") FROM restored."DataLakeArtifacts"')
    next_id = await insert(isolated_catalog, root_id, "new.json", b"new", status="fetching", schema="restored")
    assert next_id > maximum


async def test_real_catalog_source_replacement_never_completes_wrong_generation(isolated_catalog: asyncpg.Connection, tmp_path: Path) -> None:
    root_id = str(uuid4())
    root = tmp_path / "source"
    init_empty_root(root, UUID(root_id))
    source = root / "lake" / "raw" / "test.json"
    source.parent.mkdir(parents=True)
    source.write_text('{}')
    await insert(isolated_catalog, root_id, "test.json", source.read_bytes())
    destination = tmp_path / "backup"
    destination.mkdir()
    original = await create_generation(destination=destination, roots={root_id: root}, clock=lambda: 1000, should_pause=lambda: False)

    async def snapshot_then_refresh(path: Path) -> CatalogSnapshot:
        result = await dump_catalog(path)
        source.write_text('{"changed":true}')
        return result
    with pytest.raises(BackupRefusedError, match="bounded attempts"):
        await create_generation(destination=destination, roots={root_id: root}, clock=lambda: 2000,
                                should_pause=lambda: False, snapshot=snapshot_then_refresh)
    assert verify_generation(destination / "generations" / original.generation_id).catalog_rows == 1


async def test_postgres_job_ownership_excludes_second_process_connection(isolated_catalog: asyncpg.Connection) -> None:
    from app.option_archive.scheduler import PostgresJobOwnership
    first, second = PostgresJobOwnership(), PostgresJobOwnership()
    async with first.hold("singleton-test") as owned:
        assert owned
        async with second.hold("singleton-test") as overlapping:
            assert not overlapping
    async with second.hold("singleton-test") as after_release:
        assert after_release


async def test_restore_pause_leaves_explicit_incomplete_target_and_no_catalog_import(isolated_catalog: asyncpg.Connection, tmp_path: Path) -> None:
    from app.option_archive.backup import BackupPausedError
    root_id = str(uuid4())
    root = tmp_path / "source"
    init_empty_root(root, UUID(root_id))
    source = root / "lake/raw/test.json"
    source.parent.mkdir(parents=True)
    source.write_text('{}')
    await insert(isolated_catalog, root_id, "test.json", source.read_bytes())
    destination = tmp_path / "backup"
    destination.mkdir()
    manifest = await create_generation(destination=destination, roots={root_id: root}, clock=lambda: 1000, should_pause=lambda: False)
    await isolated_catalog.execute('CREATE SCHEMA paused')
    await isolated_catalog.execute('CREATE TABLE paused."DataLakeArtifacts" (LIKE public."DataLakeArtifacts" INCLUDING ALL)')
    target = tmp_path / "target"
    with pytest.raises(BackupPausedError):
        await restore_generation(generation=destination / "generations" / manifest.generation_id,
            roots={root_id: target}, connection=isolated_catalog, schema="paused",
            should_pause=lambda: (target / "lake/.data-root.json").exists())
    assert (target / ".restore-incomplete.json").is_file()
    assert not (target / "lake/raw/test.json").exists()
    assert await isolated_catalog.fetchval('SELECT COUNT(*) FROM paused."DataLakeArtifacts"') == 0


OFF_HOURS_MS = et_minute_of_day_ms(date(2026, 10, 12), 120)


@pytest.mark.parametrize(("damage", "refusal"), [
    ("intact", None),
    ("unreceipted_at_source", "complete without a digest"),
    ("altered_by_the_restore", "differ from their committed receipt"),
])
async def test_weekly_restore_check_passes_only_what_the_restored_catalog_admits(
    isolated_catalog: asyncpg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, refusal: str | None,
) -> None:
    """A receipt that retirement trusts rests on the rows the restore produced, not on the manifest's copy of them."""
    from app.option_archive.snapshot import ChainObservation, snapshot_path
    root_id = str(uuid4())
    root = tmp_path / "source"
    init_empty_root(root, UUID(root_id))
    observation = ChainObservation(underlying="SPY", session_ms=1749821400000, observation_id="test-observation",
        started_ms=1749846600000, ended_ms=1749846600000, complete=True, reason=None,
        page_receipts=[], contracts=[], field_gaps={})
    relative = str(snapshot_path(observation))
    source = root / "lake" / "raw" / relative
    source.parent.mkdir(parents=True)
    source.write_text(observation.model_dump_json())
    artifact_id = await insert(isolated_catalog, root_id, relative, source.read_bytes(), symbol="SPY", market="usa_options")
    if damage == "unreceipted_at_source":
        await isolated_catalog.execute('UPDATE "DataLakeArtifacts" SET "FileSha256"=NULL WHERE "Id"=$1', artifact_id)
    destination = tmp_path / "backup"
    destination.mkdir()
    manifest = await create_generation(destination=destination, roots={root_id: root}, clock=lambda: 2000, should_pause=lambda: False)
    if damage == "altered_by_the_restore":
        restore = service_module.restore_generation

        async def restore_then_alter(*, connection: asyncpg.Connection, schema: str, **options: object):
            result = await restore(connection=connection, schema=schema, **options)
            await connection.execute(f'UPDATE "{schema}"."DataLakeArtifacts" SET "FileSha256"=repeat(\'0\', 64)')
            return result
        monkeypatch.setattr(service_module, "restore_generation", restore_then_alter)
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True, BACKUP_DESTINATION=destination),
                                   state=ArchiveState(tmp_path / "state.json"), clock=lambda: OFF_HOURS_MS)
    service.state.update(lambda doc: doc.jobs.update(backup=JobReceipt(generation_id=manifest.generation_id)))
    if refusal is None:
        await service.restore_pass()
        receipt = service.state.read().job("restore").receipt
        assert receipt is not None and receipt.passed and receipt.decoded_counts == {"metadata": 1}
        return
    with pytest.raises(BackupRefusedError, match=refusal):
        await service.restore_pass()
    restore_job = service.state.read().job("restore")
    assert restore_job.receipt is None and restore_job.last_success_ms is None
