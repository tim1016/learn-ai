"""Focused checks for the real-Compose fleet qualification harness."""

from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast

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
        "FLEET_MAX_INFLIGHT_COMMANDS",
        "FLEET_REQUEST_QUEUE_LIMIT",
        "FLEET_REQUEST_QUEUE_TIMEOUT_MS",
    ):
        assert variable in compose
    assert "FLEET_ALLOW_LIVE_MUTATIONS" not in compose
    assert "FLEET_PAPER_IBKR_CLIENT_ID" not in compose
    assert "FLEET_LIVE_IBKR_CLIENT_ID" not in compose
    paper_env = (qualification.REPOSITORY_ROOT / "deploy/fleet/env/paper.env.example").read_text(encoding="utf-8")
    live_env = (qualification.REPOSITORY_ROOT / "deploy/fleet/env/live.env.example").read_text(encoding="utf-8")
    # 1201/1202 was the originally planned pair; 583e330c deliberately
    # changed the running override to 2/1 (round-2 Codex review on #2116)
    # without updating this assertion. The env files transcribe what
    # actually runs, not the original plan — assert the running values.
    # Exact whole-line match, not a substring: "IBKR_CLIENT_ID=2" also
    # matches "IBKR_CLIENT_ID=20", so a later drift to a two-digit client
    # id would satisfy a substring check while silently changing the
    # running value this test claims to pin.
    assert "IBKR_CLIENT_ID=2" in paper_env.splitlines()
    assert "IBKR_CLIENT_ID=1" in live_env.splitlines()
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
    script = Path(qualification.__file__).read_text(encoding="utf-8")

    assert '"/api/brokers/alpaca/account"' in script
    assert '"/api/brokers/alpaca/market-status-snapshot"' in script
    assert '"/v2/account"' in script
    assert "/read" not in script


def test_fault_matrix_is_machine_readable_and_excludes_coordinator_outage() -> None:
    """D evidence records every Paper fault without claiming an E scenario."""
    assert "coordinator_outage" not in qualification.FAULT_SCENARIOS
    assert {"passed", "attempted", "unrun"} == qualification.FAULT_STATES
    result = qualification._fault_result("unrun", "bounded host unavailable", {"clerk_id": "live"})
    assert result["state"] == "unrun"
    with pytest.raises(qualification.QualificationError, match="Unknown fault"):
        qualification._fault_result("failed", "not a vocabulary value", {})


def test_every_declared_fault_scenario_is_actually_populated_by_the_host_run() -> None:
    """The tuple is a manifest; this asserts both that it hasn't shrunk and that
    the ceremony honours it.

    Belt and braces, on purpose, because the two checks fail differently. The
    literal set below catches the vocabulary itself silently shrinking (a
    scenario deleted from FAULT_SCENARIOS, which the AST walk below would
    happily call fully "populated" since there is nothing left to check). The
    AST walk catches the complementary drift a literal copy of FAULT_SCENARIOS
    cannot: a scenario declared in the vocabulary and never assigned in
    run_host_qualification, which _assert_all_faults_passed would then fail at
    runtime on the host, hours into a maintenance window. If a scenario is
    ever extracted into a helper, keep its `faults[...] =` assignment at the
    call site in run_host_qualification, or this check goes red on an
    otherwise-correct refactor.
    """
    assert set(qualification.FAULT_SCENARIOS) == {
        "provider_outage",
        "credential_refusal_restart",
        "volume_marker_poison_mismount_refusal",
        "request_queue_saturation",
        "stream_saturation",
        "command_pool_isolation",
    }
    source = Path(qualification.__file__).read_text(encoding="utf-8")
    function = next(
        (
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run_host_qualification"
        ),
        None,
    )
    assert function is not None, "run_host_qualification was renamed or removed; update this test's target."
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


