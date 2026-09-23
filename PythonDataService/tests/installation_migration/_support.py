"""A scratch installation and pure fakes for migration tests (#2268).

Nothing here touches podman, a running container, or a live lane: volumes
are directories under the test's tmp path, ``FakePodman`` exports and imports
them as tars exactly as ``podman volume export/import`` would, and
``FakeLanes`` answers the coordinator's two lane operations from a script.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore, registry_database_path
from app.installation_migration.contents import BUNDLED_FOLDERS, BUNDLED_VOLUMES
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.lanes import Lane
from app.installation_migration.podman import ContainerUse, VolumeInfo
from app.schemas.lane_quiesce import LaneAccountQuietRead, LaneStopAllBotsReceipt
from tests.broker.fleet.conftest import FrozenClock, bind_lane, fake_alpha

REPO = Path(__file__).resolve().parents[3]
SOURCE_COMMIT = "a" * 40
T0 = 1_789_100_000_000
LIVE_VOLUME = "learn-ai-alpaca-clerk-data"
PAPER_VOLUME = "learn-ai-alpaca-paper-clerk-data"
CONTROL_VOLUME = "learn-ai_alpaca-fleet-control"
PG_VOLUME = "learn-ai_pgdata"
QUALIFICATION_VOLUME = "learn-ai-alpaca-clerk-qualification-data"
LIVE_ACCOUNT = "PA-LIVE-1"
PAPER_ACCOUNT = "PA-PAPER-1"


class FakePodman:
    """Named volumes as directories; containers as a name → state table."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.volumes: dict[str, VolumeInfo] = {}
        self.containers: dict[str, tuple[bool, frozenset[str]]] = {}
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.fail_stop: set[str] = set()
        self.fail_import: set[str] = set()
        self.truncate_export: set[str] = set()

    def volume_dir(self, name: str) -> Path:
        return self.root / name

    def add_volume(self, name: str, *, labels: dict[str, str] | None = None) -> Path:
        self.volumes[name] = VolumeInfo(name=name, driver="local", labels=labels or {})
        path = self.volume_dir(name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add_container(self, name: str, *, running: bool, volumes: Iterable[str]) -> None:
        self.containers[name] = (running, frozenset(volumes))

    # ---- PodmanPort ---------------------------------------------------------

    def volume_info(self, name: str) -> VolumeInfo | None:
        return self.volumes.get(name)

    def export_volume(self, name: str, destination: Path) -> None:
        # Like ``podman volume export``: the volume's children at the tar
        # root — no ``./`` root entry and no ``./`` prefix.
        with tarfile.open(destination, "x") as archive:
            for child in sorted(self.volume_dir(name).iterdir()):
                archive.add(child, arcname=child.name)
        if name in self.truncate_export:
            destination.write_bytes(destination.read_bytes()[:700])

    def create_volume(self, info: VolumeInfo) -> None:
        if info.name in self.volumes:
            raise MigrationRefused("podman_command_failed", f"volume {info.name} exists")
        self.volumes[info.name] = info
        self.volume_dir(info.name).mkdir(parents=True)

    def import_volume(self, name: str, source: Path) -> None:
        if name in self.fail_import:
            raise MigrationRefused("podman_command_failed", f"`podman volume import {name}` exited 125")
        with tarfile.open(source, "r:*") as archive:
            archive.extractall(self.volume_dir(name), filter="tar")

    def volume_size_bytes(self, name: str) -> int | None:
        return sum(
            path.stat().st_size for path in self.volume_dir(name).rglob("*") if path.is_file()
        )

    def remove_volume(self, name: str) -> None:
        if self.containers_using_volume(name):
            raise MigrationRefused("podman_command_failed", f"volume {name} is being used")
        shutil.rmtree(self.volume_dir(name))
        del self.volumes[name]
        self.removed.append(name)

    def containers_using_volume(self, name: str) -> list[ContainerUse]:
        return [
            ContainerUse(name=container, running=running)
            for container, (running, volumes) in sorted(self.containers.items())
            if name in volumes
        ]

    def container_running(self, name: str) -> bool | None:
        state = self.containers.get(name)
        return None if state is None else state[0]

    def stop_container(self, name: str) -> None:
        if name in self.fail_stop:
            raise MigrationRefused("podman_command_failed", f"`podman stop {name}` exited 125")
        _running, volumes = self.containers[name]
        self.containers[name] = (False, volumes)
        self.stopped.append(name)


@dataclass
class FakeLanes:
    """The coordinator's lane surface, answered from a script.

    ``calls`` records every read and stop in order; ``after_stop`` holds, per
    lane, the quiet fields a lane reports once its bots were stopped (an
    account that goes non-flat between the first read and the re-check).
    """

    lanes: list[Lane]
    quiet: dict[str, dict[str, Any]]
    receipt_roots: dict[str, Path] = field(default_factory=dict)
    stopped: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    after_stop: dict[str, dict[str, Any]] = field(default_factory=dict)

    def list_lanes(self) -> list[Lane]:
        return list(self.lanes)

    def stop_all_bots(
        self, lane: Lane, *, operator: str, change_ref: str
    ) -> LaneStopAllBotsReceipt:
        self.calls.append(("stop", lane.clerk_id))
        self.stopped.append(lane.clerk_id)
        self.quiet[lane.clerk_id] = {
            **self.quiet[lane.clerk_id],
            "runner_idle": True,
            **self.after_stop.get(lane.clerk_id, {}),
        }
        receipt = LaneStopAllBotsReceipt(
            receipt_id=f"rcpt-{lane.clerk_id}",
            requested_at_ms=T0,
            completed_at_ms=T0 + 1,
            operator=operator,
            change_ref=change_ref,
            reason="lane_stop_all",
            stopped=[],
            intent_stopped=[],
            refused=[],
            still_running=False,
            all_stopped=True,
        )
        root = self.receipt_roots.get(lane.clerk_id)
        if root is not None:
            directory = root / "lane_stop_all_receipts"
            directory.mkdir(exist_ok=True)
            (directory / f"{T0}-{receipt.receipt_id}.json").write_text("{}", encoding="utf-8")
        return receipt

    def account_quiet(self, lane: Lane) -> LaneAccountQuietRead:
        self.calls.append(("quiet", lane.clerk_id))
        answer = self.quiet[lane.clerk_id]
        outstanding = [
            name
            for name in ("runner_idle", "broker_work_ended", "account_flat", "intents_resolved")
            if not answer[name]
        ]
        return LaneAccountQuietRead(**answer, quiet=not outstanding, outstanding=outstanding)


@dataclass
class FakeGit:
    head: str = SOURCE_COMMIT
    dirty: bool = False
    known: set[str] = field(default_factory=lambda: {SOURCE_COMMIT})
    descendants: dict[str, set[str]] = field(default_factory=dict)

    def head_commit(self) -> str:
        return self.head

    def tree_dirty(self) -> bool:
        return self.dirty

    def commit_exists(self, commit: str) -> bool:
        return commit in self.known

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor == descendant or descendant in self.descendants.get(ancestor, set())


def quiet_answer(clerk_id: str, account_id: str, **unsatisfied: bool) -> dict[str, Any]:
    answer = {
        "account_id": account_id,
        "observed_at_ms": T0,
        "runner_idle": True,
        "broker_work_ended": True,
        "account_flat": True,
        "intents_resolved": True,
    }
    answer.update(unsatisfied)
    return answer


@dataclass
class Installation:
    repo_root: Path
    podman: FakePodman
    lanes: FakeLanes
    live_clerk_id: str
    paper_clerk_id: str


def make_repo(root: Path) -> Path:
    """A checkout skeleton carrying the committed topology the tool reads."""
    root.mkdir(parents=True)
    for relative in ("deploy/fleet/topology.snapshot.json", "compose.fleet.dev.yaml"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, root / relative)
    return root


#: The namespace the committed dev overlay registers clerks under.
DEV_NAMESPACE = "compose:learn-ai"


def _dev_topology_volume_roots() -> dict[str, str]:
    """Each clerk volume's mount path in the committed dev topology."""
    snapshot = json.loads(
        (REPO / "deploy" / "fleet" / "topology.snapshot.json").read_text(encoding="utf-8")
    )
    names = {key: spec.get("name") or key for key, spec in snapshot["volumes"].items()}
    roots: dict[str, str] = {}
    for service in snapshot["service_detail"].values():
        for mount in service.get("volumes") or []:
            source, _, rest = str(mount).partition("->")
            target, _, kind = rest.rpartition(":")
            if kind == "volume" and source in names:
                roots[names[source]] = target
    return roots


def _as_mounted_in_the_dev_topology(control_dir: Path) -> None:
    """Record the Live scratch clerk where the dev topology mounts its volume.

    ``provision_clerk`` proves a real on-disk root, which a test can only give
    it under its tmp path; a real installation's registry records the path
    the lane sees inside its container (``/app/artifacts/alpaca_clerk``) under
    the overlay's namespace. The identity trigger forbids that edit, so it is
    lifted for this one UPDATE and restored from its own stored SQL.

    Only the Live lane can be re-homed: the dev topology mounts *both* lane
    volumes at ``/app/artifacts/alpaca_clerk`` under one namespace, and the
    registry's unique ``(deployment_namespace, volume_root)`` index admits
    one active clerk there. The Paper lane keeps its tmp root, so a round
    trip reports exactly it for re-approval.
    """
    roots = _dev_topology_volume_roots()
    connection = sqlite3.connect(registry_database_path(control_dir))
    try:
        with connection:
            (trigger_sql,) = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'trg_clerks_identity_immutable'"
            ).fetchone()
            connection.execute("DROP TRIGGER trg_clerks_identity_immutable")
            connection.execute(
                "UPDATE clerks SET deployment_namespace = ?", (DEV_NAMESPACE,)
            )
            connection.execute(
                "UPDATE clerks SET volume_root = ? WHERE volume_attestation_id = ?",
                (roots[LIVE_VOLUME], LIVE_VOLUME),
            )
            connection.execute(trigger_sql)
    finally:
        connection.close()


