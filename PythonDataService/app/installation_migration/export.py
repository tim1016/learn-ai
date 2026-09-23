"""``migrate-installation export``: write one bundle of a flat installation (#2268).

The order is the safety argument:

1. **Preflight** — every bundled volume and folder exists, no folder holds a
   secret-shaped file, the bundle path is free. A doomed export fails here,
   before it has stopped anything.
2. **Stop every bot** on every live lane (the lane records a receipt). Bots
   stay stopped whatever happens next.
3. **Read account quiet on every lane** (#2154's reader, via the coordinator,
   no drain): broker open orders empty, positions empty, no in-flight
   intent, no bot task running. Any account not flat refuses, naming the
   account and what is open. Nothing is flattened or cancelled.
4. **Stop the containers** that write a bundled volume or folder, so the copy
   is of quiescent data — clerks first, Postgres last.
5. **Copy** each volume (``podman volume export``) and folder, hash it, and
   read the identity facts (registry, markers, generations) from the copy
   itself — then write the bundle atomically.

``--check`` runs the directory read and step 3 only: it stops nothing and
writes nothing, and a running bot is reported (export would stop it) rather
than refused. Export never drains a lane, never changes an assignment, and
does not lock the old machine: the operator shuts it down by hand (#2151
owner decision 7).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.broker.fleet.records import LANE_QUIET_CONDITIONS
from app.installation_migration.bundle import (
    MANIFEST_KIND,
    MANIFEST_SCHEMA_VERSION,
    write_bundle,
)
from app.installation_migration.contents import (
    BUNDLED_FOLDERS,
    BUNDLED_VOLUMES,
    BundledVolume,
    find_secret_files,
    resolve_folder_path,
)
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.facts import staged_identity_facts
from app.installation_migration.git import GitPort
from app.installation_migration.lanes import FleetLanes, Lane
from app.installation_migration.podman import PodmanPort, VolumeInfo
from app.installation_migration.topology import containers_writing_folders, load_topology
from app.installation_migration.tree import (
    build_folder_tar,
    sha256_file,
    tree_digest_from_tar,
)
from app.utils.timestamps import now_ms_utc

Emit = Callable[[Mapping[str, object]], None]

#: The account half of lane quiet: what "flat" means for migration.
_ACCOUNT_CONDITIONS = ("broker_work_ended", "account_flat", "intents_resolved")
_CONDITION_PHRASES = dict(LANE_QUIET_CONDITIONS)
#: Stop order: lanes before the coordinator, Postgres last.
_STOP_RANK = {"clerk": 0, "qualification": 1, "fleet_control": 2, "postgres": 3}


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """One export invocation's operator inputs."""

    repo_root: Path
    bundle_path: Path | None
    operator: str
    change_ref: str
    check_only: bool = False
    lake_dir: Path | None = None


def run_export(
    request: ExportRequest,
    *,
    lanes: FleetLanes,
    podman: PodmanPort,
    git: GitPort,
    emit: Emit,
    clock: Callable[[], int] = now_ms_utc,
) -> None:
    """Run the export (or ``--check``); every refusal raises :class:`MigrationRefused`."""
    if request.check_only:
        _check(lanes, emit)
        return
    if request.bundle_path is None:
        raise ValueError("export needs a bundle path unless it is a --check")
    _export_bundle(
        request, request.bundle_path, lanes=lanes, podman=podman, git=git, emit=emit, clock=clock
    )


def _check(lanes: FleetLanes, emit: Emit) -> None:
    """``--check``: read every lane's account quiet; stop nothing, write nothing."""
    live = _live_lanes(lanes)
    emit({"step": "lanes", "lanes": [lane.clerk_id for lane in live]})
    answers = [_quiet(lanes, lane) for lane in live]
    _refuse_unless_flat(answers, require_runner_idle=False, bots_stopped=False)
    emit(
        {
            "step": "check",
            "flat": True,
            "accounts": [answer["account_id"] for answer in answers],
            "bots_running_on": [
                answer["clerk_id"] for answer in answers if not answer["runner_idle"]
            ],
            "note": "Every account is flat. Nothing was stopped or written; "
            "export stops every bot itself before it copies.",
        }
    )