def test_parse_args_accepts_build_timeout_s_and_defaults_to_120() -> None:
    """A cold CI runner needs a wider image-build budget than a warm host."""
    assert qualification._parse_args([]).build_timeout_s == 120.0
    assert qualification._parse_args(["--build-timeout-s", "1500"]).build_timeout_s == 1500.0


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
    monkeypatch.setattr(qualification, "_qualification_local_hold", lambda path, *, method="GET": 200)
    monkeypatch.setattr(
        qualification,
        "_qualification_local_call",
        lambda path, *, method="GET": (503, {"reason": "fleet_lane_capacity_exhausted"}),
    )

    assert qualification._run_capacity_client("stream") == 0
    assert json.loads(capsys.readouterr().out) == {
        "kind": "stream",
        "reason": "fleet_lane_capacity_exhausted",
        "refusal_status": 503,
    }


def test_command_admission_client_holds_a_read_and_admits_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #2206: ``kind="command"`` now proves command-pool *isolation*,
    not self-pool saturation -- a held read (GET /hold/request) must not
    starve an admitted command (POST /hold/command). The overlap is proven
    deterministically: the second read's typed 503 is asserted before the
    command is ever issued, not inferred from timing alone."""
    hold_calls: list[tuple[str, str]] = []
    call_calls: list[tuple[str, str]] = []

    def fake_hold(path: str, *, method: str = "GET") -> int:
        hold_calls.append((path, method))
        return 200

    def fake_call(path: str, *, method: str = "GET") -> tuple[int, dict[str, object]]:
        call_calls.append((path, method))
        if path == "/hold/request":
            return 503, {"reason": "fleet_lane_capacity_exhausted"}
        return 200, {"held": "command"}

    monkeypatch.setattr(qualification, "_qualification_local_hold", fake_hold)
    monkeypatch.setattr(qualification, "_qualification_local_call", fake_call)

    assert qualification._run_capacity_client("command") == 0
    assert hold_calls == [("/hold/request", "GET")]
    assert call_calls == [("/hold/request", "GET"), ("/hold/command", "POST")]
    assert json.loads(capsys.readouterr().out) == {
        "kind": "command",
        "second_read_refusal_status": 503,
        "command_status": 200,
    }


def test_command_admission_client_fails_loudly_if_the_read_hold_is_not_provably_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the second read is not refused, the overlap was never proven --
    the command admission below would be meaningless, so this must not
    silently continue."""
    monkeypatch.setattr(qualification, "_qualification_local_hold", lambda path, *, method="GET": 200)
    monkeypatch.setattr(
        qualification, "_qualification_local_call", lambda path, *, method="GET": (200, {"held": "request"})
    )

    with pytest.raises(qualification.QualificationError, match="provably in flight"):
        qualification._run_capacity_client("command")


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


# ---- issue #2206: recorded-history ceremony plumbing ------------------------


