"""``migrate-installation export``: write one bundle of a flat installation (#2268).

The order is the safety argument:

1. **Preflight** — every bundled volume and folder exists, no folder holds
   an escaping symlink, the bundle path is free, and the tracked working
   tree is clean (or the operator overrode that, which the manifest
   records). A doomed export fails here, before it has touched anything.
   Every secret-shaped file in a bundled folder is skipped and named — in
   this step's output, in the manifest and in the final step — never
   carried (#2269).
2. **Read account quiet on every lane** (#2154's reader, via the
   coordinator, no drain): broker open orders empty, positions empty, no
   in-flight intent. Any account not flat refuses, naming the account and
   what is open — **before any bot is stopped**: a bot's Stop cancels its own
   working entry orders, so stopping a lane that still holds a position
   would leave that position unmanaged.
3. **Stop every bot** on every live lane (the lane records a receipt). Bots
   stay stopped whatever happens next.
4. **Re-read account quiet on every lane**, now also requiring no bot task
   running. Anything open refuses and says plainly that the bots were
   stopped. Nothing is flattened, and nothing but the bots' own entry orders
   is cancelled.
5. **Stop the containers** that write a bundled volume or folder, so the copy
   is of quiescent data — clerks first, Postgres last — reporting each stop
   as it happens.
6. **Copy** each volume (``podman volume export``) and folder, hash it, and
   read the identity facts (registry, markers, generations, Postgres
   version) from the copy itself; refuse a copy in which any bot's durable
   desired state is not STOPPED or Postgres did not shut down cleanly — then
   write the bundle atomically.

``--check`` runs the directory read and step 2 only: it stops nothing and
writes nothing, and a running bot is reported (export would stop it) rather
than refused. Export never drains a lane, never changes an assignment, and
does not lock the old machine: the operator shuts it down by hand (#2151
owner decision 7).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.broker.fleet.records import LANE_QUIET_CONDITIONS
from app.installation_migration.bundle import (
    MANIFEST_KIND,
    MANIFEST_SCHEMA_VERSION,
    FolderEntry,
    LaneEntry,
    Manifest,
    SkippedSecretFile,
    VolumeEntry,
    write_bundle,
)
from app.installation_migration.contents import (
    BUNDLED_FOLDERS,
    BUNDLED_VOLUMES,
    BundledVolume,
    resolve_folder_path,
    skipped_secret_note,
)
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.facts import (
    bots_not_stopped,
    read_postgres_facts,
    staged_identity_facts,
)
from app.installation_migration.git import GitPort
from app.installation_migration.lanes import FleetLanes, Lane
from app.installation_migration.podman import PodmanPort, VolumeInfo
from app.installation_migration.topology import (
    containers_writing_folders,
    deployment_namespace,
    host_topology_facts,
    load_topology,
)
from app.installation_migration.tree import (
    build_folder_tar,
    require_bundleable,
    sha256_file,
    tree_digest_from_tar,
)
from app.schemas.lane_quiesce import LaneAccountQuietRead
from app.utils.timestamps import now_ms_utc

Emit = Callable[[Mapping[str, object]], None]

#: The account half of lane quiet: what "flat" means for migration.
_ACCOUNT_CONDITIONS = tuple(
    name for name, _ in LANE_QUIET_CONDITIONS if name != "runner_idle"
)
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
    allow_dirty_tree: bool = False


@dataclass(frozen=True, slots=True)
class _LaneQuiet:
    """One lane's account-quiet answer, with the lane it answers for."""

    lane: Lane
    answer: LaneAccountQuietRead


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
    quiet = _read_quiet(lanes, live)
    _refuse_unless_flat(quiet, require_runner_idle=False, bots_stopped_on=())
    emit(
        {
            "step": "check",
            "flat": True,
            "accounts": [entry.answer.account_id for entry in quiet],
            "bots_running_on": [
                entry.lane.clerk_id for entry in quiet if not entry.answer.runner_idle
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
    volume_infos, folder_paths, source_tree_dirty, skipped = _preflight(
        request, bundle, podman, git
    )
    # Read before any bot is stopped, so a checkout that cannot describe its
    # own host refuses while nothing has changed.
    topology = load_topology(request.repo_root)
    namespace = deployment_namespace(request.repo_root)
    skipped_secrets = tuple(
        SkippedSecretFile(path=path, note=skipped_secret_note(PurePosixPath(path).name))
        for path in skipped
    )
    emit(
        {
            "step": "preflight",
            "volumes": sorted(volume_infos),
            "folders": sorted(folder_paths),
            "source_tree_dirty": source_tree_dirty,
            "skipped_secret_files": [entry.model_dump() for entry in skipped_secrets],
        }
    )

    live = _live_lanes(lanes)
    emit({"step": "lanes", "lanes": [lane.clerk_id for lane in live]})

    # Flat first, with every bot still running: nothing is stopped for an
    # account that cannot move.
    _refuse_unless_flat(
        _read_quiet(lanes, live), require_runner_idle=False, bots_stopped_on=()
    )
    emit({"step": "accounts-flat", "lanes": [lane.clerk_id for lane in live]})

    receipts = _stop_every_bot(lanes, live, request, emit)

    quiet = _read_quiet(lanes, live)
    _refuse_unless_flat(
        quiet, require_runner_idle=True, bots_stopped_on=[lane.clerk_id for lane in live]
    )
    emit({"step": "accounts-quiet", "accounts": [entry.answer.account_id for entry in quiet]})

    stopped = _quiesce_containers(podman, topology, emit)
    emit({"step": "containers-stopped", "containers": stopped})

    source_commit = git.head_commit()
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
        identity = staged_identity_facts(
            {volume.name: staging / volume.member for volume in BUNDLED_VOLUMES}, staging
        )
        _refuse_unless_bots_stopped_in_copy(identity.lane_roots)
        database = next(volume for volume in BUNDLED_VOLUMES if volume.role == "postgres")
        postgres = read_postgres_facts(staging / database.member, volume=database.name)
        manifest = Manifest(
            kind=MANIFEST_KIND,
            manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            created_at_ms=clock(),
            source_commit=source_commit,
            source_tree_dirty=source_tree_dirty,
            dirty_tree_override=request.allow_dirty_tree,
            operator=request.operator,
            change_ref=request.change_ref,
            volumes=tuple(volume_entries),
            folders=tuple(folder_entries),
            registry=identity.registry,
            clerk_volumes=identity.clerk_volumes,
            lanes=tuple(
                LaneEntry(
                    clerk_id=entry.lane.clerk_id,
                    broker=entry.lane.broker,
                    account_id=entry.answer.account_id,
                    quiet_observed_at_ms=entry.answer.observed_at_ms,
                    stop_receipt_id=receipts[entry.lane.clerk_id],
                )
                for entry in quiet
            ),
            postgres=postgres,
            skipped_secret_files=skipped_secrets,
            source_host=host_topology_facts(identity.registry, topology, namespace=namespace),
        )
        members = [volume.member for volume in BUNDLED_VOLUMES] + [
            folder.member for folder in BUNDLED_FOLDERS
        ]
        write_bundle(bundle, manifest, [(member, staging / member) for member in members])
    emit(
        {
            "step": "complete",
            "bundle": str(bundle),
            "source_commit": source_commit,
            "source_tree_dirty": source_tree_dirty,
            "skipped_secret_files": [entry.model_dump() for entry in skipped_secrets],
            "stack": "stopped",
            "next": "Copy the bundle and deploy/fleet/env/*.env (plus the repo-root "
            ".env and PythonDataService/.env) to the new machine by hand, then shut this "
            "machine down. Never restart this stack: going back is a reverse migration. "
            "Host processes outside the stack that write a bundled folder (the LEAN "
            "launcher) were not stopped by this tool; anything they wrote after the copy "
            "is not in the bundle.",
        }
    )


def _preflight(
    request: ExportRequest, bundle: Path, podman: PodmanPort, git: GitPort
) -> tuple[dict[str, VolumeInfo], dict[str, Path], bool, list[str]]:
    if bundle.exists():
        raise MigrationRefused(
            "bundle_exists",
            f"{bundle} already exists; a bundle is never overwritten.",
            details={"bundle": str(bundle)},
        )
    source_tree_dirty = git.tree_dirty()
    if source_tree_dirty and not request.allow_dirty_tree:
        raise MigrationRefused(
            "source_tree_dirty",
            "The checkout has uncommitted changes to tracked files, so the bundle's "
            "source commit would not describe the code that wrote this data. Commit or "
            "stash them, or pass --allow-dirty-tree (recorded in the manifest).",
            details={"repo_root": str(request.repo_root)},
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
    skipped = require_bundleable(paths)
    return (
        {name: info for name, info in infos.items() if info is not None},
        paths,
        source_tree_dirty,
        skipped,
    )


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


def _read_quiet(lanes: FleetLanes, live: Sequence[Lane]) -> list[_LaneQuiet]:
    return [_LaneQuiet(lane=lane, answer=lanes.account_quiet(lane)) for lane in live]


def _stop_every_bot(
    lanes: FleetLanes, live: Sequence[Lane], request: ExportRequest, emit: Emit
) -> dict[str, str]:
    """Stop every bot lane by lane; a refusal names the lanes already stopped."""
    receipts: dict[str, str] = {}
    for lane in live:
        try:
            receipt = lanes.stop_all_bots(
                lane, operator=request.operator, change_ref=request.change_ref
            )
        except MigrationRefused as exc:
            stopped_on = sorted(receipts)
            already = (
                f" Bots on lane(s) {', '.join(stopped_on)} were already stopped and stay "
                "stopped."
                if stopped_on
                else " No other lane's bots were stopped."
            )
            raise MigrationRefused(
                exc.reason,
                f"{exc.message}{already} Nothing was written.",
                details={**exc.details, "bots_stopped_on": stopped_on},
            ) from exc
        receipts[lane.clerk_id] = receipt.receipt_id
        emit(
            {
                "step": "bots-stopped",
                "clerk_id": lane.clerk_id,
                "receipt_id": receipt.receipt_id,
                "stopped": [bot.model_dump() for bot in receipt.stopped],
                "intent_stopped": [bot.model_dump() for bot in receipt.intent_stopped],
            }
        )
    return receipts


def _refuse_unless_flat(
    quiet: Sequence[_LaneQuiet], *, require_runner_idle: bool, bots_stopped_on: Sequence[str]
) -> None:
    conditions = (("runner_idle",) if require_runner_idle else ()) + _ACCOUNT_CONDITIONS
    open_work = [
        {
            "clerk_id": entry.lane.clerk_id,
            "account_id": entry.answer.account_id,
            "open": [
                _CONDITION_PHRASES[name]
                for name in conditions
                if not getattr(entry.answer, name)
            ],
        }
        for entry in quiet
    ]
    open_work = [entry for entry in open_work if entry["open"]]
    if not open_work:
        return
    listing = "; ".join(
        f"account {entry['account_id']} (lane {entry['clerk_id']}): {', '.join(entry['open'])}"
        for entry in open_work
    )
    outcome = (
        f" The bots on lane(s) {', '.join(bots_stopped_on)} were stopped and stay stopped; "
        "stopping a bot cancels that bot's own working entry orders, and nothing else was "
        "cancelled. Nothing was flattened and nothing was written."
        if bots_stopped_on
        else " Nothing was stopped, flattened, cancelled or written."
    )
    raise MigrationRefused(
        "accounts_not_flat",
        f"Migration moves only flat accounts, and these are not: {listing}.{outcome} "
        "Close the open work at the broker, then retry.",
        details={"accounts": open_work, "bots_stopped_on": list(bots_stopped_on)},
    )


def _refuse_unless_bots_stopped_in_copy(lane_roots: Mapping[str, Path]) -> None:
    running = bots_not_stopped(lane_roots)
    if running:
        listing = ", ".join(
            f"{bot['strategy_instance_id']} ({bot['desired_state']}, volume {bot['volume']})"
            for bot in running
        )
        raise MigrationRefused(
            "bots_not_stopped_in_copy",
            f"The copied lane volumes hold bot(s) whose durable desired state is not "
            f"STOPPED: {listing}. They would start on the new host. Stop them, then retry; "
            "nothing was written.",
            details={"bots": running},
        )


def _quiesce_containers(
    podman: PodmanPort, topology: Mapping[str, Any], emit: Emit
) -> list[str]:
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
        if not podman.container_running(name):
            continue
        try:
            podman.stop_container(name)
        except MigrationRefused as exc:
            raise MigrationRefused(
                "container_stop_failed",
                f"Container {name} did not stop ({exc.message}). Already stopped: "
                f"{', '.join(stopped) or 'none'}; they stay stopped. Every bot was stopped "
                "and stays stopped; nothing was written.",
                details={"container": name, "already_stopped": list(stopped), "cause": exc.to_json()},
            ) from exc
        stopped.append(name)
        emit({"step": "container-stopped", "container": name})
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
            "volume or folder; a copy of live data is not a migration. Already stopped: "
            f"{', '.join(stopped) or 'none'}.",
            details={"containers": still_running, "already_stopped": list(stopped)},
        )
    return stopped


def _export_volume(
    podman: PodmanPort, volume: BundledVolume, info: VolumeInfo, staging: Path, emit: Emit
) -> VolumeEntry:
    target = staging / volume.member
    target.parent.mkdir(parents=True, exist_ok=True)
    podman.export_volume(volume.name, target)
    entry = VolumeEntry(
        name=volume.name,
        compose_key=volume.compose_key,
        role=volume.role,
        driver=info.driver,
        labels=dict(info.labels),
        member=volume.member,
        size_bytes=target.stat().st_size,
        sha256=sha256_file(target),
        content_digest=tree_digest_from_tar(target),
    )
    emit({"step": "volume-exported", "volume": volume.name, "size_bytes": entry.size_bytes})
    return entry


def _export_folder(key: str, member: str, path: Path, staging: Path, emit: Emit) -> FolderEntry:
    target = staging / member
    target.parent.mkdir(parents=True, exist_ok=True)
    build_folder_tar(path, target)
    entry = FolderEntry(
        key=key,
        member=member,
        size_bytes=target.stat().st_size,
        sha256=sha256_file(target),
        content_digest=tree_digest_from_tar(target),
    )
    emit({"step": "folder-exported", "folder": key, "size_bytes": entry.size_bytes})
    return entry


__all__ = ["ExportRequest", "run_export"]
