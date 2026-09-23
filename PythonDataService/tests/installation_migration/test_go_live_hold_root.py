"""Every bot-hosting process reads its go-live hold where import lays it (#2269).

Import lays each clerk volume's marker at the volume's root. A process that
hosts a bot runner reads its hold through ``lane_go_live_hold`` — the gate
``app.main`` wires into ``BotTaskRegistry`` — at ``lane_go_live_hold_root``,
the clerk directory. These tests pin the chain end to end against the
committed Compose files: in every role that runs bots, including plain
``podman compose up -d`` (the ``combined`` role, whose bot runner keeps its
artifacts root on the shared ``/app/artifacts`` bind, outside the volume),
the clerk directory is exactly where a clerk volume is mounted — and a
process that cannot name that directory refuses starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import app as app_package
from app.broker_configuration import runtime
from app.broker_configuration.runtime import CLERK_DIR_ENV_VAR
from app.installation_migration.contents import BUNDLED_VOLUMES
from app.installation_migration.export import ExportRequest, run_export
from app.installation_migration.importer import ImportRequest, run_import
from app.services.bot_runner import (
    BotRunnerError,
    BotTaskRegistry,
    RunAdmissionRefusedError,
    go_live_start_gate,
)
from app.services.bot_runner_errors import LANE_GO_LIVE_HOLD_UNREADABLE, LANE_GO_LIVE_PENDING
from app.services.go_live_hold import GO_LIVE_HOLD_MARKER, GoLiveHoldMarker, go_live_marker_bytes
from app.services.lane_go_live import lane_go_live_hold, lane_go_live_hold_root
from tests._helpers.bot_runner.custody import _SID
from tests.installation_migration._support import (
    LIVE_VOLUME,
    PAPER_VOLUME,
    REPO,
    FakeGit,
    build_empty_destination,
    build_installation,
)

#: Where the service root (``PythonDataService/``) sits inside the image.
_CONTAINER_SERVICE_ROOT = Path("/app")
_SERVICE_ROOT = Path(app_package.__file__).resolve().parents[1]
_BOT_ROLES = frozenset({"combined", "clerk_agent"})
_CLERK_VOLUME_KEYS = frozenset(volume.compose_key for volume in BUNDLED_VOLUMES if volume.role == "clerk")

#: Per Compose file, the services that host a bot runner — pinned, so a
#: selection that silently matched nothing cannot pass.
_BOT_HOSTS = {
    "compose.yaml": {"python-service"},
    "compose.fleet.dev.yaml": {"alpaca-live-clerk", "alpaca-paper-clerk"},
    "compose.fleet.yaml": {"alpaca-live-clerk", "alpaca-paper-clerk"},
}
#: The files a migration bundles volumes for (``topology.FLEET_OVERLAY`` and
#: the base file); ``compose.fleet.yaml`` names its own, unbundled volumes.
_MIGRATED = frozenset({"compose.yaml", "compose.fleet.dev.yaml"})

_MARKER = GoLiveHoldMarker(
    kind="learn-ai-go-live-hold",
    schema_version=1,
    written_at_ms=1_789_100_000_000,
    volume=LIVE_VOLUME,
    source_commit="a" * 40,
    registry_id="reg_1",
)


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's ``!override`` / ``!reset`` tags as the plain node."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _environment(service: dict[str, Any]) -> dict[str, str]:
    declared = service.get("environment") or {}
    if isinstance(declared, list):
        return dict(str(entry).split("=", 1) for entry in declared)
    return {str(key): str(value) for key, value in declared.items()}


def _bot_hosts(compose_file: str) -> dict[str, dict[str, Any]]:
    services = yaml.load((REPO / compose_file).read_text(encoding="utf-8"), Loader=_ComposeLoader)[
        "services"
    ]
    hosts: dict[str, dict[str, Any]] = {}
    for name, service in services.items():
        role = _environment(service).get("FLEET_ROLE")
        runs_app = "app.main:app" in str(service.get("command") or "")
        if role in _BOT_ROLES or (role is None and runs_app):
            hosts[name] = service
    return hosts


def _named_volume_mounts(service: dict[str, Any]) -> dict[str, str]:
    """Container target → named volume key, for each named-volume mount."""
    mounts: dict[str, str] = {}
    for mount in service.get("volumes") or []:
        source, target, *_options = str(mount).split(":")
        if not source.startswith((".", "/", "$", "~")):
            mounts[target] = source
    return mounts


def _in_container(root: Path) -> Path:
    """The container path of a clerk directory the host-side test resolved."""
    if not root.is_relative_to(_SERVICE_ROOT):
        return root
    return _CONTAINER_SERVICE_ROOT / root.relative_to(_SERVICE_ROOT)


@pytest.mark.parametrize("compose_file", sorted(_BOT_HOSTS))
def test_every_bot_host_reads_its_hold_at_a_clerk_volume_root(
    compose_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = _bot_hosts(compose_file)
    assert set(hosts) == _BOT_HOSTS[compose_file]

    for name, service in hosts.items():
        clerk_dir = _environment(service).get(CLERK_DIR_ENV_VAR)
        if clerk_dir is None:
            monkeypatch.delenv(CLERK_DIR_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(CLERK_DIR_ENV_VAR, clerk_dir)

        hold_root = _in_container(lane_go_live_hold_root())

        volume_key = _named_volume_mounts(service).get(str(hold_root))
        assert volume_key is not None, (
            f"{compose_file}:{name} reads its go-live hold at {hold_root}, where no named "
            "volume is mounted, so import's marker can never be seen there"
        )
        if compose_file in _MIGRATED:
            assert volume_key in _CLERK_VOLUME_KEYS, (compose_file, name, volume_key)


def test_the_combined_roles_artifacts_root_is_not_its_volume_so_the_hold_cannot_live_there() -> None:
    """Why the hold is not read at the runner's artifacts root: in the
    combined role that root is the shared ``/app/artifacts`` bind."""
    [service] = _bot_hosts("compose.yaml").values()
    live_runs = Path(_environment(service)["IBKR_LIVE_RUNS_ROOT"])

    assert str(live_runs.parent) not in _named_volume_mounts(service)


async def test_a_combined_lane_holds_though_its_artifacts_root_is_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``ALPACA_CLERK_DIR`` (plain ``compose up``): the default clerk
    directory carries the marker, the runner's artifacts root does not."""
    volume = tmp_path / "alpaca_clerk"
    volume.mkdir()
    (volume / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(_MARKER))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.delenv(CLERK_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr(runtime, "DEFAULT_CLERK_DIR", volume)
    registry = BotTaskRegistry(
        artifacts,
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        lane_start_gates=(go_live_start_gate(lane_go_live_hold),),
    )

    with pytest.raises(RunAdmissionRefusedError) as refused:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert refused.value.reason_code == LANE_GO_LIVE_PENDING


@pytest.mark.parametrize(
    "clerk_dir", ["relative/alpaca_clerk", "/nonexistent/alpaca_clerk"], ids=["relative", "absent"]
)
async def test_a_process_that_cannot_resolve_its_clerk_volume_refuses_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clerk_dir: str
) -> None:
    monkeypatch.setenv(CLERK_DIR_ENV_VAR, clerk_dir)
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        lane_start_gates=(go_live_start_gate(lane_go_live_hold),),
    )

    with pytest.raises(RunAdmissionRefusedError) as refused:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert refused.value.reason_code == LANE_GO_LIVE_HOLD_UNREADABLE


