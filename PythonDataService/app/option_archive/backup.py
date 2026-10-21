"""Checked lake generations paired with a repeatable-read binary catalog COPY.

Content-addressed objects are immutable. COMPLETE is written last and binds
all metadata, so a failed generation cannot advance the recovery point.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.data_lake import catalog_client
from app.data_lake.atomic import _fsync_path
from app.data_lake.maintenance import lake_activity
from app.data_lake.root_identity import read_marker
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class BackupRefusedError(RuntimeError):
    """The chosen generation cannot establish a recoverable lake/catalog pair."""


class BackupPausedError(BackupRefusedError):
    """A calendar pause left no completed backup or restore."""


class BackupFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    artifact_kind: str | None = None
    artifact_ids: list[int] = []
    symbol: str | None = None
    session_ms: int | None = None
    resolution: str | None = None
    data_type: str | None = None
    market: str | None = None
    row_count: int | None = None


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    generation_id: str
    started_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    completed_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    schema_hash: str
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_rows: int
    root_ids: list[str]
    files: list[BackupFile]
    root_markers: dict[str, str]
    migration_ids: list[str]
    restore_files: dict[str, str]


class CatalogSnapshot(BaseModel):
    schema_hash: str
    row_count: int
    migration_ids: list[str]
    files: list[dict[str, Any]]


def _require_work_window(should_pause: Callable[[], bool]) -> None:
    if should_pause():
        raise BackupPausedError("Lake maintenance paused before the canonical regular open.")


async def bounded_file_work[T](work: Callable[[Callable[[], bool]], T], should_pause: Callable[[], bool]) -> T:
    """Drain a bounded file worker before releasing ownership or temporary roots."""
    stopped = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(work, lambda: stopped.is_set() or should_pause()))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stopped.set()
        await asyncio.gather(task, return_exceptions=True)
        raise


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _write_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_path(path.parent)


def _safe_relative(text: str) -> Path:
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in text.split("/")):
        raise BackupRefusedError("backup contains an unsafe relative file path")
    return Path(*path.parts)


def validate_destination(destination: Path, *, mount: Path, checkout: Path, roots: Mapping[str, Path],
                         expected_host_destination: str | None = None, host_checkout: Path | None = None,
                         host_lake_root: Path | None = None) -> None:
    if expected_host_destination is not None:
        host_destination = Path(expected_host_destination).expanduser()
        if not host_destination.is_absolute() or host_checkout is None or host_lake_root is None or not host_checkout.is_absolute():
            raise BackupRefusedError("absolute host checkout, lake and backup paths are required to verify mount aliases")
        host_lake = host_lake_root if host_lake_root.is_absolute() else host_checkout / host_lake_root
        for forbidden in (host_checkout, host_lake):
            if host_destination.resolve().is_relative_to(forbidden.resolve()):
                raise BackupRefusedError("backup host destination must be outside the checkout and the lake, including mount aliases")
    resolved = destination.resolve(strict=True)
    expected = mount.resolve(strict=True)
    if resolved != expected or destination.is_symlink() or not destination.is_dir():
        raise BackupRefusedError("backup destination is not the configured mounted directory")
    for forbidden in (checkout, *roots.values()):
        if resolved.is_relative_to(forbidden.resolve()):
            raise BackupRefusedError("backup destination must be outside the checkout and every lake root")
    marker = destination / ".backup-destination.json"
    if not marker.is_file():
        raise BackupRefusedError("backup destination identity marker is missing; verify the host mount")
    named_host = json.loads(marker.read_bytes()).get("host_destination")
    if not named_host or (expected_host_destination is not None and named_host != expected_host_destination):
        raise BackupRefusedError("backup destination marker does not name the configured host mount")


async def catalog_schema_hash(connection: Any, *, schema: str = "public") -> str:
    columns = await connection.fetch('''SELECT column_name, data_type, udt_name, is_nullable, ordinal_position
        FROM information_schema.columns WHERE table_schema=$1 AND table_name='DataLakeArtifacts'
        ORDER BY ordinal_position''', schema)
    if not columns:
        raise BackupRefusedError("catalog table is missing")
    return _digest(_json([dict(row) for row in columns]))


async def dump_catalog(path: Path) -> CatalogSnapshot:
    async with catalog_client.connection() as connection, connection.transaction(isolation="repeatable_read", readonly=True):
        schema = await catalog_schema_hash(connection)
        migrations = await connection.fetch('SELECT "MigrationId" FROM "__EFMigrationsHistory" ORDER BY "MigrationId"')
        rows = await connection.fetch('''SELECT "Id", "DataRootId", "PriceAdjustmentMode", "FilePath",
            "FileSha256", "FileSizeBytes", "ArtifactKind", "Symbol", "TradingDate", "Resolution", "DataType", "Market", "RowCount" FROM "DataLakeArtifacts"
            WHERE "Status"='complete' ORDER BY "Id"''')
        count = await connection.fetchval('SELECT COUNT(*) FROM "DataLakeArtifacts"')
        with path.open("xb") as output:
            await connection.copy_from_query('SELECT * FROM "DataLakeArtifacts" ORDER BY "Id"', output=output, format="binary")
            output.flush()
            os.fsync(output.fileno())
    return CatalogSnapshot(schema_hash=schema, row_count=count, migration_ids=[row["MigrationId"] for row in migrations],
                           files=[{key: str(value) if isinstance(value, UUID) else session_open_ms_utc(value) if isinstance(value, date) else value for key, value in dict(row).items()} for row in rows])


def _put_object(destination: Path, payload: bytes, expected: str | None = None) -> str:
    digest = _digest(payload)
    if expected is not None and digest != expected:
        raise BackupRefusedError("a source file changed after the consistent catalog snapshot")
    target = destination / "objects" / digest
    if target.exists():
        if _digest(target.read_bytes()) != digest:
            raise BackupRefusedError("a previously retained backup object is corrupt")
        return digest
    temp = target.with_name("." + digest + "." + uuid4().hex)
    _write_durable(temp, payload)
    os.replace(temp, target)
    _fsync_path(target.parent)
    return digest


def _copy_snapshot_files(destination: Path, roots: Mapping[str, Path], snapshot: CatalogSnapshot,
                         should_pause: Callable[[], bool] = lambda: False) -> tuple[list[BackupFile], dict[str, str]]:
    indexed: dict[tuple[str, str], BackupFile] = {}
    for row in snapshot.files:
        _require_work_window(should_pause)
        root_id = row["DataRootId"]
        if root_id not in roots:
            raise BackupRefusedError("catalog snapshot references an unconfigured physical root")
        mode = row["PriceAdjustmentMode"] or "raw"
        relative = Path("lake") / mode / _safe_relative(row["FilePath"])
        path = roots[root_id] / relative
        if path.is_symlink() or not path.resolve().is_relative_to(roots[root_id].resolve()):
            raise BackupRefusedError("catalog file escaped its physical root")
        payload = path.read_bytes()
        if len(payload) != row["FileSizeBytes"]:
            raise BackupRefusedError("a source file size changed after the catalog snapshot")
        digest = _put_object(destination, payload, row["FileSha256"])
        key = (root_id, relative.as_posix())
        if key in indexed:
            previous = indexed[key]
            if previous.sha256 != digest:
                raise BackupRefusedError("catalog snapshot has conflicting receipts for a file")
            previous.artifact_ids.append(row["Id"])
        else:
            indexed[key] = BackupFile(root_id=root_id, relative_path=relative.as_posix(), sha256=digest,
                size_bytes=len(payload), artifact_kind=row["ArtifactKind"], artifact_ids=[row["Id"]],
                symbol=row["Symbol"], session_ms=row["TradingDate"], resolution=row["Resolution"],
                data_type=row["DataType"], market=row["Market"], row_count=row.get("RowCount"))
    markers: dict[str, str] = {}
    for root_id, root in roots.items():
        marker = read_marker(root)
        if marker is None or str(marker.data_root_id) != root_id:
            raise BackupRefusedError("source lake root marker does not match the catalog")
        state_path = root / "option-archive-state.json"
        if state_path.exists():
            state_bytes = state_path.read_bytes()
            json.loads(state_bytes)
            state_digest = _put_object(destination, state_bytes)
            indexed[(root_id, state_path.name)] = BackupFile(root_id=root_id, relative_path=state_path.name,
                sha256=state_digest, size_bytes=len(state_bytes))
        markers[root_id] = _put_object(destination, (root / "lake" / ".data-root.json").read_bytes())
        # Coverage receipts beside bars/factors are part of the lake too.
        # Never copy scratch/staging or a symlink to data outside this root.
        for path in sorted((root / "lake").rglob("*")):
            _require_work_window(should_pause)
            if not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise BackupRefusedError("lake contains a file outside its root")
            relative = path.relative_to(root).as_posix()
            if (root_id, relative) not in indexed:
                payload = path.read_bytes()
                digest = _put_object(destination, payload)
                indexed[(root_id, relative)] = BackupFile(root_id=root_id, relative_path=relative, sha256=digest, size_bytes=len(payload))
    return sorted(indexed.values(), key=lambda item: (item.root_id, item.relative_path)), markers


async def create_generation(*, destination: Path, roots: Mapping[str, Path], clock: Callable[[], int],
                            should_pause: Callable[[], bool], snapshot: Callable[[Path], Any] = dump_catalog,
                            restore_files: Mapping[str, Path] | None = None, attempts: int = 3) -> BackupManifest:
    last_error: Exception | None = None
    original_stat = destination.stat()
    original_identity = (original_stat.st_dev, original_stat.st_ino)
    def require_same_destination() -> None:
        current = destination.stat()
        if (current.st_dev, current.st_ino) != original_identity:
            raise BackupRefusedError("backup destination was unmounted or replaced during this generation")
    for _attempt in range(attempts):
        if should_pause():
            raise BackupRefusedError("backup stopped before the canonical regular open")
        generation_id = uuid4().hex
        started = clock()
        staging = destination / "generations" / ("." + generation_id)
        staging.mkdir(parents=True)
        try:
            dump = staging / "catalog.copy"
            # All modes share this maintenance lock. A retirement cannot
            # delete any source while its catalog snapshot is being copied.
            from contextlib import ExitStack
            with ExitStack() as locks:
                for root in roots.values():
                    locks.enter_context(lake_activity(root / "lake" / "raw"))
                inventory = await snapshot(dump)
                files, markers = await bounded_file_work(lambda paused, current=inventory: _copy_snapshot_files(destination, roots, current, paused), should_pause)
            if should_pause():
                raise BackupRefusedError("backup stopped before the canonical regular open")
            require_same_destination()
            helpers = {name: _put_object(destination, path.read_bytes()) for name, path in (restore_files or {}).items()}
            manifest = BackupManifest(generation_id=generation_id, started_ms=started, completed_ms=clock(),
                schema_hash=inventory.schema_hash, catalog_sha256=_digest(dump.read_bytes()), catalog_rows=inventory.row_count,
                root_ids=sorted(roots), files=files, root_markers=markers, migration_ids=inventory.migration_ids, restore_files=helpers)
            raw = _json(manifest.model_dump())
            _write_durable(staging / "manifest.json", raw)
            require_same_destination()
            _write_durable(staging / "COMPLETE", _digest(raw).encode())
            await bounded_file_work(lambda paused, current=staging: verify_generation(current, should_pause=paused), should_pause)
            _require_work_window(should_pause)
            require_same_destination()
            final = staging.with_name(generation_id)
            os.replace(staging, final)
            _fsync_path(final.parent)
            return manifest
        except BackupPausedError:
            shutil.rmtree(staging)
            raise
        except (OSError, ValueError, BackupRefusedError) as exc:
            last_error = exc
            shutil.rmtree(staging, ignore_errors=False)
    raise BackupRefusedError(f"backup failed after {attempts} bounded attempts: {last_error}") from last_error


def verify_generation(generation: Path, *, expected_schema_hash: str | None = None,
                      should_pause: Callable[[], bool] = lambda: False) -> BackupManifest:
    try:
        raw = (generation / "manifest.json").read_bytes()
        if (generation / "COMPLETE").read_text() != _digest(raw):
            raise BackupRefusedError("generation has no valid complete marker")
        manifest = BackupManifest.model_validate_json(raw)
        if manifest.version != 1 or generation.name.lstrip(".") != manifest.generation_id:
            raise BackupRefusedError("generation identity or version is invalid")
        if expected_schema_hash is not None and manifest.schema_hash != expected_schema_hash:
            raise BackupRefusedError("restore schema differs from the backed-up schema")
        if _digest((generation / "catalog.copy").read_bytes()) != manifest.catalog_sha256:
            raise BackupRefusedError("catalog dump checksum is invalid")
        objects = generation.parent.parent / "objects"
        seen: set[tuple[str, str]] = set()
        for item in manifest.files:
            _require_work_window(should_pause)
            _safe_relative(item.relative_path)
            if item.root_id not in manifest.root_ids or (item.root_id, item.relative_path) in seen:
                raise BackupRefusedError("generation contains duplicate or foreign-root files")
            seen.add((item.root_id, item.relative_path))
            payload = (objects / item.sha256).read_bytes()
            if len(payload) != item.size_bytes or _digest(payload) != item.sha256:
                raise BackupRefusedError("backup file checksum or size is invalid")
        for root_id, digest in manifest.root_markers.items():
            raw_marker = (objects / digest).read_bytes()
            marker = json.loads(raw_marker)
            inventory_marker = next((item for item in manifest.files
                if item.root_id == root_id and item.relative_path == "lake/.data-root.json"), None)
            if inventory_marker is None or inventory_marker.sha256 != digest:
                raise BackupRefusedError("backup file inventory is missing its exact root marker")
            if _digest(raw_marker) != digest or marker.get("schema_version") != 1 or marker.get("data_root_id") != root_id:
                raise BackupRefusedError("backup root marker has the wrong identity")
        if set(manifest.root_markers) != set(manifest.root_ids):
            raise BackupRefusedError("backup is missing a root marker")
        for digest in manifest.restore_files.values():
            if _digest((objects / digest).read_bytes()) != digest:
                raise BackupRefusedError("backup restore tooling is corrupt")
        return manifest
    except (OSError, ValueError) as exc:
        raise BackupRefusedError(f"generation verification failed: {exc}") from exc


def manifest_digest(generation: Path) -> str:
    return _digest((generation / "manifest.json").read_bytes())


def prune_generations(destination: Path, *, daily_keep: int = 7, weekly_keep: int = 4) -> None:
    checked: list[tuple[Path, BackupManifest]] = []
    for directory in (destination / "generations").iterdir():
        if directory.name.startswith("."):
            continue
        checked.append((directory, verify_generation(directory)))
    checked.sort(key=lambda item: item[1].completed_ms, reverse=True)
    retained = {path for path, _ in checked[:max(1, daily_keep)]}
    pins = destination / "retention-pins"
    now = time.time_ns() // 1_000_000
    for path, manifest in checked:
        pin_path = pins / (manifest.generation_id + ".json")
        if not pin_path.exists():
            continue
        pin = json.loads(pin_path.read_bytes())
        if pin.get("manifest_sha256") != manifest_digest(path):
            raise BackupRefusedError("retention pin does not match its verified generation")
        if type(pin.get("retain_until_ms")) is not int:
            raise BackupRefusedError("retention pin has an invalid rollback deadline")
        if pin["retain_until_ms"] > now:
            retained.add(path)
    weeks: set[int] = set()
    for path, manifest in checked:
        week = manifest.completed_ms // 604_800_000
        if week not in weeks and len(weeks) < weekly_keep:
            retained.add(path)
            weeks.add(week)
    referenced: set[str] = set()
    for path, manifest in checked:
        if path in retained:
            referenced.update(item.sha256 for item in manifest.files)
            referenced.update(manifest.root_markers.values())
            referenced.update(manifest.restore_files.values())
        else:
            shutil.rmtree(path)
    for path in (destination / "objects").iterdir():
        if path.name not in referenced and not path.name.startswith("."):
            path.unlink()


async def restore_generation(*, generation: Path, roots: Mapping[str, Path], connection: Any,
                             schema: str = "public", should_pause: Callable[[], bool] = lambda: False) -> BackupManifest:
    if not schema.replace("_", "").isalnum() or not schema.isascii():
        raise BackupRefusedError("unsafe restore catalog schema")
    expected = await catalog_schema_hash(connection, schema=schema)
    manifest = await asyncio.to_thread(verify_generation, generation, expected_schema_hash=expected, should_pause=should_pause)
    migrations = await connection.fetch('SELECT "MigrationId" FROM public."__EFMigrationsHistory" ORDER BY "MigrationId"')
    if [row["MigrationId"] for row in migrations] != manifest.migration_ids:
        raise BackupRefusedError("restore migration history differs from the generation")
    if set(roots) != set(manifest.root_ids):
        raise BackupRefusedError("restore targets do not match the generation's root identities")
    for target in roots.values():
        if target.exists() and any(target.iterdir()):
            raise BackupRefusedError("restore requires fresh, empty target roots")
    count = await connection.fetchval(f'SELECT COUNT(*) FROM "{schema}"."DataLakeArtifacts"')
    if count:
        raise BackupRefusedError("restore requires an empty target catalog")
    objects = generation.parent.parent / "objects"
    # All verification happens before the first target mutation. A failure
    # thereafter leaves an explicitly incomplete target, never a live service.
    def restore_files(paused: Callable[[], bool]) -> None:
        _require_work_window(paused)
        for target in roots.values():
            _write_durable(target / ".restore-incomplete.json", _json({"generation_id": manifest.generation_id}))
        for item in manifest.files:
            _require_work_window(paused)
            target = roots[item.root_id] / _safe_relative(item.relative_path)
            _write_durable(target, (objects / item.sha256).read_bytes())
    await bounded_file_work(restore_files, should_pause)
    _require_work_window(should_pause)
    async with connection.transaction():
        await connection.copy_to_table("DataLakeArtifacts", schema_name=schema,
            source=io.BytesIO((generation / "catalog.copy").read_bytes()), format="binary")
        await connection.execute(f'''UPDATE "{schema}"."DataLakeArtifacts" SET "LeaseOwner"=NULL,
            "LeaseExpiresAtMs"=NULL, "LeaseGeneration"="LeaseGeneration"+1,
            "Status"=CASE WHEN "Status"='fetching' THEN 'failed' ELSE "Status" END,
            "LastError"=CASE WHEN "Status"='fetching' THEN 'internal_error' ELSE "LastError" END,
            "ErrorMessage"=CASE WHEN "Status"='fetching' THEN 'Interrupted at backup; restore starts no jobs' ELSE "ErrorMessage" END''')
        sequence = await connection.fetchval("SELECT pg_get_serial_sequence($1, 'Id')", f'"{schema}"."DataLakeArtifacts"')
        if sequence is not None:
            maximum = await connection.fetchval(f'SELECT MAX("Id") FROM "{schema}"."DataLakeArtifacts"')
            await connection.fetchval("SELECT setval($1::regclass, $2, $3)", sequence, maximum or 1, maximum is not None)
        _require_work_window(should_pause)
    for target in roots.values():
        (target / ".restore-incomplete.json").unlink()
    return manifest


def decode_restored_artifacts(manifest: BackupManifest, roots: Mapping[str, Path], *,
                              should_pause: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """Verify restored admission and run the canonical artifact readers."""
    from app.option_archive.restored_reads import decode_restored_lake
    return decode_restored_lake(manifest, roots, should_pause=should_pause)


class RestoreReceipt(BaseModel):
    version: int = 1
    generation_id: str
    manifest_sha256: str
    checked_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    passed: bool
    decoded_counts: dict[str, int]


def checked_restore_receipt(state: dict[str, Any]) -> RestoreReceipt | None:
    receipt = state.get("jobs", {}).get("restore", {}).get("receipt")
    return RestoreReceipt.model_validate(receipt) if receipt is not None else None
