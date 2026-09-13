"""Focused checks for the real-Compose fleet qualification harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_broker_fleet_compose_qualification as qualification


def test_compose_topology_declares_distinct_role_env_files_and_lane_volumes() -> None:
    """The production topology cannot quietly collapse into combined mode."""
    compose = (qualification.REPOSITORY_ROOT / "compose.fleet.yaml").read_text(encoding="utf-8")

    assert "FLEET_ROLE: fleet_coordinator" in compose
    assert compose.count("FLEET_ROLE: clerk_agent") == 2
    assert "deploy/fleet/env/coordinator.env" in compose
    assert "deploy/fleet/env/paper.env" in compose
    assert "deploy/fleet/env/live.env" in compose
    assert "fleet-coordinator-control:/app/artifacts/fleet" in compose
    assert compose.count(":/app/artifacts/alpaca_clerk") == 2
    assert "fleet-alpaca-paper-data:/app/artifacts/alpaca_clerk" in compose
    assert "fleet-alpaca-live-data:/app/artifacts/alpaca_clerk" in compose
    assert "internal: true" in compose
    assert "/var/run" not in compose


def test_compose_topology_has_lane_budgets_and_live_mutation_stays_disabled() -> None:
    """Resource bounds and queue budgets are deployment-visible, not folklore."""
    compose = (qualification.REPOSITORY_ROOT / "compose.fleet.yaml").read_text(encoding="utf-8")

    for variable in (
        "FLEET_MAX_INFLIGHT_REQUESTS",
        "FLEET_MAX_INFLIGHT_STREAMS",
        "FLEET_REQUEST_QUEUE_LIMIT",
        "FLEET_REQUEST_QUEUE_TIMEOUT_MS",
    ):
        assert variable in compose
    assert compose.count("FLEET_ALLOW_LIVE_MUTATIONS: \"false\"") == 2
    assert "IBKR_CLIENT_ID: ${FLEET_PAPER_IBKR_CLIENT_ID:-1201}" in compose
    assert "IBKR_CLIENT_ID: ${FLEET_LIVE_IBKR_CLIENT_ID:-1202}" in compose
    assert "ALPACA_MARKET_STATUS_UPSTREAM_URL" not in compose


def test_assert_no_custody_root_refuses_custody_named_artifact(tmp_path: Path) -> None:
    """The coordinator assertion rejects custody-like state by filename."""
    (tmp_path / "fleet-registry.sqlite").touch()
    result = qualification._assert_no_custody_root(tmp_path)
    assert result["ok"] is True
    assert "fleet-registry.sqlite" in result["entries"]
    (tmp_path / "custody.sqlite").touch()

    with pytest.raises(qualification.QualificationError, match="custody"):
        qualification._assert_no_custody_root(tmp_path)


def test_mount_and_resource_assertions_require_real_engine_evidence() -> None:
    """A Compose YAML declaration is not accepted in place of engine proof."""
    inspect = {
        "Mounts": [{"Destination": "/app/artifacts/alpaca_clerk", "Source": "/volumes/paper", "RW": True}],
        "HostConfig": {"NanoCpus": 1_000_000_000, "Memory": 805_306_368, "PidsLimit": 256},
    }

    assert qualification._mount_at(inspect, "/app/artifacts/alpaca_clerk")["Source"] == "/volumes/paper"
    assert qualification._assert_limits(inspect, "paper") == {
        "NanoCpus": 1_000_000_000,
        "Memory": 805_306_368,
        "PidsLimit": 256,
    }
    with pytest.raises(qualification.QualificationError, match="expected mount"):
        qualification._mount_at(inspect, "/wrong")


def test_live_probe_mutation_refusal_is_a_typed_non_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Host evidence preserves the fake lane's refusal instead of treating it as a crash."""
    root = tmp_path / "live"
    root.mkdir()
    result = qualification.main(["--assert-no-custody-root", str(root)])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"entries": [], "ok": True}