def test_request_json_encodes_json_body_and_sets_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--call-body`` / mode-control path must send a real JSON POST body."""
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = HTTPStatus.OK

        def read(self) -> bytes:
            return b'{"mode": "unavailable"}'

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
            captured["method"] = request.get_method()
            captured["data"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            captured["secret_header"] = request.get_header("X-fleet-qualification-secret")
            return FakeResponse()

    monkeypatch.setattr(
        qualification.urllib.request, "build_opener", lambda *_args, **_kwargs: FakeOpener()
    )

    status, body = qualification._request_json(
        "http://coordinator/internal/fleet-qualification-history/mode",
        method="POST",
        headers={"X-Fleet-Qualification-Secret": "s3cr3t"},
        json_body={"mode": "unavailable"},
    )

    assert status == HTTPStatus.OK
    assert body == {"mode": "unavailable"}
    assert captured["method"] == "POST"
    assert json.loads(captured["data"]) == {"mode": "unavailable"}
    assert captured["content_type"] == "application/json"
    assert captured["secret_header"] == "s3cr3t"


def test_exec_probe_forwards_body_and_qualification_secret_flag() -> None:
    """``_exec_probe`` must round-trip a JSON body and the secret flag as CLI args."""
    calls: list[list[str]] = []

    class ComposeStub:
        def run(self, project: str, args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"status": 200, "body": {"held": "command"}}), stderr=""
            )

    status, body = qualification._exec_probe(
        cast(qualification.ComposeCommand, ComposeStub()),
        "qualification",
        "alpaca-paper-clerk",
        "/internal/fleet-qualification/hold/command",
        method="POST",
        json_body={"strategy_instance_id": "bot", "symbol": "QUALHIST"},
        with_qualification_secret=True,
    )

    assert status == 200
    assert body == {"held": "command"}
    args = calls[0]
    assert "--call-body" in args
    assert json.loads(args[args.index("--call-body") + 1]) == {
        "strategy_instance_id": "bot",
        "symbol": "QUALHIST",
    }
    assert "--call-with-qualification-secret" in args
    assert args[args.index("--call-url") + 1] == "http://localhost:8000/internal/fleet-qualification/hold/command"


def test_call_with_qualification_secret_attaches_the_proven_forward_headers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #2206 fix (M1): a bare qualification-secret header is refused
    400 ``broker_and_clerk_required`` by the real clerk identity fence
    (``FleetIdentityMiddleware(refuse_unpinned_mutations=True)``, #2075) --
    the qualification secret alone was never proof of a coordinator forward.
    ``--call-with-qualification-secret`` must also attach the coordinator's
    proven-forward headers (``X-Fleet-Coordinator-Token``, ``X-Fleet-Broker``,
    ``X-Fleet-Clerk-Id``) so a POST like ``/hold/command`` reaches its
    handler for real. Fails before the fix: an older harness sent only the
    qualification secret and was refused (proven directly against the real
    middleware stack in ``tests/routers/test_fleet_qualification.py::
    test_hold_command_requires_a_proven_coordinator_forward_through_the_real_middleware_stack``).
    """
    captured_headers: dict[str, str] = {}

    def fake_request_json(url: str, *, method: str = "GET", headers=None, timeout_s=3.0, json_body=None):
        captured_headers.update(headers or {})
        return HTTPStatus.OK, {"held": "command"}

    monkeypatch.setattr(qualification, "_request_json", fake_request_json)
    monkeypatch.setenv("FLEET_QUALIFICATION_PROBE_SECRET", "probe-secret")
    monkeypatch.setenv("FLEET_COORDINATOR_SERVICE_TOKEN", "coord-token")
    monkeypatch.setenv("FLEET_CLERK_ID", "clrk_probe")

    result = qualification.main(
        [
            "--call-url",
            "http://localhost:8000/internal/fleet-qualification/hold/command",
            "--call-method",
            "POST",
            "--call-with-qualification-secret",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"status": 200, "body": {"held": "command"}}
    assert captured_headers == {
        "X-Fleet-Qualification-Secret": "probe-secret",
        "X-Fleet-Coordinator-Token": "coord-token",
        "X-Fleet-Broker": "alpaca",
        "X-Fleet-Clerk-Id": "clrk_probe",
    }


def test_call_with_qualification_secret_requires_the_coordinator_token_and_clerk_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container with the qualification secret but no coordinator identity
    cannot silently send a half-proven forward."""
    monkeypatch.setenv("FLEET_QUALIFICATION_PROBE_SECRET", "probe-secret")
    monkeypatch.delenv("FLEET_COORDINATOR_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("FLEET_CLERK_ID", raising=False)

    with pytest.raises(qualification.QualificationError, match="coordinator token or clerk id"):
        qualification.main(
            [
                "--call-url",
                "http://localhost:8000/internal/fleet-qualification/hold/command",
                "--call-method",
                "POST",
                "--call-with-qualification-secret",
            ]
        )


def test_set_recorded_history_mode_raises_on_a_refused_mode_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qualification, "_request_json", lambda *_args, **_kwargs: (503, {"reason": "not found"})
    )

    with pytest.raises(qualification.QualificationError, match="refused recorded-history mode"):
        qualification._set_recorded_history_mode("http://coordinator", "secret", "unavailable")


def test_history_client_probe_runs_the_history_client_role_inside_the_paper_container() -> None:
    """Must ``compose exec`` the Paper container with the real, importable
    ``history-client`` role -- not the public chart route or a bespoke path."""
    calls: list[list[str]] = []

    class ComposeStub:
        def run(self, project: str, args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            assert timeout_s == 70.0
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"bars": [], "overlay_notices": []}), stderr=""
            )

    body = qualification._history_client_probe(cast(qualification.ComposeCommand, ComposeStub()), "qualification")

    assert body == {"bars": [], "overlay_notices": []}
    args = calls[0]
    assert args[:6] == [
        "exec", "-T", "alpaca-paper-clerk", "python", "-m", "scripts.run_broker_fleet_compose_qualification",
    ]
    assert args[6:] == ["--container-role", "history-client"]


def test_recorded_history_ceremony_drives_healthy_failure_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #2206 acceptance criteria 1-3, exercised against a fully faked
    coordinator/exec layer so the sequencing and assertions are pinned
    without any real container. Owner-scoped design (2026-09-18): drives
    the in-container history client, never the public chart route or a
    strategy-instance seed."""
    modes: list[str] = []
    history_calls = 0

    def fake_request_json(url: str, *, method: str = "GET", headers=None, timeout_s=3.0, json_body=None):
        assert url.endswith("/internal/fleet-qualification-history/mode")
        modes.append(json_body["mode"])
        return HTTPStatus.OK, {"mode": json_body["mode"]}

    def fake_history_client_probe(compose, project):
        nonlocal history_calls
        history_calls += 1
        mode = modes[-1]
        if mode == "unavailable":
            return {
                "bars": [],
                "overlay_notices": [{"code": "coordinator_unavailable", "message": "x", "source": "polygon"}],
            }
        return {
            "bars": [{"start_ms": 1, "end_ms": 2, "source": "polygon"}],
            "overlay_notices": [],
        }

    monkeypatch.setattr(qualification, "_request_json", fake_request_json)
    monkeypatch.setattr(qualification, "_history_client_probe", fake_history_client_probe)

    result = qualification._recorded_history_ceremony(
        cast(qualification.ComposeCommand, object()),
        "qualification",
        coordinator_url="http://coordinator",
        qualification_secret="probe-secret",
    )

    # healthy, unavailable, healthy (recovery).
    assert modes == ["healthy", "unavailable", "healthy"]
    assert history_calls == 3
    assert result["healthy_bar_count"] == 1
    assert result["retried_bar_count"] == 1
    assert result["injected_unavailable_notice_codes"] == ["coordinator_unavailable"]


def test_recorded_history_ceremony_fails_loudly_when_the_healthy_read_returns_no_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qualification, "_request_json", lambda *_a, **_k: (HTTPStatus.OK, {"mode": "healthy"}))
    monkeypatch.setattr(
        qualification, "_history_client_probe", lambda compose, project: {"bars": [], "overlay_notices": []}
    )

    with pytest.raises(qualification.QualificationError, match="returned no candles"):
        qualification._recorded_history_ceremony(
            cast(qualification.ComposeCommand, object()),
            "qualification",
            coordinator_url="http://coordinator",
            qualification_secret="probe-secret",
        )


