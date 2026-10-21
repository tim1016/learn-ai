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
