"""Run the test-only Compose isolation qualification for ADR 0062 delivery D.

The host command starts a fresh, randomly named Compose project using the
qualification overlay. It proves properties Compose alone cannot express:
physical volume sources are distinct despite an identical in-container mount
path, the coordinator has no custody root, Paper can be killed, and Live still
reads from its independent fake provider and market-data source. It never
contacts a real broker, consumes credentials, or enables Live mutation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUBPROCESS_TIMEOUT_S = 30.0
COMPOSE_FILES = (
    REPOSITORY_ROOT / "compose.fleet.yaml",
    REPOSITORY_ROOT / "compose.fleet.qualification.yaml",
)
QUALIFICATION_SERVICES = (
    "fleet-coordinator",
    "alpaca-paper-clerk",
    "alpaca-live-clerk",
)
LANE_SERVICES = ("alpaca-paper-clerk", "alpaca-live-clerk")
_CUSTODY_TERMS = ("custody", "profile", "arming", "ledger", "order", "position")


class QualificationError(RuntimeError):
    """A topology assertion failed; retained evidence explains the failure."""


@dataclass(frozen=True, slots=True)
class ComposeCommand:
    """The host container engine and Compose invocation selected for this run."""

    engine: str
    compose: tuple[str, ...]
    environment: dict[str, str] | None = None

    @classmethod
    def discover(cls) -> ComposeCommand:
        """Select Docker Compose first, then Podman's Compose provider."""
        if shutil.which("docker") is not None:
            return cls(engine="docker", compose=("docker", "compose"))
        if shutil.which("podman") is not None:
            return cls(engine="podman", compose=("podman", "compose"))
        raise QualificationError(
            "Neither Docker nor Podman is available. Run this host-only qualification "
            "where the supported container engine is installed."
        )

    def run(
        self,
        project: str,
        args: list[str],
        *,
        check: bool = True,
        timeout_s: float = DEFAULT_SUBPROCESS_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        """Invoke Compose against only this repository's two fleet files."""
        command = [*self.compose, "--project-name", project]
        for compose_file in COMPOSE_FILES:
            command.extend(("--file", str(compose_file)))
        command.extend(args)
        environment = os.environ.copy()
        if self.environment is not None:
            environment.update(self.environment)
        try:
            return subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                check=check,
                capture_output=True,
                text=True,
                env=environment,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise QualificationError(
                f"Compose command exceeded its {timeout_s:g}s bound: {args[0]!r}."
            ) from exc

    def inspect(self, container_id: str) -> dict[str, Any]:
        """Return one engine inspection document for a known project container."""
        try:
            result = subprocess.run(
                [self.engine, "inspect", container_id],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=DEFAULT_SUBPROCESS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise QualificationError(
                f"Container inspection exceeded its {DEFAULT_SUBPROCESS_TIMEOUT_S:g}s bound."
            ) from exc
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise QualificationError(f"Unexpected {self.engine} inspect payload for {container_id!r}.")
        return payload[0]


def _request_json(
    url: str,
    *,
    method: str = "GET",
    timeout_s: float = 3.0,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call one test-only HTTP endpoint without inheriting proxy settings."""
    request = urllib.request.Request(url, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body)


def _serve_json(port: int, handler: type[BaseHTTPRequestHandler]) -> None:
    """Run one bounded-purpose test server until Compose stops its container."""
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.serve_forever()


def _json_handler(payload_for_path: dict[str, dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    """Create an inert fake endpoint handler with no credential behaviour."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = payload_for_path.get(self.path)
            if payload is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Keep fake-endpoint request lines out of qualification output."""
            del format, args

    return Handler


def _run_fake_provider() -> int:
    provider = os.environ.get("FLEET_PROBE_PROVIDER", "unknown")
    port = 8011 if provider == "paper" else 8012
    _serve_json(port, _json_handler({"/health": {"ok": True}, "/read": {"provider": provider}}))
    return 0


def _run_fake_market_data() -> int:
    _serve_json(8013, _json_handler({"/health": {"ok": True}, "/read": {"market_data": "read_only"}}))
    return 0


def _container_id(compose: ComposeCommand, project: str, service: str) -> str:
    result = compose.run(project, ["ps", "-q", service])
    container_id = result.stdout.strip()
    if not container_id:
        raise QualificationError(f"Compose did not create {service!r} in project {project!r}.")
    return container_id


def _mount_at(inspect: dict[str, Any], destination: str) -> dict[str, Any]:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise QualificationError("Container inspection has no mount list.")
    for mount in mounts:
        if isinstance(mount, dict) and mount.get("Destination") == destination:
            return mount
    raise QualificationError(f"Container has no expected mount at {destination}.")


def _assert_limits(inspect: dict[str, Any], service: str) -> dict[str, Any]:
    host_config = inspect.get("HostConfig")
    if not isinstance(host_config, dict):
        raise QualificationError(f"{service} has no host resource configuration.")
    for field in ("NanoCpus", "Memory", "PidsLimit"):
        value = host_config.get(field)
        if not isinstance(value, int) or value <= 0:
            raise QualificationError(f"{service} has no positive {field} resource limit.")
    return {field: host_config[field] for field in ("NanoCpus", "Memory", "PidsLimit")}


def _assert_coordinator_secret_absence(inspect: dict[str, Any]) -> None:
    config = inspect.get("Config")
    environment = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(environment, list):
        raise QualificationError("Coordinator inspection has no environment list.")
    forbidden = ("ALPACA_API_KEY", "ALPACA_API_SECRET", "IBKR_")
    leaks = [item.split("=", 1)[0] for item in environment if isinstance(item, str) and item.startswith(forbidden)]
    if leaks:
        raise QualificationError(f"Coordinator carries broker credential/feed variables: {sorted(leaks)}")


def _assert_private_agent_ports(inspect: dict[str, Any], service: str) -> None:
    network_settings = inspect.get("NetworkSettings")
    ports = network_settings.get("Ports") if isinstance(network_settings, dict) else None
    if ports is None:
        return
    if not isinstance(ports, dict) or any(value is not None for value in ports.values()):
        raise QualificationError(f"{service} unexpectedly publishes a host port: {ports!r}")


def _exec_probe(compose: ComposeCommand, project: str, service: str, path: str, *, method: str = "GET") -> tuple[int, dict[str, Any]]:
    target = path if path.startswith("http://") or path.startswith("https://") else f"http://localhost:8000{path}"
    result = compose.run(
        project,
        [
            "exec",
            "-T",
            service,
            "python",
            "/app/scripts/run_broker_fleet_compose_qualification.py",
            "--call-url",
            target,
            "--call-method",
            method,
        ],
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), int):
        raise QualificationError(f"{service} returned malformed probe evidence.")
    body = payload.get("body")
    if not isinstance(body, dict):
        raise QualificationError(f"{service} returned a non-object probe response.")
    return payload["status"], body


def _wait_for_lanes(compose: ComposeCommand, project: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            statuses = [_exec_probe(compose, project, service, "/health")[0] for service in QUALIFICATION_SERVICES]
        except (QualificationError, subprocess.CalledProcessError, json.JSONDecodeError):
            time.sleep(1)
            continue
        if statuses == [HTTPStatus.OK, HTTPStatus.OK, HTTPStatus.OK]:
            return
        time.sleep(1)
    raise QualificationError("Fleet qualification services did not become healthy before the deadline.")


def _json_line(result: subprocess.CompletedProcess[str], operation: str) -> dict[str, Any]:
    """Read one operator CLI response without exposing its credentials in logs."""
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QualificationError(f"{operation} returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise QualificationError(f"{operation} returned a non-object response.")
    return payload


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Write a qualification-only env file with restrictive host permissions."""
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(0o600)


def _host_ceremony(compose: ComposeCommand, project: str, temporary_dir: Path) -> ComposeCommand:
    """Provision test-only identities and return Compose with their env files."""
    control = "/app/artifacts/fleet"
    base = ["run", "--rm", "--no-deps", "fleet-qualification-enroller", "python", "-m", "scripts.manage_broker_fleet"]
    compose.run(project, ["run", "--rm", "--no-deps", "fleet-qualification-enroller", "python", "-m", "scripts.manage_data_root", "init", "--base-root", "/lean-data-writer", "--root-id", "00000000-0000-0000-0000-000000000000"])
    compose.run(project, [*base, "init", "--control-dir", control])
    deployment_namespace = f"compose:{project}"
    lanes: dict[str, dict[str, Any]] = {}
    for lane in ("paper", "live"):
        volume = f"{project}_fleet-alpaca-{lane}-data"
        result = compose.run(
            project,
            [
                *base,
                "provision",
                "--control-dir",
                control,
                "--broker",
                "alpaca",
                "--label",
                f"Qualification {lane}",
                "--volume-root",
                f"/app/artifacts/clerks/{lane}",
                "--attestation-id",
                volume,
                "--deployment-namespace",
                deployment_namespace,
            ],
        )
        lane_result = _json_line(result, f"provision {lane}")
        required = ("clerk_id", "worker_key", "agent_service_token", "coordinator_service_token")
        if not all(isinstance(lane_result.get(key), str) and lane_result[key] for key in required):
            raise QualificationError(f"Provisioning {lane} did not issue complete fleet identities.")
        compose.run(
            project,
            [
                *base,
                "approve-endpoint",
                "--control-dir",
                control,
                "--clerk-id",
                str(lane_result["clerk_id"]),
                "--endpoint-ref",
                f"alpaca-{lane}-agent",
                "--base-url",
                f"http://alpaca-{lane}-clerk:8000",
            ],
        )
        lanes[lane] = lane_result
    control_secret = "qualification-control-secret"
    coordinator_env = temporary_dir / "coordinator.env"
    _write_env(
        coordinator_env,
        {
            "FLEET_POSTGRES_PASSWORD": "qualification-postgres-password",
            "DATA_PLANE_CONTROL_SECRET": control_secret,
            "FLEET_AGENT_SERVICE_TOKENS_JSON": json.dumps(
                {entry["clerk_id"]: entry["agent_service_token"] for entry in lanes.values()}, separators=(",", ":")
            ),
            "FLEET_COORDINATOR_SERVICE_TOKENS_JSON": json.dumps(
                {entry["clerk_id"]: entry["coordinator_service_token"] for entry in lanes.values()}, separators=(",", ":")
            ),
        },
    )
    runtime_environment = {"FLEET_COORDINATOR_ENV_FILE": str(coordinator_env)}
    for lane, entry in lanes.items():
        lane_env = temporary_dir / f"{lane}.env"
        _write_env(
            lane_env,
            {
                "DATA_PLANE_CONTROL_SECRET": control_secret,
                "FLEET_CLERK_ID": str(entry["clerk_id"]),
                "FLEET_WORKER_KEY": str(entry["worker_key"]),
                "FLEET_AGENT_SERVICE_TOKEN": str(entry["agent_service_token"]),
                "FLEET_COORDINATOR_SERVICE_TOKEN": str(entry["coordinator_service_token"]),
            },
        )
        runtime_environment[f"FLEET_{lane.upper()}_ENV_FILE"] = str(lane_env)
    return ComposeCommand(engine=compose.engine, compose=compose.compose, environment=runtime_environment)


def _wait_for_http(url: str, timeout_s: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Wait for an actual application HTTP surface, bounded by the ceremony timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            status, body = _request_json(url, headers=headers)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(1)
            continue
        if status == HTTPStatus.OK:
            return body
        time.sleep(1)
    raise QualificationError(f"Timed out waiting for {url!r}.")


def run_host_qualification(*, keep: bool, timeout_s: float, evidence_path: Path | None) -> dict[str, Any]:
    """Exercise actual coordinator/agent roles on isolated Compose volumes."""
    bootstrap_discovered = ComposeCommand.discover()
    bootstrap = ComposeCommand(engine=bootstrap_discovered.engine, compose=bootstrap_discovered.compose, environment={"FLEET_POSTGRES_PASSWORD": "qualification-postgres-password"})
    project = f"fleetqualification{uuid.uuid4().hex[:12]}"
    evidence: dict[str, Any] = {"project": project, "compose_files": [str(path) for path in COMPOSE_FILES]}
    started = False
    with tempfile.TemporaryDirectory(prefix="learn-ai-fleet-qualification-") as temporary:
        try:
            bootstrap.run(project, ["--profile", "fleet-qualification", "config"])
            started = True
            compose = _host_ceremony(bootstrap, project, Path(temporary))
            compose.run(project, ["build", "fleet-coordinator"], timeout_s=120.0)
            started = True
            compose.run(
                project,
                ["--profile", "fleet-qualification", "up", "--detach", "fleet-db", "fleet-redis", "fleet-coordinator", "fleet-fake-paper-provider", "fleet-fake-live-provider", "fleet-fake-market-data"],
                timeout_s=90.0,
            )
            started = True
            _wait_for_http("http://127.0.0.1:8100/health", timeout_s)
            compose.run(project, ["--profile", "fleet-qualification", "up", "--detach", "alpaca-paper-clerk", "alpaca-live-clerk"], timeout_s=60.0)
            _wait_for_http("http://127.0.0.1:8100/health", timeout_s)

            inspections = {service: compose.inspect(_container_id(compose, project, service)) for service in QUALIFICATION_SERVICES}
            mounts = {
                "coordinator": _mount_at(inspections["fleet-coordinator"], "/app/artifacts/fleet"),
                "paper": _mount_at(inspections["alpaca-paper-clerk"], "/app/artifacts/alpaca_clerk"),
                "live": _mount_at(inspections["alpaca-live-clerk"], "/app/artifacts/alpaca_clerk"),
            }
            sources = {role: mount.get("Source") for role, mount in mounts.items()}
            if len(set(sources.values())) != 3 or not all(isinstance(source, str) and source for source in sources.values()):
                raise QualificationError(f"Fleet volumes are not physically distinct: {sources!r}")
            _assert_coordinator_secret_absence(inspections["fleet-coordinator"])
            resources = {service: _assert_limits(inspections[service], service) for service in QUALIFICATION_SERVICES}
            for service in LANE_SERVICES:
                _assert_private_agent_ports(inspections[service], service)

            token = "qualification-control-secret"
            directory_headers = {"X-Data-Plane-Control-Secret": token}
            directory = _wait_for_http("http://127.0.0.1:8100/api/broker-clerks", timeout_s, directory_headers)
            clerks = directory.get("clerks")
            if not isinstance(clerks, list) or len(clerks) != 2:
                raise QualificationError("Actual coordinator did not project both enrolled clerk registrations.")
            live_clerk = next((entry for entry in clerks if entry.get("display_label") == "Qualification live"), None)
            if not isinstance(live_clerk, dict):
                raise QualificationError("Actual coordinator directory does not contain the Live qualification lane.")
            live_id = str(live_clerk["clerk_id"])
            mutation_status, mutation_refusal = _request_json(
                f"http://127.0.0.1:8100/api/brokers/alpaca/clerks/{live_id}/orders",
                method="POST",
                headers=directory_headers,
            )
            if mutation_status < HTTPStatus.BAD_REQUEST:
                raise QualificationError("Live mutation route unexpectedly accepted an unbound qualification lane.")
            compose.run(project, ["kill", "alpaca-paper-clerk"])
            compose.run(project, ["rm", "--force", "--stop", "alpaca-paper-clerk"])
            live_health = _wait_for_http("http://127.0.0.1:8100/api/broker-clerks", timeout_s, directory_headers)
            market_status, market_body = _exec_probe(compose, project, "alpaca-live-clerk", "http://fleet-fake-market-data:8013/health")
            provider_status, provider_body = _exec_probe(compose, project, "alpaca-live-clerk", "http://fleet-fake-live-provider:8012/health")
            if market_status != HTTPStatus.OK or provider_status != HTTPStatus.OK:
                raise QualificationError("Live container lost private market/provider egress after Paper failure.")
            coordinator_check = compose.run(project, ["exec", "-T", "fleet-coordinator", "python", "/app/scripts/run_broker_fleet_compose_qualification.py", "--assert-no-custody-root", "/app/artifacts/fleet"])
            evidence.update({"volume_sources": sources, "resources": resources, "directory_before_paper_fault": directory, "directory_after_paper_fault": live_health, "live_mutation_denial": mutation_refusal, "live_mutation_status": mutation_status, "live_market_egress": market_body, "live_provider_egress": provider_body, "coordinator_custody_check": json.loads(coordinator_check.stdout), "bounded_exclusions": ["Fake provider and market endpoints prove private egress only; this qualification does not emulate Alpaca account/profile binding, broker protocol calls, or a Live arming ceremony.", "The runtime currently exposes queue limits as deployment budgets; queue and stream saturation are separately bounded resource checks, not proof of provider stream semantics."], "result": "passed"})
            return evidence
        finally:
            if evidence_path is not None:
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if started and not keep:
                try:
                    bootstrap.run(project, ["--profile", "fleet-qualification", "down", "--volumes", "--remove-orphans"], timeout_s=60.0)
                except (QualificationError, subprocess.CalledProcessError) as exc:
                    logger.error("Scoped fleet qualification cleanup failed.", extra={"error": str(exc), "project": project})


def _assert_no_custody_root(root: Path) -> dict[str, object]:
    """Prove a coordinator volume has only its inert registry probe artifact."""
    names = sorted(path.name.lower() for path in root.iterdir())
    forbidden = [name for name in names if any(term in name for term in _CUSTODY_TERMS)]
    if forbidden:
        raise QualificationError(f"Coordinator control root contains lane custody material: {forbidden}")
    return {"ok": True, "entries": names}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-role", choices=("fake-provider", "fake-market-data"))
    parser.add_argument("--health-url")
    parser.add_argument("--call-url")
    parser.add_argument("--call-method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--assert-no-custody-root", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout-s", type=float, default=90.0)
    parser.add_argument("--evidence-path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Dispatch a container role helper or run the host qualification ceremony."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.container_role:
        return {"fake-provider": _run_fake_provider, "fake-market-data": _run_fake_market_data}[args.container_role]()
    if args.health_url:
        status, _body = _request_json(args.health_url)
        return 0 if status == HTTPStatus.OK else 1
    if args.call_url:
        status, body = _request_json(args.call_url, method=args.call_method)
        sys.stdout.write(json.dumps({"status": status, "body": body}, sort_keys=True) + "\n")
        return 0
    if args.assert_no_custody_root:
        sys.stdout.write(json.dumps(_assert_no_custody_root(args.assert_no_custody_root), sort_keys=True) + "\n")
        return 0
    try:
        evidence = run_host_qualification(keep=args.keep, timeout_s=args.timeout_s, evidence_path=args.evidence_path)
    except (QualificationError, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        logger.error("Fleet Compose qualification failed: %s", exc)
        return 1
    sys.stdout.write(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
