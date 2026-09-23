"""``migrate-installation import``: restore a bundle on the new host (#2268).

The order is the safety argument, and nothing on the destination changes
until every check before step 6 has passed:

1. **Env files.** Every ``deploy/fleet/env/*.env`` the topology reads, the
   repo-root ``.env`` and ``PythonDataService/.env`` must be present — the
   operator copies them by hand; the bundle never carries them. Presence
   only: their values are never read.
2. **Code and cluster.** The checked-out commit must be the manifest's commit
   or a descendant of it, by git ancestry; a bundle exported from a dirty
   tree needs the operator's explicit acknowledgement; and the bundled
   Postgres cluster must be the major version this checkout's Compose image
   runs.
3. **Stack stopped** and **room to work.** No container may reference a
   bundled volume, and no folder writer may be running; the staging, aside
   and folder filesystems must have room for the whole import.
4. **Bundle verified before anything is touched, and before any schema
   upgrade can run:** each member's SHA-256, each volume tree's content
   digest, and every folder **extracted** (next to where it will live,
   through the safe ``data`` filter) and digested there — so an unsafe
   member refuses here, not half-way through a restore. The identity facts
   read back from the staged copy — registry identity, clerks, assignment
   and authority generations, every lane marker proven through the
   canonical volume gate, the Postgres version — must equal the manifest's.
   Registry ``volume_root``s and approved endpoints are checked against this
   host's topology and any re-approval they need is **reported, never
   performed**.
5. **Existing data moved aside**, never deleted: each existing volume is
   exported to a dated aside directory and that copy read back whole before
   the volume is removed; each existing folder is moved there whole.
6. **Restore** (volumes imported, each staged folder renamed into place),
   then **re-verify the destination** against the manifest's content digests.

Any failure once step 5 has begun is ``restore_incomplete``: it names the
aside directory, its ``moved-aside.json``, and exactly which volumes and
folders were restored, and writes the same as a partial receipt.

The stack is left stopped. Go-live — the IB Gateway gate and the operator's
"old machine is off" confirmation — is #2269's, not this command's.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.installation_migration.bundle import Manifest, extract_verified_members, read_manifest
from app.installation_migration.contents import (
    BUNDLED_FOLDERS,
    BUNDLED_VOLUMES,
    resolve_folder_path,
)
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.facts import (
    ClerkVolumeFacts,
    PostgresFacts,
    RegistryFacts,
    read_postgres_facts,
    staged_identity_facts,
)
from app.installation_migration.git import GitPort
from app.installation_migration.podman import PodmanPort, VolumeInfo
from app.installation_migration.topology import (
    containers_writing_folders,
    deployment_namespace,
    host_resolution_report,
    load_topology,
    postgres_image_major,
    required_env_files,
)
from app.installation_migration.tree import (
    extract_tar,
    sha256_file,
    tree_digest_from_dir,
    tree_digest_from_tar,
)
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

Emit = Callable[[Mapping[str, object]], None]
DiskFree = Callable[[Path], int]

IMPORT_RECEIPT = "import-receipt.json"
IMPORT_INCOMPLETE = "import-incomplete.json"
ASIDE_RECORD = "moved-aside.json"

#: The staging filesystem holds the verified members, the extracted identity
#: volumes and the post-restore re-exports at once: roughly three bundles.
_STAGING_BUNDLE_MULTIPLE = 3


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """One import invocation's operator inputs."""

    repo_root: Path
    bundle_path: Path
    aside_dir: Path | None = None
    lake_dir: Path | None = None
    accept_dirty_source: bool = False