def test_host_ceremony_shares_the_qualification_probe_secret_with_the_coordinator() -> None:
    """Issue #2206: the coordinator's own recorded-history gate needs the same
    per-run probe secret every lane env file already carries. Checked as text
    within ``_host_ceremony``'s own source, mirroring this file's other
    source-level checks (real Compose/Docker is unavailable in this suite).
    """
    source = Path(qualification.__file__).read_text(encoding="utf-8")
    start = source.index("def _host_ceremony(")
    end = source.index("\ndef _wait_for_http(")
    ceremony_source = source[start:end]

    assert '"FLEET_QUALIFICATION_PROBE_SECRET": probe_secret,' in ceremony_source
    assert 'runtime_environment["FLEET_QUALIFICATION_PROBE_SECRET"] = probe_secret' in ceremony_source


def test_run_host_qualification_calls_the_recorded_history_ceremony_before_the_paper_poison_fault() -> None:
    """The recorded-history stage must run while Paper is still healthy -- the
    poison fault deliberately leaves Paper down for the rest of the ceremony.
    """
    source = Path(qualification.__file__).read_text(encoding="utf-8")
    recorded_history_index = source.index("_recorded_history_ceremony(\n", source.index("def run_host_qualification"))
    poison_index = source.index('evidence["stage"] = "volume_marker_poison_mismount_refusal"')

    assert recorded_history_index < poison_index
