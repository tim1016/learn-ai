"""Checked lake generations paired with a repeatable-read binary catalog COPY.

Content-addressed objects are immutable. COMPLETE is written last and binds
all metadata, so a failed generation cannot advance the recovery point.
Verification and restore live in ``lake_restore``, the code an operator's
recovery runs too; this module adds the calendar pause around them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.data_lake import catalog_client
from app.data_lake.maintenance import lake_activity
from app.data_lake.path_policy import lake_container_within, lake_root_within, lake_subpath
from app.data_lake.root_identity import LakeRootIdentityError, resolve_root_context
from app.engine.live.identity import confine_path_to_root
from app.option_archive import lake_restore
from app.option_archive.settings import STATE_FILE_NAME
from app.research.runs.hashing import canonical_json
from app.utils.advisory_lock import try_advisory_file_lock
from app.utils.atomic_file import atomic_write_bytes, fsync_parent_dir
from app.utils.session_anchors import MAX_TIMESTAMP_MS

logger = logging.getLogger(__name__)


class BackupRefusedError(RuntimeError):
    """The chosen generation cannot establish a recoverable lake/catalog pair."""


@contextmanager
def backup_destination_activity(destination: Path, *, shared: bool = False) -> Iterator[None]:
    """Order rollback protection and retention as one destination transaction (#3432).

    A backup holds it shared while its staging folder exists; pruning and
    retirement hold it exclusively, so pruning never sees a live staging folder.
    """
    with try_advisory_file_lock(destination.resolve(strict=True) / "retention", shared=shared) as acquired:
        if not acquired:
            raise BackupRefusedError("backup retention or rollback protection is active; retry later")
        yield


class BackupPausedError(BackupRefusedError):
    """A calendar pause left no completed backup or restore."""


class BackupFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    artifact_ids: list[int] = []


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
    migration_ids: list[str]
    restore_files: dict[str, str]


class CatalogSnapshot(BaseModel):
    schema_hash: str
    row_count: int
    migration_ids: list[str]
    files: list[dict[str, Any]]


class BackupDestinationMarker(BaseModel):
    """``.backup-destination.json``: the absolute host folder the operator mounted."""

    model_config = ConfigDict(extra="forbid")
    host_destination: str


class RetentionPin(BaseModel):
    """Keeps one exact generation, named by its manifest digest, through a rollback deadline (#3432)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retain_until_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)


def require_work_window(should_pause: Callable[[], bool]) -> None:
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


def validate_destination(destination: Path, *, checkout: Path, roots: Mapping[str, Path],
                         expected_host_destination: str, host_checkout: Path | None,
                         host_lake_root: Path | None) -> None:
    # Host paths are compared as written: inside the container ``~`` names the
    # wrong home and host symlinks are invisible, so nothing is expanded.
    if not Path(expected_host_destination).is_absolute():
        raise BackupRefusedError("the backup host destination must be an absolute path; '~' is never expanded")
    if host_checkout is None or host_lake_root is None or not host_checkout.is_absolute():
        raise BackupRefusedError("absolute host checkout and lake paths are required to verify mount aliases")
    host_destination = Path(os.path.normpath(expected_host_destination))
    host_lake = host_lake_root if host_lake_root.is_absolute() else host_checkout / host_lake_root
    for forbidden in (host_checkout, host_lake):
        if host_destination.is_relative_to(os.path.normpath(forbidden)):
            raise BackupRefusedError("backup host destination must be outside the checkout and the lake, including mount aliases")
    resolved = destination.resolve(strict=True)
    if destination.is_symlink() or not destination.is_dir():
        raise BackupRefusedError("backup destination is not the configured mounted directory")
    for forbidden in (checkout, *roots.values()):
        if resolved.is_relative_to(forbidden.resolve()):
            raise BackupRefusedError("backup destination must be outside the checkout and every lake root")
    marker = destination / ".backup-destination.json"
    if not marker.is_file():
        raise BackupRefusedError("backup destination identity marker is missing; verify the host mount")
    if BackupDestinationMarker.model_validate_json(marker.read_bytes()).host_destination != expected_host_destination:
        raise BackupRefusedError("backup destination marker does not name the configured host mount")


async def dump_catalog(path: Path) -> CatalogSnapshot:
    async with catalog_client.connection() as connection, connection.transaction(isolation="repeatable_read", readonly=True):
        schema = await lake_restore.catalog_schema_hash(connection)
        migrations = await connection.fetch("SELECT version FROM public.schema_migrations ORDER BY version")
        rows = await connection.fetch('''SELECT "Id", "DataRootId", "PriceAdjustmentMode", "FilePath",
            "FileSha256", "FileSizeBytes" FROM "DataLakeArtifacts" WHERE "Status"='complete' ORDER BY "Id"''')
        count = await connection.fetchval('SELECT COUNT(*) FROM "DataLakeArtifacts"')
        with path.open("xb") as output:
            await connection.copy_from_query('SELECT * FROM "DataLakeArtifacts" ORDER BY "Id"', output=output, format="binary")
            output.flush()
            os.fsync(output.fileno())
    return CatalogSnapshot(schema_hash=schema, row_count=count, migration_ids=[row["version"] for row in migrations],
                           files=[{key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()} for row in rows])


def _put_object(destination: Path, payload: bytes, expected: str | None = None) -> str:
    digest = lake_restore.digest(payload)
    if expected is not None and digest != expected:
        raise BackupRefusedError("a source file changed after the consistent catalog snapshot")
    target = destination / "objects" / digest
    if target.exists():
        if lake_restore.digest(target.read_bytes()) != digest:
            raise BackupRefusedError("a previously retained backup object is corrupt")
        return digest
    temp = target.with_name("." + digest + "." + uuid4().hex)
    lake_restore.write_durable(temp, payload)
    os.replace(temp, target)
    fsync_parent_dir(target)
    return digest


def _copy_snapshot_files(destination: Path, roots: Mapping[str, Path], snapshot: CatalogSnapshot,
                         should_pause: Callable[[], bool] = lambda: False) -> list[BackupFile]:
    indexed: dict[tuple[str, str], BackupFile] = {}
    for row in snapshot.files:
        require_work_window(should_pause)
        root_id = row["DataRootId"]
        if root_id not in roots:
            raise BackupRefusedError("catalog snapshot references an unconfigured physical root")
        mode = row["PriceAdjustmentMode"] or "raw"
        mode_root = lake_root_within(roots[root_id], mode)
        lake_restore.safe_relative(row["FilePath"])
        relative = lake_subpath(mode) / row["FilePath"]
        source = roots[root_id] / relative
        if source.is_symlink():
            raise BackupRefusedError("catalog file escaped its physical root")
        payload = confine_path_to_root(source, mode_root, label="catalog file").read_bytes()
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
                size_bytes=len(payload), artifact_ids=[row["Id"]])
    for root_id, root in roots.items():
        try:
            resolve_root_context(UUID(root_id), base_root=root)
        except LakeRootIdentityError as exc:
            raise BackupRefusedError(f"source lake root marker does not match the catalog: {exc}") from exc
        state_path = root / STATE_FILE_NAME
        if state_path.exists():
            state_bytes = state_path.read_bytes()
            json.loads(state_bytes)
            state_digest = _put_object(destination, state_bytes)
            indexed[(root_id, state_path.name)] = BackupFile(root_id=root_id, relative_path=state_path.name,
                sha256=state_digest, size_bytes=len(state_bytes))
        # Coverage receipts beside bars/factors and the root marker are part
        # of the lake too. Never copy scratch/staging or a symlink to data
        # outside this root.
        for path in sorted(lake_container_within(root).rglob("*")):
            require_work_window(should_pause)
            if not path.is_file():
                continue
            if path.is_symlink():
                raise BackupRefusedError("lake contains a file outside its root")
            confine_path_to_root(path, root, label="lake file")
            relative_path = path.relative_to(root).as_posix()
            if (root_id, relative_path) not in indexed:
                payload = path.read_bytes()
                digest = _put_object(destination, payload)
                indexed[(root_id, relative_path)] = BackupFile(root_id=root_id, relative_path=relative_path, sha256=digest, size_bytes=len(payload))
    return sorted(indexed.values(), key=lambda item: (item.root_id, item.relative_path))


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
    with backup_destination_activity(destination, shared=True):
        for _attempt in range(attempts):
            require_work_window(should_pause)
            generation_id = uuid4().hex
            started = clock()
            staging = destination / "generations" / ("." + generation_id)
            staging.mkdir(parents=True)
            try:
                dump = staging / "catalog.copy"
                # All modes share this maintenance lock. A retirement cannot
                # delete any source while its catalog snapshot is being copied.
                with ExitStack() as locks:
                    for root in roots.values():
                        locks.enter_context(lake_activity(lake_root_within(root, "raw")))
                    inventory = await snapshot(dump)
                    files = await bounded_file_work(lambda paused, current=inventory: _copy_snapshot_files(destination, roots, current, paused), should_pause)
                require_work_window(should_pause)
                require_same_destination()
                helpers = {name: _put_object(destination, path.read_bytes()) for name, path in (restore_files or {}).items()}
                manifest = BackupManifest(generation_id=generation_id, started_ms=started, completed_ms=clock(),
                    schema_hash=inventory.schema_hash, catalog_sha256=lake_restore.digest(dump.read_bytes()), catalog_rows=inventory.row_count,
                    root_ids=sorted(roots), files=files, migration_ids=inventory.migration_ids, restore_files=helpers)
                raw = lake_restore.canonical(manifest.model_dump())
                lake_restore.write_durable(staging / "manifest.json", raw)
                require_same_destination()
                lake_restore.write_durable(staging / "COMPLETE", lake_restore.digest(raw).encode())
                await bounded_file_work(lambda paused, current=staging: verify_generation(current, should_pause=paused), should_pause)
                require_work_window(should_pause)
                require_same_destination()
                final = staging.with_name(generation_id)
                os.replace(staging, final)
                fsync_parent_dir(final)
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
        return BackupManifest.model_validate(lake_restore.verify(
            generation, expected_schema_hash=expected_schema_hash, checkpoint=lambda: require_work_window(should_pause)))
    except (OSError, ValueError) as exc:
        raise BackupRefusedError(f"generation verification failed: {exc}") from exc


def manifest_digest(generation: Path) -> str:
    return lake_restore.digest((generation / "manifest.json").read_bytes())


def _pin_path(destination: Path, generation_id: str) -> Path:
    return destination / "retention-pins" / f"{generation_id}.json"


def read_pin(destination: Path, generation_id: str) -> RetentionPin | None:
    """The generation's rollback pin, or ``None``; a pin that does not parse is refused."""
    path = _pin_path(destination, generation_id)
    if not path.exists():
        return None
    try:
        return RetentionPin.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise BackupRefusedError("rollback pin is not a manifest-bound deadline") from exc


def extend_pin(destination: Path, generation_id: str, until_ms: int, *, manifest_sha256: str) -> RetentionPin:
    """Pin the generation through ``until_ms``, keeping any later deadline it already has."""
    current = read_pin(destination, generation_id)
    if current is not None and current.manifest_sha256 != manifest_sha256:
        raise BackupRefusedError("existing rollback pin does not match this restore point")
    pin = RetentionPin(manifest_sha256=manifest_sha256,
                       retain_until_ms=until_ms if current is None else max(until_ms, current.retain_until_ms))
    atomic_write_bytes(_pin_path(destination, generation_id), canonical_json(pin.model_dump()).encode())
    return pin


def _objects_of(manifest: BackupManifest) -> set[str]:
    return {item.sha256 for item in manifest.files} | set(manifest.restore_files.values())


def prune_generations(destination: Path, *, clock: Callable[[], int], daily_keep: int = 7, weekly_keep: int = 4,
                      should_pause: Callable[[], bool] = lambda: False) -> list[str]:
    """Apply retention; return the generations kept unverified for an operator to diagnose.

    An unverifiable generation is never removed and never counts as good, so
    one damaged generation cannot stop retention for the others.
    """
    with backup_destination_activity(destination):
        checked: list[tuple[Path, BackupManifest]] = []
        unverified: list[Path] = []
        for directory in (destination / "generations").iterdir():
            require_work_window(should_pause)
            if not directory.is_dir():
                continue
            if directory.name.startswith("."):
                # Every live backup holds this lock shared, so this staging
                # folder belongs to a process that died.
                shutil.rmtree(directory)
                continue
            try:
                checked.append((directory, verify_generation(directory, should_pause=should_pause)))
            except BackupPausedError:
                raise
            except BackupRefusedError as exc:
                logger.warning("Backup generation failed verification; kept for diagnosis", extra={
                    "action": "backup_generation_unverified", "generation_id": directory.name, "error": str(exc)})
                unverified.append(directory)
        checked.sort(key=lambda item: item[1].completed_ms, reverse=True)
        retained = {path for path, _ in checked[:max(1, daily_keep)]}
        now = clock()
        for path, manifest in checked:
            require_work_window(should_pause)
            pin = read_pin(destination, manifest.generation_id)
            if pin is None:
                continue
            if pin.manifest_sha256 != manifest_digest(path):
                raise BackupRefusedError("retention pin does not match its verified generation")
            if pin.retain_until_ms > now:
                retained.add(path)
        weeks: set[int] = set()
        for path, manifest in checked:
            require_work_window(should_pause)
            week = manifest.completed_ms // 604_800_000
            if week not in weeks and len(weeks) < weekly_keep:
                retained.add(path)
                weeks.add(week)
        referenced: set[str] = set()
        sweep_objects = True
        for path in unverified:
            try:
                referenced.update(_objects_of(BackupManifest.model_validate_json((path / "manifest.json").read_bytes())))
            except (OSError, ValueError):
                # Its objects are unknown, so no object is provably unreferenced.
                sweep_objects = False
        for path, manifest in checked:
            require_work_window(should_pause)
            if path in retained:
                referenced.update(_objects_of(manifest))
            else:
                shutil.rmtree(path)
        if sweep_objects:
            for path in (destination / "objects").iterdir():
                require_work_window(should_pause)
                if path.name not in referenced:
                    path.unlink()
        return [path.name for path in unverified]


async def restore_generation(*, generation: Path, roots: Mapping[str, Path], connection: Any,
                             schema: str = "public", should_pause: Callable[[], bool] = lambda: False) -> BackupManifest:
    """``lake_restore.restore`` with the calendar pause between files and before the import commits."""
    try:
        manifest = await lake_restore.restore(generation, roots, connection, schema=schema,
            checkpoint=lambda: require_work_window(should_pause),
            run_files=lambda work: bounded_file_work(lambda paused: work(lambda: require_work_window(paused)), should_pause))
    except lake_restore.RestoreRefusedError as exc:
        raise BackupRefusedError(str(exc)) from exc
    return BackupManifest.model_validate(manifest)


class RestoreReceipt(BaseModel):
    version: int = 1
    generation_id: str
    manifest_sha256: str
    checked_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    passed: bool
    decoded_counts: dict[str, int]