def default_aside_root(repo_root: Path) -> Path:
    """Beside the checkout, never inside it, so no ``git add`` can sweep it up."""
    return repo_root.parent / f"{repo_root.name}-migration-aside"


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass
class _Progress:
    """What has changed on the destination so far — the partial receipt."""

    moved_volumes: list[dict[str, Any]] = field(default_factory=list)
    moved_folders: list[dict[str, str]] = field(default_factory=list)
    removed_volumes: list[str] = field(default_factory=list)
    restored_volumes: list[str] = field(default_factory=list)
    restored_folders: list[str] = field(default_factory=list)

    def aside_record(self) -> dict[str, Any]:
        return {"volumes": self.moved_volumes, "folders": self.moved_folders}


def run_import(
    request: ImportRequest,
    *,
    podman: PodmanPort,
    git: GitPort,
    emit: Emit,
    clock: Callable[[], int] = now_ms_utc,
    disk_free: DiskFree = _disk_free,
) -> None:
    """Run the import; every refusal raises :class:`MigrationRefused`."""
    manifest = read_manifest(request.bundle_path)
    emit(
        {
            "step": "manifest",
            "source_commit": manifest.source_commit,
            "source_tree_dirty": manifest.source_tree_dirty,
            "created_at_ms": manifest.created_at_ms,
            "registry_id": manifest.registry.registry_id,
            "pg_version": manifest.postgres.pg_version,
        }
    )
    _require_dirty_source_acknowledged(manifest, request)
    topology = load_topology(request.repo_root)
    _require_env_files(request.repo_root, topology)
    _require_postgres_major(manifest, topology)
    destination_commit = _require_code_not_older(git, manifest.source_commit)
    emit({"step": "code", "destination_commit": destination_commit})
    folder_paths = {
        folder.key: resolve_folder_path(request.repo_root, folder, lake_dir=request.lake_dir)
        for folder in BUNDLED_FOLDERS
    }
    _require_stack_stopped(podman, topology)

    started_at_ms = clock()
    aside = (request.aside_dir or default_aside_root(request.repo_root)) / f"import-{started_at_ms}"
    if aside.exists():
        raise MigrationRefused(
            "aside_exists",
            f"{aside} already exists; moved-aside data is never mixed or overwritten.",
            details={"aside": str(aside)},
        )
    _require_disk_space(podman, manifest, aside.parent, folder_paths, disk_free)
    aside.parent.mkdir(parents=True, exist_ok=True)

    staged_folders = {
        key: path.parent / f".{path.name}.migrate-import-{started_at_ms}"
        for key, path in folder_paths.items()
    }
    progress = _Progress()
    try:
        with tempfile.TemporaryDirectory(prefix=".migrate-import-", dir=aside.parent) as scratch:
            staging = Path(scratch).resolve()
            staged = extract_verified_members(request.bundle_path, manifest, staging / "members")
            emit({"step": "bundle-verified", "members": sorted(staged)})
            _require_volume_digests(manifest, staged)
            _stage_folders(manifest, staged, staged_folders)
            emit({"step": "folders-staged", "folders": {k: str(p) for k, p in staged_folders.items()}})
            identity = staged_identity_facts(
                {volume.name: staged[volume.member] for volume in BUNDLED_VOLUMES}, staging
            )
            database = next(volume for volume in BUNDLED_VOLUMES if volume.role == "postgres")
            postgres = read_postgres_facts(staged[database.member], volume=database.name)
            _require_identity(manifest, identity.registry, identity.clerk_volumes, postgres)
            registry = identity.registry
            emit(
                {
                    "step": "identity-verified",
                    "registry_id": registry.registry_id,
                    "clerks": [clerk.clerk_id for clerk in registry.clerks],
                }
            )
            resolution = host_resolution_report(
                registry, topology, namespace=deployment_namespace(request.repo_root)
            )
            emit(
                {
                    "step": "host-resolution",
                    "clerks": resolution,
                    "reapproval_required": [
                        entry["clerk_id"] for entry in resolution if entry["reapproval_required"]
                    ],
                }
            )

            # From here on the destination changes; every failure must say
            # exactly what changed and where the preserved data is.
            try:
                _move_aside(podman, manifest, folder_paths, aside, progress)
                emit({"step": "moved-aside", "aside": str(aside), **progress.aside_record()})
                _restore(podman, manifest, folder_paths, staged, staged_folders, progress)
                emit({"step": "restored"})
                _verify_destination(podman, manifest, folder_paths, staging / "verify")
                emit({"step": "destination-verified"})
            except Exception as exc:
                # Deliberately broad: a podman, filesystem or verification
                # failure after the first move all leave the same half-changed
                # destination, and the operator needs the same account of it.
                raise _restore_incomplete(request, manifest, aside, progress, exc) from exc
    finally:
        for staged_dir in staged_folders.values():
            if staged_dir.exists():
                shutil.rmtree(staged_dir)

    receipt = {
        "imported_at_ms": started_at_ms,
        "completed_at_ms": clock(),
        "bundle": str(request.bundle_path),
        "source_commit": manifest.source_commit,
        "source_tree_dirty": manifest.source_tree_dirty,
        "destination_commit": destination_commit,
        "registry_id": manifest.registry.registry_id,
        "moved_aside": progress.aside_record(),
        "host_resolution": resolution,
        "stack": "stopped",
    }
    atomic_write_bytes(
        aside / IMPORT_RECEIPT, json.dumps(receipt, sort_keys=True, indent=2).encode("utf-8")
    )
    reapproval = [entry["clerk_id"] for entry in resolution if entry["reapproval_required"]]
    emit(
        {
            "step": "complete",
            "receipt": str(aside / IMPORT_RECEIPT),
            "stack": "stopped",
            "reapproval_required": reapproval,
            "next": (
                "Re-approve the named clerks' endpoints (manage_broker_fleet approve-endpoint) "
                "before go-live. "
                if reapproval
                else ""
            )
            + "The stack stays stopped; go-live (IB Gateway reachable, old machine confirmed "
            "off) is a separate step. Bots stay stopped until the operator starts them.",
        }
    )