def write_fleet_env_files(repo_root: Path) -> None:
    env = repo_root / "deploy" / "fleet" / "env"
    env.mkdir(parents=True, exist_ok=True)
    for name in ("coordinator.env", "live.env", "paper.env"):
        (env / name).write_text("FLEET_WORKER_KEY=secret\n", encoding="utf-8")


def write_host_env_files(repo_root: Path) -> None:
    """The repo-root and data-plane ``.env`` files the operator copies by hand."""
    (repo_root / ".env").write_text("POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    (repo_root / "PythonDataService").mkdir(parents=True, exist_ok=True)
    (repo_root / "PythonDataService" / ".env").write_text("POLYGON_API_KEY=secret\n", encoding="utf-8")


def build_installation(tmp_path: Path, *, name: str = "source") -> Installation:
    """A complete, flat scratch installation: five volumes, three folders."""
    repo_root = make_repo(tmp_path / name / "learn-ai")
    podman = FakePodman(tmp_path / name / "podman")
    for volume in BUNDLED_VOLUMES:
        podman.add_volume(volume.name, labels={"io.podman.compose.project": "learn-ai"})

    pg = podman.volume_dir(PG_VOLUME)
    (pg / "base" / "5").mkdir(parents=True)
    (pg / "PG_VERSION").write_text("16\n", encoding="utf-8")
    (pg / "base" / "5" / "16384").write_bytes(os.urandom(4096))
    (podman.volume_dir(QUALIFICATION_VOLUME) / "evidence").mkdir()
    (podman.volume_dir(QUALIFICATION_VOLUME) / "evidence" / "rehearsal.json").write_text(
        "{}", encoding="utf-8"
    )

    clock = FrozenClock(T0)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=podman.volume_dir(CONTROL_VOLUME)),
        provider_adapters={"fake_alpha": fake_alpha()},
        clock=clock,
    )
    try:
        clerk_ids: dict[str, str] = {}
        for volume, account, label in (
            (LIVE_VOLUME, LIVE_ACCOUNT, "live"),
            (PAPER_VOLUME, PAPER_ACCOUNT, "paper"),
        ):
            provisioned = service.provision_clerk(
                broker="fake_alpha",
                display_label=label,
                volume_root=podman.volume_dir(volume),
                attestation_id=volume,
            )
            clerk_ids[label] = provisioned.clerk.clerk_id
            service.approve_endpoint(
                clerk_id=provisioned.clerk.clerk_id,
                endpoint_ref=f"alpaca-{label}-agent",
                base_url=f"http://alpaca-{label}-clerk:8000",
            )

            @dataclass
            class _Lane:
                clerk_id: str
                broker: str
                worker_key: str

            bind_lane(
                service,
                _Lane(provisioned.clerk.clerk_id, "fake_alpha", provisioned.clerk.worker_key),
                account=account,
            )
            repository = ClerkSqliteRepository.initialize(
                account_id=account, artifacts_root=podman.volume_dir(volume), clock=clock
            )
            repository.close()
    finally:
        service.close()
    _as_mounted_in_the_dev_topology(podman.volume_dir(CONTROL_VOLUME))

    for folder in BUNDLED_FOLDERS:
        (repo_root / folder.key).mkdir(parents=True, exist_ok=True)
    (repo_root / "data-lake-volume" / "lake" / "bars").mkdir(parents=True)
    (repo_root / "data-lake-volume" / "lake" / "bars" / "SPY.parquet").write_bytes(os.urandom(2048))
    (repo_root / "PythonDataService" / "artifacts" / "live_runs").mkdir()
    (repo_root / "PythonDataService" / "artifacts" / "live_runs" / "run.json").write_text(
        '{"run": 1}', encoding="utf-8"
    )
    (repo_root / "PythonDataService" / "lean-cache" / ".gitkeep").write_text("", encoding="utf-8")

    podman.add_container("my-postgres", running=True, volumes=[PG_VOLUME])
    podman.add_container("polygon-data-service", running=True, volumes=[CONTROL_VOLUME])
    podman.add_container("alpaca-live-clerk", running=True, volumes=[LIVE_VOLUME])
    podman.add_container("alpaca-paper-clerk", running=True, volumes=[PAPER_VOLUME])

    lanes = FakeLanes(
        lanes=[
            Lane(clerk_ids["live"], "alpaca", "provisioned", "Live"),
            Lane(clerk_ids["paper"], "alpaca", "provisioned", "Paper"),
        ],
        quiet={
            clerk_ids["live"]: quiet_answer(clerk_ids["live"], LIVE_ACCOUNT, runner_idle=False),
            clerk_ids["paper"]: quiet_answer(clerk_ids["paper"], PAPER_ACCOUNT),
        },
        receipt_roots={
            clerk_ids["live"]: podman.volume_dir(LIVE_VOLUME),
            clerk_ids["paper"]: podman.volume_dir(PAPER_VOLUME),
        },
    )
    return Installation(
        repo_root=repo_root,
        podman=podman,
        lanes=lanes,
        live_clerk_id=clerk_ids["live"],
        paper_clerk_id=clerk_ids["paper"],
    )


def build_empty_destination(tmp_path: Path, *, name: str = "destination") -> tuple[Path, FakePodman]:
    """A fresh host: a checkout with its env files, and no volumes at all."""
    repo_root = make_repo(tmp_path / name / "learn-ai")
    write_fleet_env_files(repo_root)
    write_host_env_files(repo_root)
    return repo_root, FakePodman(tmp_path / name / "podman")