def _export_bundle(
    request: ExportRequest,
    bundle: Path,
    *,
    lanes: FleetLanes,
    podman: PodmanPort,
    git: GitPort,
    emit: Emit,
    clock: Callable[[], int],
) -> None:
    volume_infos, folder_paths = _preflight(request, bundle, podman)
    emit({"step": "preflight", "volumes": sorted(volume_infos), "folders": sorted(folder_paths)})

    live = _live_lanes(lanes)
    emit({"step": "lanes", "lanes": [lane.clerk_id for lane in live]})

    receipts: dict[str, str] = {}
    for lane in live:
        receipt = lanes.stop_all_bots(
            lane, operator=request.operator, change_ref=request.change_ref
        )
        receipts[lane.clerk_id] = str(receipt["receipt_id"])
        emit(
            {
                "step": "bots-stopped",
                "clerk_id": lane.clerk_id,
                "receipt_id": receipt["receipt_id"],
                "stopped": receipt.get("stopped", []),
            }
        )

    answers = [_quiet(lanes, lane) for lane in live]
    _refuse_unless_flat(answers, require_runner_idle=True, bots_stopped=True)
    emit({"step": "accounts-quiet", "accounts": [answer["account_id"] for answer in answers]})

    topology = load_topology(request.repo_root)
    stopped = _quiesce_containers(podman, topology)
    emit({"step": "containers-stopped", "containers": stopped})

    source_commit = git.head_commit()
    source_tree_dirty = git.tree_dirty()
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".migrate-export-", dir=bundle.parent) as scratch:
        staging = Path(scratch).resolve()
        volume_entries = [
            _export_volume(podman, volume, volume_infos[volume.name], staging, emit)
            for volume in BUNDLED_VOLUMES
        ]
        folder_entries = [
            _export_folder(folder.key, folder.member, folder_paths[folder.key], staging, emit)
            for folder in BUNDLED_FOLDERS
        ]
        registry, clerk_volumes = staged_identity_facts(
            {volume.name: staging / volume.member for volume in BUNDLED_VOLUMES}, staging
        )
        manifest = {
            "kind": MANIFEST_KIND,
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "created_at_ms": clock(),
            "source_commit": source_commit,
            "source_tree_dirty": source_tree_dirty,
            "operator": request.operator,
            "change_ref": request.change_ref,
            "volumes": volume_entries,
            "folders": folder_entries,
            "registry": registry,
            "clerk_volumes": clerk_volumes,
            "lanes": [
                {
                    "clerk_id": answer["clerk_id"],
                    "broker": answer["broker"],
                    "account_id": answer["account_id"],
                    "quiet_observed_at_ms": answer["observed_at_ms"],
                    "stop_receipt_id": receipts[answer["clerk_id"]],
                }
                for answer in answers
            ],
        }
        write_bundle(
            bundle,
            manifest,
            [(entry["member"], staging / entry["member"]) for entry in volume_entries + folder_entries],
        )
    emit(
        {
            "step": "complete",
            "bundle": str(bundle),
            "source_commit": source_commit,
            "source_tree_dirty": source_tree_dirty,
            "stack": "stopped",
            "next": "Copy the bundle and deploy/fleet/env/*.env (plus the repo-root "
            ".env and PythonDataService/.env) to the new machine by hand, then shut this "
            "machine down. Never restart this stack: going back is a reverse migration.",
        }
    )


def _preflight(
    request: ExportRequest, bundle: Path, podman: PodmanPort
) -> tuple[dict[str, VolumeInfo], dict[str, Path]]:
    if bundle.exists():
        raise MigrationRefused(
            "bundle_exists",
            f"{bundle} already exists; a bundle is never overwritten.",
            details={"bundle": str(bundle)},
        )
    infos = {volume.name: podman.volume_info(volume.name) for volume in BUNDLED_VOLUMES}
    missing_volumes = sorted(name for name, info in infos.items() if info is None)
    if missing_volumes:
        raise MigrationRefused(
            "volume_missing",
            f"This host has no podman volume {', '.join(missing_volumes)}; the bundle "
            "carries every installation volume or none.",
            details={"volumes": missing_volumes},
        )
    paths = {
        folder.key: resolve_folder_path(request.repo_root, folder, lake_dir=request.lake_dir)
        for folder in BUNDLED_FOLDERS
    }
    missing_folders = sorted(str(path) for path in paths.values() if not path.is_dir())
    if missing_folders:
        raise MigrationRefused(
            "folder_missing",
            f"Bundled folder(s) {', '.join(missing_folders)} do not exist on this host.",
            details={"folders": missing_folders},
        )
    secrets = sorted(str(found) for path in paths.values() for found in find_secret_files(path))
    if secrets:
        raise MigrationRefused(
            "secret_in_bundle_source",
            f"Secret-shaped file(s) {', '.join(secrets)} sit inside a bundled folder; "
            "the bundle never carries a secret. Move them out, then retry.",
            details={"paths": secrets},
        )
    return {name: info for name, info in infos.items() if info is not None}, paths


