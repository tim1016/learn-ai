"""``migrate-installation import``: restore a bundle on the new host (#2268).

The order is the safety argument, and nothing on the destination changes
until every check before step 5 has passed:

1. **Env files.** Every ``deploy/fleet/env/*.env`` the topology reads must be
   present — the operator copies them by hand; the bundle never carries them.
2. **Code.** The checked-out commit must be the manifest's commit or a
   descendant of it, by git ancestry.
3. **Stack stopped.** No container may reference a bundled volume, and no
   folder writer may be running: a restore under a live process is not one.
4. **Bundle verified before anything is touched, and before any schema
   upgrade can run:** each member's SHA-256, each tree's content digest, and
   the identity facts read back from the staged copy — registry identity,
   clerks, assignment and authority generations, and every lane marker proven
   through the canonical volume gate — must equal the manifest's. Registry
   ``volume_root``s and approved endpoints are checked against this host's
   topology and any re-approval they need is **reported, never performed**.
5. **Existing data moved aside**, never deleted: each existing volume is
   exported to a dated aside directory before it is removed, each existing
   folder is moved there whole.
6. **Restore**, then **re-verify the destination** against the manifest's
   content digests (volumes re-exported, folders re-walked).

The stack is left stopped. Go-live — the IB Gateway gate and the operator's
"old machine is off" confirmation — is #2269's, not this command's.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    RegistryFacts,
    staged_identity_facts,
)
from app.installation_migration.git import GitPort
from app.installation_migration.podman import PodmanPort, VolumeInfo
from app.installation_migration.topology import (
    containers_writing_folders,
    deployment_namespace,
    host_resolution_report,
    load_topology,
    required_fleet_env_files,
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

IMPORT_RECEIPT = "import-receipt.json"
ASIDE_RECORD = "moved-aside.json"


@dataclass(frozen=True, slots=True)
class ImportRequest:
    """One import invocation's operator inputs."""

    repo_root: Path
    bundle_path: Path
    aside_dir: Path | None = None
    lake_dir: Path | None = None


def default_aside_root(repo_root: Path) -> Path:
    """Beside the checkout, never inside it, so no ``git add`` can sweep it up."""
    return repo_root.parent / f"{repo_root.name}-migration-aside"


def run_import(
    request: ImportRequest,
    *,
    podman: PodmanPort,
    git: GitPort,
    emit: Emit,
    clock: Callable[[], int] = now_ms_utc,
) -> None:
    """Run the import; every refusal raises :class:`MigrationRefused`."""
    manifest = read_manifest(request.bundle_path)
    emit(
        {
            "step": "manifest",
            "source_commit": manifest.source_commit,
            "created_at_ms": manifest.created_at_ms,
            "registry_id": manifest.registry.registry_id,
        }
    )
    topology = load_topology(request.repo_root)
    _require_env_files(request.repo_root, topology)
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
    aside.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".migrate-import-", dir=aside.parent) as scratch:
        staging = Path(scratch).resolve()
        staged = extract_verified_members(request.bundle_path, manifest, staging / "members")
        emit({"step": "bundle-verified", "members": sorted(staged)})
        _require_digests(manifest, staged)
        identity = staged_identity_facts(
            {volume.name: staged[volume.member] for volume in BUNDLED_VOLUMES}, staging
        )
        registry = identity.registry
        _require_identity(manifest, registry, identity.clerk_volumes)
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

        moved = _move_aside(podman, manifest, folder_paths, aside)
        emit({"step": "moved-aside", "aside": str(aside), **moved})
        _restore(podman, manifest, folder_paths, staged)
        emit({"step": "restored"})
        _verify_destination(podman, manifest, folder_paths, staging / "verify")
        emit({"step": "destination-verified"})

    receipt = {
        "imported_at_ms": started_at_ms,
        "completed_at_ms": clock(),
        "bundle": str(request.bundle_path),
        "source_commit": manifest.source_commit,
        "destination_commit": destination_commit,
        "registry_id": manifest.registry.registry_id,
        "moved_aside": moved,
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


def _require_env_files(repo_root: Path, topology: Mapping[str, Any]) -> None:
    missing = [str(path) for path in required_fleet_env_files(repo_root, topology) if not path.is_file()]
    if missing:
        raise MigrationRefused(
            "fleet_env_files_missing",
            f"{', '.join(missing)} must be copied here by hand before import; the "
            "bundle never carries a secret.",
            details={"paths": missing},
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


def _require_digests(manifest: Manifest, staged: Mapping[str, Path]) -> None:
    mismatched = [
        member
        for member, entry in manifest.member_entries().items()
        if tree_digest_from_tar(staged[member]) != entry.content_digest
    ]
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


def _move_aside(
    podman: PodmanPort,
    manifest: Manifest,
    folder_paths: Mapping[str, Path],
    aside: Path,
) -> dict[str, Any]:
    """Preserve everything a restore would overwrite; remove only what is preserved."""
    (aside / "volumes").mkdir(parents=True)
    (aside / "folders").mkdir()
    volumes: list[dict[str, Any]] = []
    existing: list[str] = []
    for entry in manifest.volumes:
        info = podman.volume_info(entry.name)
        if info is None:
            continue
        copy = aside / "volumes" / f"{info.name}.tar"
        podman.export_volume(info.name, copy)
        volumes.append(
            {
                "name": info.name,
                "driver": info.driver,
                "labels": dict(info.labels),
                "copy": str(copy),
                "sha256": sha256_file(copy),
            }
        )
        existing.append(info.name)
    folders: list[dict[str, str]] = []
    for entry in manifest.folders:
        path = folder_paths[entry.key]
        if not path.exists():
            continue
        destination = aside / "folders" / entry.key.replace("/", "__")
        shutil.move(str(path), str(destination))
        folders.append({"key": entry.key, "from": str(path), "copy": str(destination)})
    record = {"volumes": volumes, "folders": folders}
    atomic_write_bytes(
        aside / ASIDE_RECORD, json.dumps(record, sort_keys=True, indent=2).encode("utf-8")
    )
    for name in existing:
        podman.remove_volume(name)
    return record


def _restore(
    podman: PodmanPort,
    manifest: Manifest,
    folder_paths: Mapping[str, Path],
    staged: Mapping[str, Path],
) -> None:
    for entry in manifest.volumes:
        podman.create_volume(VolumeInfo(name=entry.name, driver=entry.driver, labels=entry.labels))
        podman.import_volume(entry.name, staged[entry.member])
    for entry in manifest.folders:
        path = folder_paths[entry.key]
        path.mkdir(parents=True)
        extract_tar(staged[entry.member], path)


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
            f"After restore, {', '.join(mismatched)} do not match the manifest. The "
            "moved-aside data is untouched; do not start the stack.",
            details={"mismatched": mismatched},
        )


__all__ = ["ImportRequest", "default_aside_root", "run_import"]