async def test_a_never_migrated_clerk_volume_is_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = tmp_path / "alpaca_clerk"
    volume.mkdir()
    monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(volume))
    registry = BotTaskRegistry(
        tmp_path / "artifacts",
        feed_resolver=lambda: None,
        boot_recovery_required=False,
        lane_start_gates=(go_live_start_gate(lane_go_live_hold),),
    )

    assert lane_go_live_hold().held is False
    # The hold does not answer; a later gate does.
    with pytest.raises(BotRunnerError) as later:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert later.value.reason_code not in (LANE_GO_LIVE_PENDING, LANE_GO_LIVE_HOLD_UNREADABLE)
    assert "go-live" not in str(later.value)


def test_the_production_gate_sees_the_marker_import_lays_on_each_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import, then read each restored volume exactly as a lane mounted on it
    would: through ``lane_go_live_hold`` with the clerk directory at the
    volume's root."""
    source = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    run_export(
        ExportRequest(
            repo_root=source.repo_root, bundle_path=bundle, operator="inkant", change_ref="m"
        ),
        lanes=source.lanes,
        podman=source.podman,
        git=FakeGit(),
        emit=lambda _step: None,
    )
    repo_root, podman = build_empty_destination(tmp_path)
    run_import(
        ImportRequest(repo_root=repo_root, bundle_path=bundle),
        podman=podman,
        git=FakeGit(),
        emit=lambda _step: None,
    )

    for volume in (LIVE_VOLUME, PAPER_VOLUME):
        monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(podman.volume_dir(volume)))
        hold = lane_go_live_hold()
        assert hold.held is True
        assert hold.problem is None
        assert hold.marker is not None
        assert hold.marker.volume == volume