def _require_dirty_source_acknowledged(manifest: Manifest, request: ImportRequest) -> None:
    if manifest.source_tree_dirty and not request.accept_dirty_source:
        raise MigrationRefused(
            "source_tree_dirty_unacknowledged",
            f"The bundle was exported from a checkout with uncommitted changes, so commit "
            f"{manifest.source_commit} does not fully describe the code that wrote its data. "
            "Pass --accept-dirty-source to restore it anyway.",
            details={"source_commit": manifest.source_commit},
        )


def _require_env_files(repo_root: Path, topology: Mapping[str, Any]) -> None:
    missing = [str(path) for path in required_env_files(repo_root, topology) if not path.is_file()]
    if missing:
        raise MigrationRefused(
            "env_files_missing",
            f"{', '.join(missing)} must be copied here by hand before import; the "
            "bundle never carries a secret.",
            details={"paths": missing},
        )


def _require_postgres_major(manifest: Manifest, topology: Mapping[str, Any]) -> None:
    database = next(volume for volume in BUNDLED_VOLUMES if volume.role == "postgres")
    image_major = postgres_image_major(topology, volume_key=database.compose_key)
    bundle_major = manifest.postgres.pg_version.split(".")[0]
    if image_major is not None and image_major != bundle_major:
        raise MigrationRefused(
            "postgres_major_mismatch",
            f"The bundled database cluster is Postgres {manifest.postgres.pg_version}, and "
            f"this checkout runs Postgres {image_major}; a cluster only opens under its own "
            "major version. Upgrade it (pg_upgrade) as its own step.",
            details={
                "bundle_pg_version": manifest.postgres.pg_version,
                "destination_image_major": image_major,
            },
        )


