"""What the committed topology tells a migration about this host (#2268)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from app.installation_migration.contents import BUNDLED_FOLDERS
from app.installation_migration.facts import RegistryClerk, RegistryEndpoint, RegistryFacts
from app.installation_migration.topology import (
    ClerkMount,
    containers_writing_folders,
    deployment_namespace,
    host_topology_facts,
    load_topology,
    reapproval_report,
    required_fleet_env_files,
)

_REPO = Path(__file__).resolve().parents[3]


def _clerk(clerk_id: str, volume: str, volume_root: str, **overrides: str) -> RegistryClerk:
    fields = {
        "clerk_id": clerk_id,
        "broker": "alpaca",
        "volume_id": f"vol_{clerk_id}",
        "volume_root": volume_root,
        "deployment_namespace": "compose:learn-ai",
        "attestation_kind": "compose_named_volume",
        "attestation_id": volume,
        "lifecycle_state": "provisioned",
    }
    fields.update(overrides)
    return RegistryClerk(**fields)


def _registry(**live_overrides: str) -> RegistryFacts:
    """The owner's dev registry as it really is (#2269): the Live clerk's
    ``volume_root`` is where the topology mounts it, the Paper clerk's is
    ``/paper-volume``, which no service mounts — a pre-existing mismatch."""
    return RegistryFacts(
        registry_id="reg_1",
        schema_version=1,
        clerks=(
            _clerk(
                "clrk_live",
                "learn-ai-alpaca-clerk-data",
                "/app/artifacts/alpaca_clerk",
                **live_overrides,
            ),
            _clerk("clrk_paper", "learn-ai-alpaca-paper-clerk-data", "/paper-volume"),
        ),
        assignments=(),
        approved_endpoints=(
            RegistryEndpoint(
                endpoint_ref="alpaca-live-agent",
                clerk_id="clrk_live",
                base_url="http://alpaca-live-clerk:8000",
            ),
            RegistryEndpoint(
                endpoint_ref="alpaca-paper-agent",
                clerk_id="clrk_paper",
                base_url="http://alpaca-paper-clerk:8000",
            ),
        ),
    )


def _facts(topology: dict, *, namespace: str = "compose:learn-ai", registry=None):
    return host_topology_facts(registry or _registry(), topology, namespace=namespace)


def _remount(topology: dict, service: str, old: str, new: str) -> dict:
    """A copy of ``topology`` with one service's mount spec rewritten."""
    changed = copy.deepcopy(topology)
    detail = changed["service_detail"][service]
    detail["volumes"] = [new if mount == old else mount for mount in detail["volumes"]]
    return changed


def test_only_the_coordinator_writes_a_bundled_host_folder() -> None:
    topology = load_topology(_REPO)

    assert containers_writing_folders(
        topology, [folder.key for folder in BUNDLED_FOLDERS]
    ) == ["polygon-data-service"]


def test_the_fleet_env_files_are_the_three_committed_lane_and_coordinator_files() -> None:
    topology = load_topology(_REPO)

    assert [path.name for path in required_fleet_env_files(_REPO, topology)] == [
        "coordinator.env",
        "live.env",
        "paper.env",
    ]


def test_the_namespace_defaults_to_the_overlay_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLEET_DEPLOYMENT_NAMESPACE", raising=False)
    (tmp_path / "compose.fleet.dev.yaml").write_text(
        (_REPO / "compose.fleet.dev.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert deployment_namespace(tmp_path) == "compose:learn-ai"

    (tmp_path / ".env").write_text(
        "export FLEET_DEPLOYMENT_NAMESPACE=compose:other  # second mac\n", encoding="utf-8"
    )
    assert deployment_namespace(tmp_path) == "compose:other"

    monkeypatch.setenv("FLEET_DEPLOYMENT_NAMESPACE", "compose:env")
    assert deployment_namespace(tmp_path) == "compose:env"


def test_the_dev_topology_records_where_each_lane_volume_is_mounted() -> None:
    facts = _facts(load_topology(_REPO))

    assert facts.deployment_namespace == "compose:learn-ai"
    assert [(clerk.clerk_id, clerk.mounts) for clerk in facts.clerks] == [
        (
            "clrk_live",
            (
                ClerkMount(
                    service="alpaca-live-clerk",
                    container="alpaca-live-clerk",
                    target="/app/artifacts/alpaca_clerk",
                ),
            ),
        ),
        (
            "clrk_paper",
            (
                ClerkMount(
                    service="alpaca-paper-clerk",
                    container="alpaca-paper-clerk",
                    target="/app/artifacts/alpaca_clerk",
                ),
            ),
        ),
    ]


def test_the_real_dev_registry_moved_to_an_identical_host_needs_no_reapproval() -> None:
    """#2269: Paper's ``/paper-volume`` never matched its mount; that is
    carried over as it was and must not demand a re-approval."""
    topology = load_topology(_REPO)

    report = reapproval_report(_registry(), _facts(topology), _facts(topology))

    assert [(entry["clerk_id"], entry["reapproval_required"]) for entry in report] == [
        ("clrk_live", False),
        ("clrk_paper", False),
    ]
    paper = report[1]
    assert paper["volume_root"] == "/paper-volume"
    assert paper["issues"] == []


def test_a_lane_volume_mounted_elsewhere_on_the_new_host_requires_reapproval() -> None:
    topology = load_topology(_REPO)
    moved = _remount(
        topology,
        "alpaca-paper-clerk",
        "alpaca-paper-clerk-data->/app/artifacts/alpaca_clerk:volume",
        "alpaca-paper-clerk-data->/srv/paper:volume",
    )

    report = reapproval_report(_registry(), _facts(topology), _facts(moved))

    assert [(entry["clerk_id"], entry["reapproval_required"]) for entry in report] == [
        ("clrk_live", False),
        ("clrk_paper", True),
    ]
    [issue] = report[1]["issues"]
    assert "/app/artifacts/alpaca_clerk" in issue
    assert "/srv/paper" in issue


def test_a_lane_volume_no_service_mounts_on_the_new_host_requires_reapproval() -> None:
    topology = load_topology(_REPO)
    unmounted = copy.deepcopy(topology)
    del unmounted["service_detail"]["alpaca-live-clerk"]

    report = reapproval_report(_registry(), _facts(topology), _facts(unmounted))

    live = report[0]
    assert live["reapproval_required"] is True
    assert "no service here" in live["issues"][0]


def test_a_different_namespace_on_the_new_host_requires_reapproval_for_every_lane() -> None:
    topology = load_topology(_REPO)

    report = reapproval_report(
        _registry(), _facts(topology), _facts(topology, namespace="compose:elsewhere")
    )

    assert all(entry["reapproval_required"] for entry in report)
    assert "compose:elsewhere" in report[0]["issues"][0]


def test_retired_clerks_are_neither_recorded_nor_reported() -> None:
    topology = load_topology(_REPO)
    registry = _registry(lifecycle_state="retired")

    facts = _facts(topology, registry=registry)
    report = reapproval_report(registry, facts, facts)

    assert [clerk.clerk_id for clerk in facts.clerks] == ["clrk_paper"]
    assert [entry["clerk_id"] for entry in report] == ["clrk_paper"]
