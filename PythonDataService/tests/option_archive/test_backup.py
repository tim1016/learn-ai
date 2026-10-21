from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.data_lake.catalog_schema import DATA_LAKE_ARTIFACTS, ColumnExpectation
from app.data_lake.root_identity import MARKER_SCHEMA_VERSION, init_empty_root, marker_path
from app.option_archive import lake_restore
from app.option_archive.backup import (
    BackupPausedError,
    BackupRefusedError,
    CatalogSnapshot,
    backup_destination_activity,
    create_generation,
    extend_pin,
    manifest_digest,
    prune_generations,
    read_pin,
    restore_generation,
    validate_destination,
    verify_generation,
)

COLUMNS = [{"column_name": "Id", "data_type": "bigint", "udt_name": "int8", "is_nullable": "NO", "ordinal_position": 1}]
SCHEMA_HASH = lake_restore.digest(lake_restore.canonical(COLUMNS))
NOW = 2_000


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, dict[str, Path], Path, CatalogSnapshot]:
    root_id = uuid4()
    root = tmp_path / "source"
    init_empty_root(root, root_id)
    path = root / "lake" / "raw" / "equity" / "usa" / "spy.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"canonical bytes")
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / ".backup-destination.json").write_text(json.dumps({"host_destination": str(tmp_path / "host-backup")}))
    row = {"Id": 5, "DataRootId": str(root_id), "PriceAdjustmentMode": "raw", "FilePath": "equity/usa/spy.csv",
           "FileSha256": hashlib.sha256(path.read_bytes()).hexdigest(), "FileSizeBytes": len(path.read_bytes())}
    snapshot = CatalogSnapshot(schema_hash=SCHEMA_HASH, row_count=1, migration_ids=["v1"], files=[row])
    return destination, {str(root_id): root}, path, snapshot


def host_paths(tmp_path: Path) -> dict[str, object]:
    return {"expected_host_destination": str(tmp_path / "host-backup"), "host_checkout": tmp_path / "host-checkout",
            "host_lake_root": tmp_path / "host-lake"}


async def generation(source: tuple[Path, dict[str, Path], Path, CatalogSnapshot], *, completed_ms: int = 1000) -> Path:
    destination, roots, _path, inventory = source
    async def snapshot(path: Path) -> CatalogSnapshot:
        path.write_bytes(b"binary-copy-test-receipt")
        return inventory
    manifest = await create_generation(destination=destination, roots=roots, clock=lambda: completed_ms,
                                       should_pause=lambda: False, snapshot=snapshot)
    return destination / "generations" / manifest.generation_id


async def test_generation_binds_exact_original_receipts_and_shares_unchanged_objects(source: tuple) -> None:
    first = await generation(source)
    before = set((source[0] / "objects").iterdir())
    second = await generation(source, completed_ms=2000)
    assert before == set((source[0] / "objects").iterdir())
    manifest = verify_generation(second)
    original = next(item for item in manifest.files if item.artifact_ids)
    assert (original.artifact_ids, original.sha256, original.size_bytes) == ([5], source[3].files[0]["FileSha256"], 15)
    assert verify_generation(first).catalog_rows == 1
    assert any(item.relative_path == lake_restore.MARKER_PATH for item in manifest.files)


@pytest.mark.parametrize("damage", ["dump", "file", "marker", "complete", "schema"])
async def test_generation_refuses_each_mismatched_half(source: tuple, damage: str) -> None:
    path = await generation(source)
    manifest = verify_generation(path)
    if damage == "dump":
        (path / "catalog.copy").write_bytes(b"wrong")
    elif damage == "complete":
        (path / "COMPLETE").unlink()
    elif damage == "marker":
        marker = next(item for item in manifest.files if item.relative_path == lake_restore.MARKER_PATH)
        (source[0] / "objects" / marker.sha256).write_text('{}')
    elif damage == "file":
        (source[0] / "objects" / manifest.files[0].sha256).write_bytes(b"wrong")
    with pytest.raises(BackupRefusedError):
        verify_generation(path, expected_schema_hash="different" if damage == "schema" else SCHEMA_HASH)


def test_restore_tool_names_the_lake_root_marker(tmp_path: Path) -> None:
    assert tmp_path / lake_restore.MARKER_PATH == marker_path(tmp_path)
    assert lake_restore.MARKER_SCHEMA_VERSION == MARKER_SCHEMA_VERSION