def _require_code_not_older(git: GitPort, source_commit: str) -> str:
    head = git.head_commit()
    if not git.commit_exists(source_commit):
        raise MigrationRefused(
            "source_commit_unknown",
            f"This checkout does not know the bundle's commit {source_commit}; fetch it "
            "(or check out code that contains it) and retry.",
            details={"source_commit": source_commit, "destination_commit": head},
        )
    if not git.is_ancestor(source_commit, head):
        raise MigrationRefused(
            "destination_code_older",
            f"The checked-out commit {head} is not {source_commit} or a descendant of it; "
            "a bundle is restored only by the same code or newer.",
            details={"source_commit": source_commit, "destination_commit": head},
        )
    return head


def _require_stack_stopped(podman: PodmanPort, topology: Mapping[str, Any]) -> None:
    holders = sorted(
        {
            f"{use.name} ({'running' if use.running else 'stopped'}) uses {volume.name}"
            for volume in BUNDLED_VOLUMES
            for use in podman.containers_using_volume(volume.name)
        }
        | {
            f"{name} (running) writes a bundled folder"
            for name in containers_writing_folders(topology, [f.key for f in BUNDLED_FOLDERS])
            if podman.container_running(name)
        }
    )
    if holders:
        raise MigrationRefused(
            "destination_stack_present",
            f"{'; '.join(holders)}. Bring the destination stack down (containers "
            "removed, volumes kept) before import.",
            details={"containers": holders},
        )