def _live_lanes(lanes: FleetLanes) -> list[Lane]:
    live = [lane for lane in lanes.list_lanes() if lane.lifecycle_state != "retired"]
    draining = sorted(lane.clerk_id for lane in live if lane.lifecycle_state == "draining")
    if draining:
        raise MigrationRefused(
            "lane_draining",
            f"Lane(s) {', '.join(draining)} are draining. Migration moves lanes in "
            "service with their assignments effective; finish the drain first.",
            details={"clerk_ids": draining},
        )
    return live


def _quiet(lanes: FleetLanes, lane: Lane) -> dict[str, Any]:
    return {"clerk_id": lane.clerk_id, "broker": lane.broker, **lanes.account_quiet(lane)}


def _refuse_unless_flat(
    answers: list[dict[str, Any]], *, require_runner_idle: bool, bots_stopped: bool
) -> None:
    conditions = (("runner_idle",) if require_runner_idle else ()) + _ACCOUNT_CONDITIONS
    open_work = [
        {
            "clerk_id": answer["clerk_id"],
            "account_id": answer["account_id"],
            "open": [_CONDITION_PHRASES[name] for name in conditions if not answer[name]],
        }
        for answer in answers
    ]
    open_work = [entry for entry in open_work if entry["open"]]
    if not open_work:
        return
    listing = "; ".join(
        f"account {entry['account_id']} (lane {entry['clerk_id']}): {', '.join(entry['open'])}"
        for entry in open_work
    )
    tail = (
        " Every bot was stopped and stays stopped;" if bots_stopped else " Nothing was stopped;"
    )
    raise MigrationRefused(
        "accounts_not_flat",
        f"Migration moves only flat accounts, and these are not: {listing}.{tail} "
        "nothing was flattened, cancelled or written. Close the open work at the broker, "
        "then retry.",
        details={"accounts": open_work},
    )


def _quiesce_containers(podman: PodmanPort, topology: Mapping[str, Any]) -> list[str]:
    ordered = sorted(BUNDLED_VOLUMES, key=lambda volume: _STOP_RANK[volume.role])
    by_volume = [
        use.name
        for volume in ordered
        if volume.role != "postgres"
        for use in podman.containers_using_volume(volume.name)
    ]
    folder_writers = containers_writing_folders(topology, [f.key for f in BUNDLED_FOLDERS])
    database = [
        use.name
        for volume in ordered
        if volume.role == "postgres"
        for use in podman.containers_using_volume(volume.name)
    ]
    names = list(dict.fromkeys([*by_volume, *folder_writers, *database]))
    stopped: list[str] = []
    for name in names:
        if podman.container_running(name):
            podman.stop_container(name)
            stopped.append(name)
    still_running = sorted(
        {
            use.name
            for volume in BUNDLED_VOLUMES
            for use in podman.containers_using_volume(volume.name)
            if use.running
        }
        | {name for name in folder_writers if podman.container_running(name)}
    )
    if still_running:
        raise MigrationRefused(
            "container_still_running",
            f"Container(s) {', '.join(still_running)} still run against a bundled "
            "volume or folder; a copy of live data is not a migration.",
            details={"containers": still_running},
        )
    return stopped


def _export_volume(
    podman: PodmanPort, volume: BundledVolume, info: VolumeInfo, staging: Path, emit: Emit
) -> dict[str, Any]:
    target = staging / volume.member
    target.parent.mkdir(parents=True, exist_ok=True)
    podman.export_volume(volume.name, target)
    entry = {
        "name": volume.name,
        "compose_key": volume.compose_key,
        "role": volume.role,
        "driver": info.driver,
        "labels": dict(info.labels),
        "member": volume.member,
        "size_bytes": target.stat().st_size,
        "sha256": sha256_file(target),
        "content_digest": tree_digest_from_tar(target),
    }
    emit({"step": "volume-exported", "volume": volume.name, "size_bytes": entry["size_bytes"]})
    return entry


def _export_folder(
    key: str, member: str, path: Path, staging: Path, emit: Emit
) -> dict[str, Any]:
    target = staging / member
    target.parent.mkdir(parents=True, exist_ok=True)
    build_folder_tar(path, target)
    entry = {
        "key": key,
        "member": member,
        "size_bytes": target.stat().st_size,
        "sha256": sha256_file(target),
        "content_digest": tree_digest_from_tar(target),
    }
    emit({"step": "folder-exported", "folder": key, "size_bytes": entry["size_bytes"]})
    return entry


__all__ = ["ExportRequest", "run_export"]