class _EmptyTargetCatalog:
    """An empty, migrated target catalog; the real import runs daily in test_backup_postgres."""

    def __init__(self) -> None:
        self.imported: bytes | None = None

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        return COLUMNS if "information_schema" in query else [{"version": "v1"}]

    async def fetchval(self, query: str, *args: object) -> object:
        return 0 if "COUNT" in query else None

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield

    async def copy_to_table(self, table: str, **options: object) -> None:
        self.imported = options["source"].read()

    async def execute(self, query: str) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_legacy_manifest_still_verifies_and_restores_without_catalog_mirrors(
    source: tuple, tmp_path: Path,
) -> None:
    path = await generation(source)
    legacy_fields = dict(artifact_kind="metadata", symbol="SPY", session_ms=1749821400000,
                         resolution=None, data_type=None, market="usa_options", row_count=None)
    manifest = json.loads((path / "manifest.json").read_bytes())
    for item in manifest["files"]:
        item.update(legacy_fields)
    payload = lake_restore.canonical(manifest)
    (path / "manifest.json").write_bytes(payload)
    (path / "COMPLETE").write_text(lake_restore.digest(payload))

    checked = verify_generation(path)
    assert all(not (legacy_fields.keys() & item.model_dump().keys()) for item in checked.files)
    root_id, = source[1]
    target = tmp_path / "legacy-restored"
    catalog = _EmptyTargetCatalog()
    await restore_generation(generation=path, roots={root_id: target}, connection=catalog)
    assert (target / "lake/raw/equity/usa/spy.csv").read_bytes() == source[2].read_bytes()
    assert catalog.imported == (path / "catalog.copy").read_bytes()


@pytest.mark.parametrize("damaged", [False, True])
async def test_operator_tool_restores_exact_bytes_only_from_a_verified_generation(
    source: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damaged: bool,
) -> None:
    path = await generation(source)
    if damaged:
        (source[0] / "objects" / source[3].files[0]["FileSha256"]).write_bytes(b"canonical bytez")
    catalog = _EmptyTargetCatalog()
    async def connect(dsn: str, **_: object) -> _EmptyTargetCatalog:
        return catalog
    monkeypatch.setattr(lake_restore.asyncpg, "connect", connect)
    monkeypatch.setenv("RESTORE_TEST_DSN", "postgres://target")
    (root_id, root), = source[1].items()
    target = tmp_path / "target"
    argv = [str(path), "--root", f"{root_id}={target}", "--dsn-env", "RESTORE_TEST_DSN"]
    if damaged:
        with pytest.raises(lake_restore.RestoreRefusedError):
            await lake_restore.main(argv)
        assert not target.exists() and catalog.imported is None
        return
    await lake_restore.main(argv)
    assert (target / "lake/raw/equity/usa/spy.csv").read_bytes() == b"canonical bytes"
    assert marker_path(target).read_bytes() == marker_path(root).read_bytes()
    assert catalog.imported == (path / "catalog.copy").read_bytes()
    assert not (target / lake_restore.INCOMPLETE_MARKER).exists()