def _existing_ancestor(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _require_disk_space(
    podman: PodmanPort,
    manifest: Manifest,
    staging_root: Path,
    folder_paths: Mapping[str, Path],
    disk_free: DiskFree,
) -> None:
    """Refuse up front rather than run out of room half-way through a restore.

    The staging filesystem (which also holds the aside directory) needs about
    three bundles plus a copy of every volume already here; each folder is
    extracted next to its destination, so that folder's filesystem needs it
    once more. An existing volume podman cannot size is estimated at its
    bundled size.
    """
    bundle_bytes = sum(entry.size_bytes for entry in (*manifest.volumes, *manifest.folders))
    existing_bytes = 0
    for entry in manifest.volumes:
        if podman.volume_info(entry.name) is not None:
            size = podman.volume_size_bytes(entry.name)
            existing_bytes += entry.size_bytes if size is None else size
    needs: list[tuple[Path, int]] = [
        (staging_root, _STAGING_BUNDLE_MULTIPLE * bundle_bytes + existing_bytes)
    ]
    needs.extend(
        (folder_paths[entry.key].parent, entry.size_bytes) for entry in manifest.folders
    )
    by_device: dict[int, tuple[Path, int]] = {}
    for path, required in needs:
        anchor = _existing_ancestor(path)
        device = os.stat(anchor).st_dev
        first, total = by_device.get(device, (anchor, 0))
        by_device[device] = (first, total + required)
    short = [
        {"path": str(anchor), "required_bytes": required, "free_bytes": free}
        for anchor, required in by_device.values()
        if (free := disk_free(anchor)) < required
    ]
    if short:
        listing = "; ".join(
            f"{entry['path']} needs {entry['required_bytes']} bytes, has {entry['free_bytes']}"
            for entry in short
        )
        raise MigrationRefused(
            "insufficient_disk_space",
            f"Not enough free disk for this import ({listing}). Nothing was changed; free "
            "space or pass --aside-dir on a larger disk, then retry.",
            details={"filesystems": short},
        )


def _require_volume_digests(manifest: Manifest, staged: Mapping[str, Path]) -> None:
    mismatched = [
        entry.member
        for entry in manifest.volumes
        if tree_digest_from_tar(staged[entry.member]) != entry.content_digest
    ]
    if mismatched:
        raise MigrationRefused(
            "bundle_content_digest_mismatch",
            f"{', '.join(mismatched)} do not hold the content their manifest describes.",
            details={"members": mismatched},
        )


def _stage_folders(
    manifest: Manifest, staged: Mapping[str, Path], staged_folders: Mapping[str, Path]
) -> None:
    """Extract each folder beside its destination and prove it there.

    Beside, so the restore is a same-filesystem rename; extracted now, so an
    unsafe member (the ``data`` filter refuses it) or a digest mismatch stops
    the import before anything on the destination has moved.
    """
    mismatched: list[str] = []
    for entry in manifest.folders:
        target = staged_folders[entry.key]
        target.mkdir(parents=True)
        extract_tar(staged[entry.member], target)
        if tree_digest_from_dir(target) != entry.content_digest:
            mismatched.append(entry.member)
    if mismatched:
        raise MigrationRefused(
            "bundle_content_digest_mismatch",
            f"{', '.join(mismatched)} do not hold the content their manifest describes.",
            details={"members": mismatched},
        )


def _require_identity(
    manifest: Manifest,
    registry: RegistryFacts,
    clerk_volumes: tuple[ClerkVolumeFacts, ...],
    postgres: PostgresFacts,
) -> None:
    if registry != manifest.registry:
        differing = sorted(
            name
            for name in RegistryFacts.model_fields
            if getattr(registry, name) != getattr(manifest.registry, name)
        )
        raise MigrationRefused(
            "registry_identity_mismatch",
            f"The bundled registry disagrees with its manifest on {', '.join(differing)} "
            "(registry id, clerks, assignment generations or endpoints).",
            details={"fields": differing},
        )
    recorded = {entry.volume: entry for entry in manifest.clerk_volumes}
    for observed in clerk_volumes:
        expected = recorded.get(observed.volume)
        if expected != observed:
            raise MigrationRefused(
                "volume_identity_mismatch",
                f"Volume {observed.volume}'s marker or account authority generations "
                "disagree with the manifest; a lane volume is restored only with the "
                "identity it was exported with.",
                details={
                    "volume": observed.volume,
                    "expected": None if expected is None else expected.model_dump(),
                    "observed": observed.model_dump(),
                },
            )
    if set(recorded) != {entry.volume for entry in clerk_volumes}:
        raise MigrationRefused(
            "volume_identity_mismatch",
            "The manifest's lane volumes are not the bundle's lane volumes.",
            details={"manifest": sorted(recorded)},
        )
    if postgres != manifest.postgres:
        raise MigrationRefused(
            "postgres_identity_mismatch",
            f"The bundled cluster says Postgres {postgres.pg_version}; its manifest says "
            f"{manifest.postgres.pg_version}.",
            details={"observed": postgres.pg_version, "expected": manifest.postgres.pg_version},
        )


def _write_aside_record(aside: Path, progress: _Progress) -> None:
    atomic_write_bytes(
        aside / ASIDE_RECORD,
        json.dumps(progress.aside_record(), sort_keys=True, indent=2).encode("utf-8"),
    )


def _move_aside(
    podman: PodmanPort,
    manifest: Manifest,
    folder_paths: Mapping[str, Path],
    aside: Path,
    progress: _Progress,
) -> None:
    """Preserve everything a restore would overwrite; remove only what is preserved.

    The record is rewritten after every move, so it is current whenever a
    later step fails. A volume is removed only after its aside copy has been
    read back to the end.
    """
    (aside / "volumes").mkdir(parents=True)
    (aside / "folders").mkdir()
    _write_aside_record(aside, progress)
    for entry in manifest.volumes:
        info = podman.volume_info(entry.name)
        if info is None:
            continue
        copy = aside / "volumes" / f"{info.name}.tar"
        podman.export_volume(info.name, copy)
        progress.moved_volumes.append(
            {
                "name": info.name,
                "driver": info.driver,
                "labels": dict(info.labels),
                "copy": str(copy),
                "sha256": sha256_file(copy),
                "content_digest": tree_digest_from_tar(copy),
            }
        )
        _write_aside_record(aside, progress)
    for entry in manifest.folders:
        path = folder_paths[entry.key]
        if not path.exists():
            continue
        destination = aside / "folders" / entry.key.replace("/", "__")
        shutil.move(str(path), str(destination))
        progress.moved_folders.append({"key": entry.key, "from": str(path), "copy": str(destination)})
        _write_aside_record(aside, progress)
    for moved in progress.moved_volumes:
        podman.remove_volume(moved["name"])
        progress.removed_volumes.append(moved["name"])


def _restore(
    podman: PodmanPort,
    manifest: Manifest,
    folder_paths: Mapping[str, Path],
    staged: Mapping[str, Path],
    staged_folders: Mapping[str, Path],
    progress: _Progress,
) -> None:
    for entry in manifest.volumes:
        podman.create_volume(VolumeInfo(name=entry.name, driver=entry.driver, labels=entry.labels))
        podman.import_volume(entry.name, staged[entry.member])
        progress.restored_volumes.append(entry.name)
    for entry in manifest.folders:
        path = folder_paths[entry.key]
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_folders[entry.key], path)
        progress.restored_folders.append(entry.key)


