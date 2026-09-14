"""Focused checks for the real-Compose fleet qualification harness."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import cast

import pytest

from app.broker.fleet import schema
from scripts import run_broker_fleet_compose_qualification as qualification


def test_compose_environment_overlay_preserves_base_interpolation_values() -> None:
    """Adding temporary lane files must not discard required base Compose values."""
    base = qualification.ComposeCommand(
        engine="podman",
        compose=("podman", "compose"),
        environment={"POSTGRES_PASSWORD": "kept", "FLEET_COORDINATOR_PORT": "12345"},
    )

    updated = base.with_environment({"FLEET_PAPER_ENV_FILE": "/tmp/paper.env"})

    assert updated.environment == {
        "POSTGRES_PASSWORD": "kept",
        "FLEET_COORDINATOR_PORT": "12345",
        "FLEET_PAPER_ENV_FILE": "/tmp/paper.env",
    }
    assert base.environment == {"POSTGRES_PASSWORD": "kept", "FLEET_COORDINATOR_PORT": "12345"}


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
    assert "internal: true" not in compose
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
    assert "FLEET_ALLOW_LIVE_MUTATIONS" not in compose
    assert "FLEET_PAPER_IBKR_CLIENT_ID" not in compose
    assert "FLEET_LIVE_IBKR_CLIENT_ID" not in compose
    paper_env = (qualification.REPOSITORY_ROOT / "deploy/fleet/env/paper.env.example").read_text(encoding="utf-8")
    live_env = (qualification.REPOSITORY_ROOT / "deploy/fleet/env/live.env.example").read_text(encoding="utf-8")
    assert "IBKR_CLIENT_ID=1201" in paper_env
    assert "IBKR_CLIENT_ID=1202" in live_env
    assert "ALPACA_MARKET_STATUS_UPSTREAM_URL" not in compose
    assert "${LEAN_DATA_VOLUME_HOST_PATH:-./data-lake-volume}:/lean-data-writer:rw,z" in compose
    assert "./PythonDataService/cache:/app/cache:z" in compose
    assert "FLEET_POSTGRES_PASSWORD" not in compose
    assert "POSTGRES_URL:" not in compose
    assert "REDIS_URL:" not in compose
    assert "POLYGON_API_KEY: ${POLYGON_API_KEY:-}" in compose
    assert "healthcheck:" in compose
    assert "/app/cache:size=256m,mode=1777" in compose
    assert "TRUSTED_HOSTS: localhost,127.0.0.1,fleet-coordinator,backend" in compose
    assert "alpaca-paper-clerk,alpaca-live-clerk,fleet-coordinator" in compose
    coordinator_env = (qualification.REPOSITORY_ROOT / "deploy/fleet/env/coordinator.env.example").read_text(encoding="utf-8")
    assert "POSTGRES_URL=" in coordinator_env
    assert "REDIS_URL=" in coordinator_env


def test_qualification_overlay_keeps_actual_roles_and_only_fakes_external_dependencies() -> None:
    """Qualification may fake providers, never the coordinator or clerk runtime."""
    overlay = (qualification.REPOSITORY_ROOT / "compose.fleet.qualification.yaml").read_text(encoding="utf-8")

    assert "fleet-qualification-enroller" in overlay
    assert "fleet-fake-paper-provider" in overlay
    assert "fleet-fake-live-provider" in overlay
    assert "fleet-coordinator:\n    profiles" in overlay
    assert "alpaca-paper-clerk:\n    profiles" in overlay
    assert "alpaca-live-clerk:\n    profiles" in overlay
    assert '"--container-role", "coordinator"' not in overlay
    assert '"--container-role", "lane"' not in overlay
    assert "fleet-coordinator-postgres" in overlay
    assert overlay.count('restart: "no"') == 2
    assert overlay.count("disable: true") == 2
    assert "FLEET_QUALIFICATION_ACCOUNT_MODE: paper" in overlay
    assert "FLEET_QUALIFICATION_ACCOUNT_MODE: live" in overlay


def test_qualification_uses_declared_alpaca_reads_not_raw_probe_endpoints() -> None:
    """The host evidence reaches the SDK/client and market-status consumer seams."""
    script_path = (
        qualification.REPOSITORY_ROOT
        / "PythonDataService/scripts/run_broker_fleet_compose_qualification.py"
    )
    script = script_path.read_text(encoding="utf-8")

    assert '"/api/brokers/alpaca/account"' in script
    assert '"/api/brokers/alpaca/market-status-snapshot"' in script
    assert '"/v2/account"' in script
    assert "/read" not in script


def test_fault_matrix_is_machine_readable_and_excludes_coordinator_outage() -> None:
    """D evidence records every Paper fault without claiming an E scenario."""
    assert {"passed", "attempted", "unrun"} == qualification.FAULT_STATES
    result = qualification._fault_result("unrun", "bounded host unavailable", {"clerk_id": "live"})
    assert result["state"] == "unrun"
    with pytest.raises(qualification.QualificationError, match="Unknown fault"):
        qualification._fault_result("failed", "not a vocabulary value", {})


def test_every_declared_fault_scenario_is_actually_populated_by_the_host_run() -> None:
    """The tuple is a manifest; this asserts the ceremony honours it.

    Comparing FAULT_SCENARIOS to a literal copy of itself cannot catch the one
    drift that matters — a scenario declared in the vocabulary and never
    assigned in run_host_qualification, which _assert_all_faults_passed would
    then fail at runtime on the host, hours into a maintenance window.
    """
    import ast

    source = Path(qualification.__file__).read_text(encoding="utf-8")
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "run_host_qualification"
    )
    populated = {
        target.slice.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == "faults"
        and isinstance(target.slice, ast.Constant)
        and isinstance(target.slice.value, str)
    }
    assert populated == set(qualification.FAULT_SCENARIOS)


def test_partial_fault_matrix_cannot_be_labelled_passed() -> None:
    """An attempted or missing fault remains failed qualification evidence."""
    faults = {
        scenario: qualification._fault_result("passed", "proved", {"clerk_id": "live"})
        for scenario in qualification.FAULT_SCENARIOS
    }
    qualification._assert_all_faults_passed(faults)
    faults["credential_refusal_restart"] = qualification._fault_result(
        "attempted", "process did not refuse", {"clerk_id": "live"}
    )

    with pytest.raises(qualification.QualificationError, match="nonpassing"):
        qualification._assert_all_faults_passed(faults)

    del faults["provider_outage"]
    with pytest.raises(qualification.QualificationError, match="missing"):
        qualification._assert_all_faults_passed(faults)


def test_qualification_runtime_uses_enrollment_namespace(tmp_path: Path) -> None:
    """Agents cannot accidentally boot in the production-default namespace."""
    coordinator_env = tmp_path / "coordinator.env"

    assert qualification._qualification_runtime_environment(
        coordinator_env=coordinator_env,
        deployment_namespace="compose:fleetqualification123",
    ) == {
        "FLEET_COORDINATOR_ENV_FILE": str(coordinator_env),
        "FLEET_DEPLOYMENT_NAMESPACE": "compose:fleetqualification123",
    }


def test_qualification_env_is_created_restricted_and_never_overwritten(
    tmp_path: Path,
) -> None:
    """Secret-bearing ceremony files are private before their first byte."""
    path = tmp_path / "paper.env"

    qualification._write_env(path, {"FLEET_AGENT_SERVICE_TOKEN": "first"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "FLEET_AGENT_SERVICE_TOKEN=first\n"
    with pytest.raises(FileExistsError):
        qualification._write_env(path, {"FLEET_AGENT_SERVICE_TOKEN": "replacement"})
    assert path.read_text(encoding="utf-8") == "FLEET_AGENT_SERVICE_TOKEN=first\n"


def test_qualification_coordinator_environment_has_complete_support_store_urls(
    tmp_path: Path,
) -> None:
    """The tracked qualification topology contains no connection strings."""
    # The concrete URLs are generated into the private coordinator file; this
    # narrowly exercises the same input used by the host ceremony without
    # provisioning a real Compose project.
    values = {
        "POSTGRES_URL": "postgresql://postgres:qualification-postgres-password@fleet-db:5432/postgres",
        "REDIS_URL": "redis://fleet-redis:6379/0",
    }
    path = tmp_path / "coordinator.env"
    qualification._write_env(path, values)
    assert path.read_text(encoding="utf-8") == (
        "POSTGRES_URL=postgresql://postgres:qualification-postgres-password@fleet-db:5432/postgres\n"
        "REDIS_URL=redis://fleet-redis:6379/0\n"
    )


def test_cleanup_failure_changes_qualification_evidence_to_failed() -> None:
    """A failed scoped teardown cannot leave a passing evidence record."""
    class ComposeStub:
        def run(self, _project: str, _args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise qualification.QualificationError("engine refused teardown")

    evidence: dict[str, object] = {"result": "passed", "stage": "complete"}
    error = qualification._cleanup_qualification(
        cast(qualification.ComposeCommand, ComposeStub()), "qualification", evidence
    )

    assert error is not None
    assert evidence == {
        "result": "failed",
        "stage": "complete",
        "failure": {"stage": "cleanup", "error_type": "QualificationError"},
    }


def test_cleanup_failure_preserves_an_earlier_qualification_failure() -> None:
    """Teardown trouble must not erase the root failure's evidence stage."""
    class ComposeStub:
        def run(self, _project: str, _args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise qualification.QualificationError("engine refused teardown")

    evidence: dict[str, object] = {
        "result": "failed",
        "failure": {"stage": "lane_startup", "error_type": "QualificationError"},
    }
    qualification._cleanup_qualification(
        cast(qualification.ComposeCommand, ComposeStub()), "qualification", evidence
    )

    assert evidence["failure"] == {"stage": "lane_startup", "error_type": "QualificationError"}
    assert evidence["cleanup_failure"] == {"stage": "cleanup", "error_type": "QualificationError"}


def test_capacity_probe_runs_as_module_inside_application_image() -> None:
    """The real-image probe keeps `/app` on the import path for middleware imports."""
    calls: list[list[str]] = []

    class ComposeStub:
        def run(
            self,
            project: str,
            args: list[str],
            *,
            timeout_s: float,
        ) -> subprocess.CompletedProcess[str]:
            assert project == "qualification"
            assert timeout_s == 20.0
            calls.append(args)
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "kind": "request",
                        "refusal_status": 503,
                        "reason": "fleet_lane_capacity_exhausted",
                    }
                ),
                stderr="",
            )

    qualification._capacity_probe(
        cast(qualification.ComposeCommand, ComposeStub()), "qualification", "request"
    )

    assert calls[0][3:6] == ["python", "-m", "scripts.run_broker_fleet_compose_qualification"]


