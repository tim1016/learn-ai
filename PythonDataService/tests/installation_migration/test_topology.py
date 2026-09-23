"""What the committed topology tells a migration about this host (#2268)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.installation_migration.contents import BUNDLED_FOLDERS
from app.installation_migration.facts import RegistryClerk, RegistryEndpoint, RegistryFacts
from app.installation_migration.topology import (
    containers_writing_folders,
    deployment_namespace,
    host_resolution_report,
    load_topology,
    required_fleet_env_files,
)

_REPO = Path(__file__).resolve().parents[3]


def _registry(**overrides: str) -> RegistryFacts:
    clerk = {
        "clerk_id": "clrk_live",
        "broker": "alpaca",
        "volume_id": "vol_live",
        "volume_root": "/app/artifacts/alpaca_clerk",
        "deployment_namespace": "compose:learn-ai",
        "attestation_kind": "compose_named_volume",
        "attestation_id": "learn-ai-alpaca-clerk-data",
        "lifecycle_state": "provisioned",
    }
    base_url = overrides.pop("base_url", "http://alpaca-live-clerk:8000")
    clerk.update(overrides)
    return RegistryFacts(
        registry_id="reg_1",
        schema_version=1,
        clerks=(RegistryClerk(**clerk),),
        assignments=(),
        approved_endpoints=(
            RegistryEndpoint(
                endpoint_ref="alpaca-live-agent", clerk_id="clrk_live", base_url=base_url
            ),
        ),
    )


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


def test_a_clerk_mounted_where_the_registry_says_resolves() -> None:
    report = host_resolution_report(
        _registry(), load_topology(_REPO), namespace="compose:learn-ai"
    )

    assert report == [
        {
            "clerk_id": "clrk_live",
            "volume_root": "/app/artifacts/alpaca_clerk",
            "endpoint_ref": "alpaca-live-agent",
            "base_url": "http://alpaca-live-clerk:8000",
            "resolves": True,
            "reapproval_required": False,
            "issues": [],
        }
    ]


def test_a_moved_volume_root_requires_reapproval_and_says_why() -> None:
    report = host_resolution_report(
        _registry(volume_root="/somewhere/else"),
        load_topology(_REPO),
        namespace="compose:learn-ai",
    )

    assert report[0]["reapproval_required"] is True
    assert any("/somewhere/else" in issue for issue in report[0]["issues"])


def test_an_endpoint_naming_another_host_requires_reapproval() -> None:
    report = host_resolution_report(
        _registry(base_url="http://old-mac.local:8000"),
        load_topology(_REPO),
        namespace="compose:learn-ai",
    )

    assert report[0]["reapproval_required"] is True
    assert any("old-mac.local" in issue for issue in report[0]["issues"])


def test_a_different_namespace_requires_reapproval() -> None:
    report = host_resolution_report(
        _registry(), load_topology(_REPO), namespace="compose:elsewhere"
    )

    assert report[0]["reapproval_required"] is True


def test_retired_clerks_are_not_reported() -> None:
    report = host_resolution_report(
        _registry(lifecycle_state="retired"), load_topology(_REPO), namespace="compose:learn-ai"
    )

    assert report == []