def _verify_destination(
    podman: PodmanPort,
    manifest: Manifest,
    folder_paths: Mapping[str, Path],
    scratch: Path,
) -> None:
    scratch.mkdir(parents=True)
    mismatched: list[str] = []
    for entry in manifest.volumes:
        export = scratch / f"{entry.name}.tar"
        podman.export_volume(entry.name, export)
        if tree_digest_from_tar(export) != entry.content_digest:
            mismatched.append(entry.name)
    for entry in manifest.folders:
        if tree_digest_from_dir(folder_paths[entry.key]) != entry.content_digest:
            mismatched.append(str(folder_paths[entry.key]))
    if mismatched:
        raise MigrationRefused(
            "destination_verification_failed",
            f"After restore, {', '.join(mismatched)} do not match the manifest.",
            details={"mismatched": mismatched},
        )


def _listing(items: Iterable[str]) -> str:
    return ", ".join(items) or "none"


def _restore_incomplete(
    request: ImportRequest,
    manifest: Manifest,
    aside: Path,
    progress: _Progress,
    cause: Exception,
) -> MigrationRefused:
    """Write the partial receipt and name exactly what changed."""
    cause_json = (
        cause.to_json()
        if isinstance(cause, MigrationRefused)
        else {"error": f"{type(cause).__name__}: {cause}", "reason": type(cause).__name__}
    )
    record = {
        "bundle": str(request.bundle_path),
        "source_commit": manifest.source_commit,
        "aside": str(aside),
        "aside_record": str(aside / ASIDE_RECORD),
        "moved_aside": progress.aside_record(),
        "removed_volumes": progress.removed_volumes,
        "restored_volumes": progress.restored_volumes,
        "restored_folders": progress.restored_folders,
        "cause": cause_json,
        "stack": "stopped",
    }
    aside.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        aside / IMPORT_INCOMPLETE, json.dumps(record, sort_keys=True, indent=2).encode("utf-8")
    )
    return MigrationRefused(
        "restore_incomplete",
        f"The import stopped part-way: {cause_json['error']}. Existing data was moved aside "
        f"to {aside} (listed in {aside / ASIDE_RECORD}); volumes removed after being "
        f"preserved: {_listing(progress.removed_volumes)}; restored so far: volumes "
        f"{_listing(progress.restored_volumes)}, folders {_listing(progress.restored_folders)}. "
        f"Do not start the stack. {aside / IMPORT_INCOMPLETE} records exactly what changed; "
        "fix the cause and re-run the import (it moves this partial state aside too), or "
        "restore the moved-aside data by hand.",
        details={key: value for key, value in record.items() if key != "moved_aside"},
    )


__all__ = [
    "ASIDE_RECORD",
    "IMPORT_INCOMPLETE",
    "IMPORT_RECEIPT",
    "ImportRequest",
    "default_aside_root",
    "run_import",
]