def test_stream_capacity_client_does_not_parse_held_sse_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first held SSE body is opaque; only the typed refusal is JSON."""
    monkeypatch.setattr(qualification, "_qualification_local_hold", lambda path: 200)
    monkeypatch.setattr(
        qualification,
        "_qualification_local_call",
        lambda path: (503, {"reason": "fleet_lane_capacity_exhausted"}),
    )

    assert qualification._run_capacity_client("stream") == 0
    assert json.loads(capsys.readouterr().out) == {
        "kind": "stream",
        "reason": "fleet_lane_capacity_exhausted",
        "refusal_status": 503,
    }

def test_full_stack_overlay_suppresses_combined_and_retargets_ingress() -> None:
    """The shipped Backend/Frontend resolve the coordinator, never combined mode."""
    if shutil.which("docker") is None and shutil.which("podman") is None:
        pytest.skip("requires Docker or Podman for a real Compose render")
    environment = {
        **os.environ,
        "FLEET_POSTGRES_PASSWORD": "qualification-postgres-password",
        "POSTGRES_PASSWORD": "qualification-postgres-password",
        "REDIS_PASSWORD": "qualification-redis-password",
        "DATA_PLANE_CONTROL_SECRET": "qualification-control-secret",
        "POLYGON_API_KEY": "qualification-polygon-placeholder",
    }
    compose_command = qualification.ComposeCommand.discover().compose
    result = subprocess.run(
        [
            *compose_command,
            "--project-name",
            "fleet-overlay-test",
            "--file",
            str(qualification.REPOSITORY_ROOT / "compose.yaml"),
            "--file",
            str(qualification.REPOSITORY_ROOT / "compose.fleet.yaml"),
            "--profile",
            "fleet",
            "config",
            "--services",
        ],
        cwd=qualification.REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )
    services = set(result.stdout.splitlines())
    assert "python-service" not in services
    assert {"fleet-coordinator", "backend", "frontend"} <= services

    rendered = subprocess.run(
        [
            *compose_command,
            "--project-name",
            "fleet-overlay-test",
            "--file",
            str(qualification.REPOSITORY_ROOT / "compose.yaml"),
            "--file",
            str(qualification.REPOSITORY_ROOT / "compose.fleet.yaml"),
            "--profile",
            "fleet",
            "config",
        ],
        cwd=qualification.REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    ).stdout
    assert "PolygonService__BaseUrl: http://fleet-coordinator:8000" in rendered
    assert "DATA_PLANE_PROXY_TARGET: http://fleet-coordinator:8000" in rendered


def test_assert_no_custody_root_refuses_custody_named_artifact(tmp_path: Path) -> None:
    """The coordinator proof rejects an unknown nested artifact and table."""
    control_root = tmp_path / "control"
    registry_directory = control_root / "fleet"
    registry_directory.mkdir(parents=True)
    connection = sqlite3.connect(registry_directory / "registry.db")
    schema.configure_connection(connection)
    schema.apply_schema(connection)
    connection.close()
    result = qualification._assert_no_custody_root(control_root)
    assert result["ok"] is True
    assert set(result["tables"]) == qualification._FLEET_REGISTRY_TABLES
    (registry_directory / "nested.bin").touch()

    with pytest.raises(qualification.QualificationError, match="unapproved material"):
        qualification._assert_no_custody_root(control_root)


def test_assert_no_custody_root_requires_exact_registry_schema(tmp_path: Path) -> None:
    """A neutral or partial database is not accepted as coordinator-only state."""
    control_root = tmp_path / "control"
    registry_directory = control_root / "fleet"
    registry_directory.mkdir(parents=True)
    connection = sqlite3.connect(registry_directory / "registry.db")
    connection.execute("CREATE TABLE fleet_meta (id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(qualification.QualificationError, match="table set is not exact"):
        qualification._assert_no_custody_root(control_root)


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


def test_coordinator_mount_isolation_rejects_a_lane_source() -> None:
    """A distinct visible source is insufficient if coordinator also mounts it."""
    inspection = {
        "Mounts": [
            {"Source": "/volumes/control", "Destination": "/app/artifacts/fleet"},
            {"Source": "/volumes/paper", "Destination": "/elsewhere"},
        ]
    }
    with pytest.raises(qualification.QualificationError, match="lane source"):
        qualification._assert_coordinator_mount_isolation(inspection, {"/volumes/paper", "/volumes/live"})


def test_live_probe_mutation_refusal_is_a_typed_non_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI serializes the closed coordinator registry evidence."""
    root = tmp_path / "control"
    root.mkdir()
    registry_directory = root / "fleet"
    registry_directory.mkdir()
    connection = sqlite3.connect(registry_directory / "registry.db")
    schema.configure_connection(connection)
    schema.apply_schema(connection)
    connection.close()
    result = qualification.main(["--assert-no-custody-root", str(root)])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