def test_generation_copy_of_the_restore_tool_runs_without_the_app(tmp_path: Path) -> None:
    tool = tmp_path / "restore_lake_generation.py"
    tool.write_bytes(Path(lake_restore.__file__).read_bytes())
    result = subprocess.run([sys.executable, "-I", str(tool), "--help"], cwd=tmp_path, capture_output=True,
                            text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr


def test_catalog_fixture_ddl_matches_the_drift_authority() -> None:
    ddl = (Path(__file__).resolve().parents[1] / "fixtures/option_archive/catalog.sql").read_text()
    body = ddl.split('CREATE TABLE "DataLakeArtifacts" (', 1)[1].split(");", 1)[0]
    columns = []
    for line in body.splitlines():
        name, definition = re.fullmatch(r'"(\w+)" (.+?),?', line.strip()).groups()
        pg_type = re.sub(r"\(\d+\)", "", re.split(r" (?:GENERATED|PRIMARY|NOT NULL)", definition)[0])
        columns.append(ColumnExpectation(name, pg_type, nullable="NOT NULL" not in definition))
    assert tuple(columns) == DATA_LAKE_ARTIFACTS.columns
    assert set(re.findall(r"ADD CONSTRAINT (\w+)", ddl)) == set(DATA_LAKE_ARTIFACTS.check_constraints)
    assert set(re.findall(r"CREATE UNIQUE INDEX (\w+)", ddl)) == set(DATA_LAKE_ARTIFACTS.partial_unique_indexes)


async def test_replacement_during_backup_preserves_previous_generation(source: tuple) -> None:
    first = await generation(source)
    source[2].write_bytes(b"replacement bytes")
    with pytest.raises(BackupRefusedError, match="bounded attempts"):
        await generation(source, completed_ms=2000)
    assert verify_generation(first).catalog_rows == 1
    assert [path for path in (source[0] / "generations").iterdir() if not path.name.startswith('.')] == [first]


async def test_interruption_before_open_never_publishes_complete(source: tuple) -> None:
    async def snapshot(path: Path) -> CatalogSnapshot:
        path.write_bytes(b"dump")
        return source[3]
    calls = 0
    def paused() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1
    with pytest.raises(BackupRefusedError):
        await create_generation(destination=source[0], roots=source[1], clock=lambda: 1000,
                                should_pause=paused, snapshot=snapshot)
    assert not list((source[0] / "generations").glob("*/COMPLETE"))


async def test_pause_after_the_copy_is_reported_as_a_pause(source: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.option_archive.backup as backup
    paused = False
    copy = backup._copy_snapshot_files
    def copied(*args, **kwargs):
        nonlocal paused
        result = copy(*args, **kwargs)
        paused = True
        return result
    monkeypatch.setattr(backup, "_copy_snapshot_files", copied)
    async def snapshot(path: Path) -> CatalogSnapshot:
        path.write_bytes(b"dump")
        return source[3]
    with pytest.raises(BackupPausedError):
        await create_generation(destination=source[0], roots=source[1], clock=lambda: 1000,
                                should_pause=lambda: paused, snapshot=snapshot)
    assert not list((source[0] / "generations").iterdir())


async def test_retention_keeps_pinned_rollback_and_prunes_only_unreferenced_objects(source: tuple) -> None:
    first = await generation(source)
    second = await generation(source, completed_ms=100_000_000_000)
    extend_pin(source[0], first.name, 253402300799999, manifest_sha256=manifest_digest(first))
    prune_generations(source[0], clock=lambda: NOW, daily_keep=1, weekly_keep=0)
    assert first.exists() and second.exists()
    (source[0] / "retention-pins" / (first.name + ".json")).unlink()
    prune_generations(source[0], clock=lambda: NOW, daily_keep=1, weekly_keep=0)
    assert not first.exists() and verify_generation(second)


@pytest.mark.parametrize("now", [4_999, 5_000])
async def test_pin_holds_its_generation_until_the_injected_clock_reaches_its_deadline(source: tuple, now: int) -> None:
    first = await generation(source)
    await generation(source, completed_ms=100_000_000_000)
    extend_pin(source[0], first.name, 5_000, manifest_sha256=manifest_digest(first))
    prune_generations(source[0], clock=lambda: now, daily_keep=1, weekly_keep=0)
    assert first.exists() == (now < 5_000)


async def test_extending_a_pin_keeps_the_later_deadline_and_its_restore_point(source: tuple) -> None:
    first = await generation(source)
    digest = manifest_digest(first)
    extend_pin(source[0], first.name, 9_000, manifest_sha256=digest)
    assert extend_pin(source[0], first.name, 7_000, manifest_sha256=digest).retain_until_ms == 9_000
    assert read_pin(source[0], first.name).retain_until_ms == 9_000
    with pytest.raises(BackupRefusedError, match="rollback pin"):
        extend_pin(source[0], first.name, 10_000, manifest_sha256="b" * 64)


@pytest.mark.parametrize("pin", ["[]", "not json", '{"manifest_sha256": null, "retain_until_ms": true}'])
async def test_unreadable_pin_is_refused_by_reader_and_pruning(source: tuple, pin: str) -> None:
    first = await generation(source)
    await generation(source, completed_ms=100_000_000_000)
    pins = source[0] / "retention-pins"
    pins.mkdir()
    (pins / (first.name + ".json")).write_text(pin.replace("null", json.dumps(manifest_digest(first))))
    with pytest.raises(BackupRefusedError, match="rollback pin"):
        read_pin(source[0], first.name)
    with pytest.raises(BackupRefusedError, match="rollback pin"):
        prune_generations(source[0], clock=lambda: NOW, daily_keep=1, weekly_keep=0)
    assert first.exists()


async def test_unverifiable_generation_is_kept_and_reported_while_retention_continues(source: tuple) -> None:
    damaged = await generation(source)
    middle = await generation(source, completed_ms=1_000_000_000)
    newest = await generation(source, completed_ms=100_000_000_000)
    (damaged / "catalog.copy").write_bytes(b"bit rot")
    assert prune_generations(source[0], clock=lambda: NOW, daily_keep=1, weekly_keep=0) == [damaged.name]
    assert damaged.exists() and not middle.exists() and verify_generation(newest)
    assert all((source[0] / "objects" / item.sha256).exists() for item in verify_generation(newest).files)


async def test_pruning_removes_a_dead_staging_folder_but_never_runs_beside_a_live_backup(source: tuple) -> None:
    await generation(source)
    dead = source[0] / "generations" / ".deadbeef"
    dead.mkdir()
    (dead / "catalog.copy").write_bytes(b"left by a killed backup")
    leftover = source[0] / "objects" / ".partial-object"
    leftover.write_bytes(b"left by a killed backup")
    (source[0] / "generations" / ".DS_Store").write_bytes(b"host file-browser metadata")
    with backup_destination_activity(source[0], shared=True), pytest.raises(BackupRefusedError, match="retry later"):
        prune_generations(source[0], clock=lambda: NOW)
    assert dead.exists()
    prune_generations(source[0], clock=lambda: NOW)
    assert not dead.exists() and not leftover.exists()


def test_destination_must_be_an_identified_external_mount(source: tuple, tmp_path: Path) -> None:
    destination, roots, _, _ = source
    host = host_paths(tmp_path)
    validate_destination(destination, checkout=tmp_path / "checkout", roots=roots, **host)
    with pytest.raises(BackupRefusedError, match="outside"):
        validate_destination(destination, checkout=tmp_path, roots=roots, **host)
    (destination / ".backup-destination.json").unlink()
    with pytest.raises(BackupRefusedError, match="marker"):
        validate_destination(destination, checkout=tmp_path / "checkout", roots=roots, **host)


@pytest.mark.parametrize("host_destination", ["~/learn-ai-lake-backup", "learn-ai-lake-backup"])
def test_host_destination_is_never_expanded_inside_the_container(source: tuple, tmp_path: Path, host_destination: str) -> None:
    destination, roots, _, _ = source
    (destination / ".backup-destination.json").write_text(json.dumps({"host_destination": host_destination}))
    with pytest.raises(BackupRefusedError, match="absolute"):
        validate_destination(destination, checkout=tmp_path / "checkout", roots=roots,
                             **{**host_paths(tmp_path), "expected_host_destination": host_destination})


async def test_verification_runs_off_the_option_loop_and_rechecks_pause_before_publish(source: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    import app.option_archive.backup as backup
    entered, release = threading.Event(), threading.Event()
    original = backup.verify_generation
    def held_verification(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(backup, "verify_generation", held_verification)
    task = asyncio.create_task(generation(source))
    assert await asyncio.to_thread(entered.wait, 2)
    assert not task.done(), "the option loop must remain responsive while generation hashes are checked"
    release.set()
    await task


async def test_copy_pauses_between_files_without_a_complete_generation(source: tuple) -> None:
    async def snapshot(path: Path) -> CatalogSnapshot:
        path.write_bytes(b"dump")
        return source[3]
    calls = 0
    def pause() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3
    with pytest.raises(BackupPausedError):
        await create_generation(destination=source[0], roots=source[1], clock=lambda: 1000,
                                should_pause=pause, snapshot=snapshot)
    assert not list((source[0] / "generations").glob("*/COMPLETE"))


def test_host_mount_alias_cannot_hide_a_backup_inside_the_checkout(source: tuple, tmp_path: Path) -> None:
    destination, roots, _, _ = source
    host_checkout = tmp_path / "host-checkout"
    aliased_inside = host_checkout / "backup"
    (destination / ".backup-destination.json").write_text(json.dumps({"host_destination": str(aliased_inside)}))
    with pytest.raises(BackupRefusedError, match="host destination"):
        validate_destination(destination, checkout=tmp_path / "container-checkout", roots=roots,
            expected_host_destination=str(aliased_inside), host_checkout=host_checkout, host_lake_root=host_checkout / "lake")


@pytest.mark.parametrize("boundary", ["verification", "deletion"])
async def test_pruning_pauses_between_hashes_and_deletions(source: tuple, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    import app.option_archive.backup as backup
    first = await generation(source)
    second = await generation(source, completed_ms=2000)
    orphan = source[0] / "objects" / ("f" * 64)
    orphan.write_bytes(b"unreferenced")
    paused = False
    verify, remove = backup.verify_generation, backup.shutil.rmtree
    def checked(*args, **kwargs):
        nonlocal paused
        result = verify(*args, **kwargs)
        if boundary == "verification":
            paused = True
        return result
    def deleted(*args, **kwargs):
        nonlocal paused
        remove(*args, **kwargs)
        paused = True
    monkeypatch.setattr(backup, "verify_generation", checked)
    monkeypatch.setattr(backup.shutil, "rmtree", deleted)
    with pytest.raises(backup.BackupPausedError):
        prune_generations(source[0], clock=lambda: NOW, daily_keep=1, weekly_keep=0, should_pause=lambda: paused)
    assert orphan.exists() and second.exists()
    assert first.exists() == (boundary == "verification")
    assert verify(second)


async def test_cancelled_pruning_drains_current_hash_and_stops_before_deleting(source: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    import app.option_archive.backup as backup
    first = await generation(source)
    second = await generation(source, completed_ms=2000)
    entered, release = threading.Event(), threading.Event()
    verify = backup.verify_generation
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return verify(*args, **kwargs)
    monkeypatch.setattr(backup, "verify_generation", held)
    task = asyncio.create_task(backup.bounded_file_work(lambda paused: prune_generations(
        source[0], clock=lambda: NOW, daily_keep=1, weekly_keep=0, should_pause=paused), lambda: False))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.exists() and second.exists()
