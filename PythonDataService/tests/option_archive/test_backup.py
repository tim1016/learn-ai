from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.data_lake.root_identity import init_empty_root
from app.option_archive.backup import (
    BackupRefusedError,
    CatalogSnapshot,
    RestoreReceipt,
    checked_restore_receipt,
    create_generation,
    manifest_digest,
    prune_generations,
    validate_destination,
    verify_generation,
)


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
    (destination / ".backup-destination.json").write_text('{"host_destination":"local-test"}')
    row = {"Id": 5, "DataRootId": str(root_id), "PriceAdjustmentMode": "raw", "FilePath": "equity/usa/spy.csv",
           "FileSha256": hashlib.sha256(path.read_bytes()).hexdigest(), "FileSizeBytes": len(path.read_bytes()),
           "ArtifactKind": "metadata", "Symbol": "SPY", "TradingDate": None, "Resolution": None, "DataType": None, "Market": "usa"}
    snapshot = CatalogSnapshot(schema_hash="schema", row_count=1, migration_ids=["v1"], files=[row])
    return destination, {str(root_id): root}, path, snapshot


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
    assert manifest.root_markers


@pytest.mark.parametrize("damage", ["dump", "file", "marker", "complete", "schema"])
async def test_generation_refuses_each_mismatched_half(source: tuple, damage: str) -> None:
    path = await generation(source)
    manifest = verify_generation(path)
    if damage == "dump":
        (path / "catalog.copy").write_bytes(b"wrong")
    elif damage == "complete":
        (path / "COMPLETE").unlink()
    elif damage == "marker":
        (source[0] / "objects" / next(iter(manifest.root_markers.values()))).write_text('{}')
    elif damage == "file":
        (source[0] / "objects" / manifest.files[0].sha256).write_bytes(b"wrong")
    with pytest.raises(BackupRefusedError):
        verify_generation(path, expected_schema_hash="different" if damage == "schema" else "schema")


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


async def test_retention_keeps_pinned_rollback_and_prunes_only_unreferenced_objects(source: tuple) -> None:
    first = await generation(source)
    second = await generation(source, completed_ms=100_000_000_000)
    pins = source[0] / "retention-pins"
    pins.mkdir()
    (pins / (first.name + ".json")).write_text(json.dumps({"manifest_sha256": manifest_digest(first), "retain_until_ms": 253402300799999}))
    prune_generations(source[0], daily_keep=1, weekly_keep=0)
    assert first.exists() and second.exists()
    (pins / (first.name + ".json")).unlink()
    prune_generations(source[0], daily_keep=1, weekly_keep=0)
    assert not first.exists() and verify_generation(second)


def test_destination_must_be_an_identified_external_mount(source: tuple, tmp_path: Path) -> None:
    destination, roots, _, _ = source
    validate_destination(destination, mount=destination, checkout=tmp_path / "checkout", roots=roots)
    with pytest.raises(BackupRefusedError, match="outside"):
        validate_destination(destination, mount=destination, checkout=tmp_path, roots=roots)
    (destination / ".backup-destination.json").unlink()
    with pytest.raises(BackupRefusedError, match="marker"):
        validate_destination(destination, mount=destination, checkout=tmp_path / "checkout", roots=roots)


def test_checked_restore_receipt_binds_generation_and_manifest() -> None:
    receipt = RestoreReceipt(generation_id="g", manifest_sha256="a" * 64, checked_at_ms=1000, passed=True, decoded_counts={"metadata": 1})
    assert checked_restore_receipt({"jobs": {"restore": {"receipt": receipt.model_dump()}}}) == receipt


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
    from app.option_archive.backup import BackupPausedError
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
        validate_destination(destination, mount=destination, checkout=tmp_path / "container-checkout", roots=roots,
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
        prune_generations(source[0], daily_keep=1, weekly_keep=0, should_pause=lambda: paused)
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
        source[0], daily_keep=1, weekly_keep=0, should_pause=paused), lambda: False))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.exists() and second.exists()
