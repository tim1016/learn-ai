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
import concurrent.futures
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
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
_COORDINATOR_CONTROL_PATHS = frozenset(
    (
        "fleet",
        "fleet/.registry.db.lock",
        "fleet/registry.db",
        "fleet/registry.db-shm",
        "fleet/registry.db-wal",
    )
)
_FLEET_REGISTRY_TABLES = frozenset(
    (
        "fleet_meta",
        "clerks",
        "clerk_sessions",
        "clerk_session_history",
        "approved_endpoints",
        "account_assignments",
        "account_assignment_history",
        "routing_receipts",
    )
)
FAULT_SCENARIOS = (
    "provider_outage",
    "credential_refusal_restart",
    "volume_marker_poison_mismount_refusal",
    "request_queue_saturation",
    "stream_saturation",
)
FAULT_STATES = frozenset(("passed", "attempted", "unrun"))


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

    def with_environment(self, updates: dict[str, str]) -> ComposeCommand:
        """Return the same invocation while preserving required base interpolation."""
        return ComposeCommand(
            engine=self.engine,
            compose=self.compose,
            environment={**(self.environment or {}), **updates},
        )


def _request_json(
    url: str,
    *,
    method: str = "GET",
    timeout_s: float = 3.0,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call one test-only HTTP endpoint without inheriting proxy settings."""
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    request_headers = dict(headers or {})
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
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


def _json_handler(
    payload_for_path: dict[str, dict[str, Any] | Callable[[], dict[str, Any]]],
) -> type[BaseHTTPRequestHandler]:
    """Create an inert fake endpoint handler with no credential behaviour."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = payload_for_path.get(self.path)
            if payload is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if callable(payload):
                payload = payload()
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


#: The fake providers' account numbers (referenced by the recorded-history
#: ceremony steps too, so the Paper account_id used to drive the public
#: chart-history route can never drift from what the fake provider answers).
_PAPER_ACCOUNT_NUMBER = "PAQUALIFICATION"
_LIVE_ACCOUNT_NUMBER = "LQUALIFICATION"


def _run_fake_provider() -> int:
    provider = os.environ.get("FLEET_PROBE_PROVIDER", "unknown")
    port = 8011 if provider == "paper" else 8012
    account_number = _PAPER_ACCOUNT_NUMBER if provider == "paper" else _LIVE_ACCOUNT_NUMBER
    _serve_json(
        port,
        _json_handler(
            {
                "/health": {"ok": True},
                # Alpaca's real TradingClient reads this endpoint.  Keep this
                # payload valid for the production adapter, so qualification
                # proves the SDK/client/adapter path rather than bare egress.
                "/v2/account": {
                    "account_number": account_number,
                    "status": "ACTIVE",
                    "cash": "1000",
                    "equity": "1000",
                    "buying_power": "1000",
                    "portfolio_value": "1000",
                    "long_market_value": "0",
                    "short_market_value": "0",
                    "trading_blocked": False,
                    "account_blocked": False,
                },
            }
        ),
    )
    return 0


def _run_fake_market_data() -> int:
    def market_status_snapshot() -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        return {
            "source": "alpaca.stock_data.status",
            "connected": True,
            "observed_at_ms": now_ms,
            "connection_changed_at_ms": now_ms,
            "symbol_statuses": [],
        }

    _serve_json(
        8013,
        _json_handler(
            {
                "/health": {"ok": True},
                # This is the exact protected route read by the supported
                # Alpaca Paper market-status consumer, not a bespoke probe
                # endpoint.
                "/api/brokers/alpaca/market-status-snapshot": market_status_snapshot,
            }
        ),
    )
    return 0


def _qualification_local_call(path: str, *, method: str = "GET") -> tuple[int, dict[str, Any]]:
    """Call the already-running clerk ASGI process through its loopback socket."""
    secret = os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", "")
    if not secret:
        raise QualificationError("Qualification client has no per-run probe secret.")
    return _request_json(
        f"http://127.0.0.1:8000/internal/fleet-qualification{path}",
        method=method,
        headers={"X-Fleet-Qualification-Secret": secret},
        timeout_s=4.0,
    )


def _qualification_declared_read(path: str) -> tuple[int, dict[str, Any]]:
    """Call a normal declared Alpaca read through the running clerk ASGI app."""
    headers: dict[str, str] = {}
    control_secret = os.environ.get("DATA_PLANE_CONTROL_SECRET", "")
    if control_secret:
        headers["X-Data-Plane-Control-Secret"] = control_secret
    return _request_json(f"http://127.0.0.1:8000{path}", headers=headers, timeout_s=4.0)


def _qualification_local_hold(path: str, *, method: str = "GET") -> int:
    """Consume a held response whose body may be SSE rather than JSON."""
    secret = os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", "")
    if not secret:
        raise QualificationError("Qualification client has no per-run probe secret.")
    request = urllib.request.Request(
        f"http://127.0.0.1:8000/internal/fleet-qualification{path}",
        method=method,
        headers={"X-Fleet-Qualification-Secret": secret},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=4.0) as response:
        response.read()
        return response.status


def _run_dependency_client(kind: str) -> int:
    """Emit one real running-clerk client/feed result for host evidence."""
    if kind == "broker":
        status, body = _qualification_declared_read("/api/brokers/alpaca/account")
    else:
        refresh_status, refresh_body = _qualification_local_call("/dependency/market-data")
        if refresh_status != HTTPStatus.OK:
            status, body = refresh_status, refresh_body
        else:
            status, body = _qualification_declared_read(
                "/api/brokers/alpaca/market-status-snapshot"
            )
    sys.stdout.write(json.dumps({"status": status, "body": body}, sort_keys=True) + "\n")
    return 0


#: ``/hold/request`` and ``/hold/stream`` are ``GET`` (the request/stream
#: pools); ``/hold/command`` (issue #2206) must be ``POST`` on a path other
#: than the internal history-batch route so the deployed
#: ``FleetLaneRuntimeMiddleware`` classifies it into the command pool
#: (``_requires_command_capacity``) instead.
_HOLD_METHODS: dict[str, str] = {"request": "GET", "stream": "GET", "command": "POST"}


def _run_capacity_client(kind: str) -> int:
    """Contend against the deployed Paper ASGI middleware, never a fresh instance."""
    path = f"/hold/{kind}"
    method = _HOLD_METHODS[kind]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_qualification_local_hold, path, method=method)
        time.sleep(0.15)
        second = _qualification_local_call(path, method=method)
        first_status = first.result(timeout=4.0)
    if first_status != HTTPStatus.OK:
        raise QualificationError(f"Running Paper {kind} hold did not enter the service.")
    status, body = second
    if status != HTTPStatus.SERVICE_UNAVAILABLE or body.get("reason") != "fleet_lane_capacity_exhausted":
        raise QualificationError(f"Running Paper {kind} capacity did not return its typed 503.")
    sys.stdout.write(json.dumps({"kind": kind, "refusal_status": status, "reason": body["reason"]}, sort_keys=True) + "\n")
    return 0


def _container_id(compose: ComposeCommand, project: str, service: str) -> str:
    result = compose.run(project, ["ps", "--all", "-q", service])
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


def _assert_coordinator_mount_isolation(inspect: dict[str, Any], lane_sources: set[str]) -> None:
    """Reject every coordinator mount that could be a lane-local custody root."""
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise QualificationError("Coordinator inspection has no mount list.")
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        source = mount.get("Source")
        destination = mount.get("Destination")
        if source in lane_sources or (isinstance(destination, str) and "alpaca_clerk" in destination):
            raise QualificationError("Coordinator exposes a lane source or custody destination.")


def _exec_probe(
    compose: ComposeCommand,
    project: str,
    service: str,
    path: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    with_qualification_secret: bool = False,
) -> tuple[int, dict[str, Any]]:
    target = path if path.startswith("http://") or path.startswith("https://") else f"http://localhost:8000{path}"
    args = [
        "exec",
        "-T",
        service,
        "python",
        "/app/scripts/run_broker_fleet_compose_qualification.py",
        "--call-url",
        target,
        "--call-method",
        method,
    ]
    if json_body is not None:
        args.extend(["--call-body", json.dumps(json_body)])
    if with_qualification_secret:
        args.append("--call-with-qualification-secret")
    result = compose.run(project, args)
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
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as environment_file:
        for key, value in values.items():
            environment_file.write(f"{key}={value}\n")
        environment_file.flush()
        os.fsync(environment_file.fileno())


def _available_loopback_port() -> int:
    """Reserve a currently free loopback port for the scoped Compose project."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _qualification_runtime_environment(
    *, coordinator_env: Path, deployment_namespace: str
) -> dict[str, str]:
    """Pin every qualification role to the namespace used during enrollment."""
    return {
        "FLEET_COORDINATOR_ENV_FILE": str(coordinator_env),
        "FLEET_DEPLOYMENT_NAMESPACE": deployment_namespace,
    }


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
    probe_secret = f"qualification-probe-{uuid.uuid4().hex}"
    coordinator_env = temporary_dir / "coordinator.env"
    _write_env(
        coordinator_env,
        {
            "FLEET_POSTGRES_PASSWORD": "qualification-postgres-password",
            "DATA_PLANE_CONTROL_SECRET": control_secret,
            "POSTGRES_URL": "postgresql://postgres:qualification-postgres-password@fleet-db:5432/postgres",
            "REDIS_URL": "redis://fleet-redis:6379/0",
            "FLEET_AGENT_SERVICE_TOKENS_JSON": json.dumps(
                {entry["clerk_id"]: entry["agent_service_token"] for entry in lanes.values()}, separators=(",", ":")
            ),
            "FLEET_COORDINATOR_SERVICE_TOKENS_JSON": json.dumps(
                {entry["clerk_id"]: entry["coordinator_service_token"] for entry in lanes.values()}, separators=(",", ":")
            ),
            # Issue #2206: the coordinator's own recorded-history seam is
            # gated by this same per-run secret, mirroring how each lane
            # below already carries it for the clerk-side qualification hooks.
            "FLEET_QUALIFICATION_PROBE_SECRET": probe_secret,
        },
    )
    runtime_environment = _qualification_runtime_environment(
        coordinator_env=coordinator_env,
        deployment_namespace=deployment_namespace,
    )
    # Carried on the returned ``ComposeCommand`` purely so
    # ``run_host_qualification`` can read it back for its own direct
    # (non-``compose exec``) calls to the coordinator's qualification-only
    # surface -- no compose file references this variable.
    runtime_environment["FLEET_QUALIFICATION_PROBE_SECRET"] = probe_secret
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
                "IBKR_CLIENT_ID": "1201" if lane == "paper" else "1202",
                "FLEET_QUALIFICATION_PROBE_SECRET": probe_secret,
            },
        )
        runtime_environment[f"FLEET_{lane.upper()}_ENV_FILE"] = str(lane_env)
    return compose.with_environment(runtime_environment)


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


def _live_identity_after_paper_fault(
    compose: ComposeCommand,
    project: str,
    timeout_s: float,
    headers: dict[str, str],
    live_id: str,
    coordinator_url: str,
) -> dict[str, Any]:
    """Prove the coordinator still projects the enrolled Live identity."""
    directory = _wait_for_http(f"{coordinator_url}/api/broker-clerks", timeout_s, headers)
    clerks = directory.get("clerks")
    if not isinstance(clerks, list):
        raise QualificationError("Coordinator directory lost its clerk projection after Paper fault.")
    live = next((entry for entry in clerks if isinstance(entry, dict) and str(entry.get("clerk_id")) == live_id), None)
    if not isinstance(live, dict):
        raise QualificationError("Live identity disappeared after an isolated Paper fault.")
    return {"clerk_id": live_id, "display_label": live.get("display_label"), "directory_size": len(clerks)}


def _fault_result(state: str, detail: str, live: dict[str, Any]) -> dict[str, Any]:
    """Return a machine-readable bounded-fault result without implying a pass."""
    if state not in FAULT_STATES:
        raise QualificationError(f"Unknown fault evidence state {state!r}.")
    return {"state": state, "detail": detail, "live_after_fault": live}


def _assert_all_faults_passed(faults: dict[str, dict[str, Any]]) -> None:
    """Prevent a partial/attempted fault matrix from being labelled passed."""
    missing = sorted(set(FAULT_SCENARIOS) - faults.keys())
    incomplete = sorted(name for name, result in faults.items() if result.get("state") != "passed")
    if missing or incomplete:
        raise QualificationError(
            f"Fleet fault matrix is incomplete; missing={missing!r}, nonpassing={incomplete!r}."
        )


def _capacity_probe(compose: ComposeCommand, project: str, kind: str) -> dict[str, Any]:
    """Run contention through the deployed, already-running Paper ASGI service."""
    result = compose.run(
        project,
        [
            "exec",
            "-T",
            "alpaca-paper-clerk",
            "python",
            "-m",
            "scripts.run_broker_fleet_compose_qualification",
            "--container-role",
            "capacity-client",
            "--capacity-kind",
            kind,
        ],
        timeout_s=20.0,
    )
    payload = _json_line(result, f"Paper {kind} capacity probe")
    if payload.get("refusal_status") != HTTPStatus.SERVICE_UNAVAILABLE:
        raise QualificationError(f"Paper {kind} capacity probe did not emit a typed 503.")
    if payload.get("reason") != "fleet_lane_capacity_exhausted":
        raise QualificationError(f"Paper {kind} capacity probe emitted the wrong refusal reason.")
    return payload


def _dependency_probe(
    compose: ComposeCommand, project: str, service: str, kind: str
) -> tuple[int, dict[str, Any]]:
    """Call a qualification dependency through the real named clerk ASGI process."""
    result = compose.run(
        project,
        ["exec", "-T", service, "python", "-m", "scripts.run_broker_fleet_compose_qualification", "--container-role", "dependency-client", "--dependency-kind", kind],
    )
    payload = _json_line(result, f"{service} dependency probe")
    status = payload.get("status")
    body = payload.get("body")
    if not isinstance(status, int) or not isinstance(body, dict):
        raise QualificationError(f"{service} dependency probe returned malformed evidence.")
    return status, body


#: Issue #2206's recorded-history ceremony identities. An arbitrary but fixed
#: strategy-instance id and symbol -- the recorded provider is deterministic
#: given ``(symbol, bar_start_ms)``, so any symbol works; a fixed one keeps
#: the seed idempotent-in-spirit across a single ceremony run.
_RECORDED_HISTORY_SID = "qualification-recorded-history-bot"
_RECORDED_HISTORY_SYMBOL = "QUALHIST"
_RECORDED_HISTORY_TIMEFRAME = "1d"


def _history_chart_url(coordinator_url: str, *, clerk_id: str, account_id: str, sid: str, timeframe: str) -> str:
    """The coordinator's public catalog route for one bot's chart history.

    ``/api/brokers/{broker}/clerks/{clerk_id}/accounts/{account_id}/bots/{sid}/chart/history``
    -- the exact ``bot_chart_history`` ``ProviderOperation`` path template
    (``app.broker.alpaca.clerk.fleet_adapter``) prefixed by
    ``broker_clerks.py``'s ``_CLERK_SCOPE``. Calling this (not the internal
    ``/internal/fleet/history/batch`` route) is what makes the ceremony a
    genuine browser -> coordinator -> Clerk -> coordinator round trip
    (issue #2206 acceptance criterion 1): the coordinator forwards to the
    Clerk, whose own history assembly calls back into the coordinator's
    internal history-batch operation to get bars.
    """
    return (
        f"{coordinator_url}/api/brokers/alpaca/clerks/{clerk_id}/accounts/{account_id}"
        f"/bots/{sid}/chart/history?timeframe={timeframe}"
    )


def _set_recorded_history_mode(coordinator_url: str, qualification_secret: str, mode: str) -> None:
    """Arm the coordinator's recorded-history provider (issue #2206)."""
    status, body = _request_json(
        f"{coordinator_url}/internal/fleet-qualification-history/mode",
        method="POST",
        headers={"X-Fleet-Qualification-Secret": qualification_secret},
        json_body={"mode": mode},
    )
    if status != HTTPStatus.OK or body.get("mode") != mode:
        raise QualificationError(f"Coordinator refused recorded-history mode {mode!r}: {status} {body!r}")


def _recorded_history_ceremony(
    compose: ComposeCommand,
    project: str,
    *,
    coordinator_url: str,
    control_headers: dict[str, str],
    qualification_secret: str,
    paper_clerk_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Prove issue #2206's recorded-history provider end to end.

    Drives the Clerk's real public chart-history route (never the internal
    operation directly) through five steps: seed one minimal strategy
    instance, a healthy read, an injected-unavailable read, a recovered
    read after an explicit retry, and a slow read held in flight long
    enough to prove a concurrent command is still admitted through its own
    pool (issue #2204's split) rather than starved behind the occupied
    request pool.
    """
    seed_status, seed_body = _exec_probe(
        compose,
        project,
        "alpaca-paper-clerk",
        "/internal/fleet-qualification/seed-bot",
        method="POST",
        json_body={"strategy_instance_id": _RECORDED_HISTORY_SID, "symbol": _RECORDED_HISTORY_SYMBOL},
        with_qualification_secret=True,
    )
    if seed_status != HTTPStatus.OK or seed_body.get("seeded") != "strategy_instance":
        raise QualificationError(f"Paper did not seed its qualification chart-history bot: {seed_status} {seed_body!r}")

    url = _history_chart_url(
        coordinator_url,
        clerk_id=paper_clerk_id,
        account_id=_PAPER_ACCOUNT_NUMBER,
        sid=_RECORDED_HISTORY_SID,
        timeframe=_RECORDED_HISTORY_TIMEFRAME,
    )

    _set_recorded_history_mode(coordinator_url, qualification_secret, "healthy")
    healthy_status, healthy_body = _request_json(url, headers=control_headers, timeout_s=timeout_s)
    if healthy_status != HTTPStatus.OK or not healthy_body.get("bars"):
        raise QualificationError(f"Healthy recorded-history request returned no candles: {healthy_status} {healthy_body!r}")
    if any(bar.get("source") != "polygon" for bar in healthy_body["bars"]):
        raise QualificationError("Healthy recorded-history bars were not all source='polygon'.")

    _set_recorded_history_mode(coordinator_url, qualification_secret, "unavailable")
    failed_status, failed_body = _request_json(url, headers=control_headers, timeout_s=timeout_s)
    notices = failed_body.get("overlay_notices") or []
    if failed_status != HTTPStatus.OK or failed_body.get("bars"):
        raise QualificationError(f"Injected-unavailable request unexpectedly returned bars: {failed_status} {failed_body!r}")
    if not any(notice.get("code") == "coordinator_unavailable" for notice in notices):
        raise QualificationError(f"Injected-unavailable request did not settle on coordinator_unavailable: {notices!r}")

    _set_recorded_history_mode(coordinator_url, qualification_secret, "healthy")
    recovered_status, recovered_body = _request_json(url, headers=control_headers, timeout_s=timeout_s)
    if recovered_status != HTTPStatus.OK or not recovered_body.get("bars"):
        raise QualificationError(f"Explicit retry after recovery returned no candles: {recovered_status} {recovered_body!r}")

    _set_recorded_history_mode(coordinator_url, qualification_secret, "slow")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        slow_call = executor.submit(_request_json, url, headers=control_headers, timeout_s=timeout_s)
        time.sleep(0.3)
        command_status, command_body = _exec_probe(
            compose,
            project,
            "alpaca-paper-clerk",
            "/internal/fleet-qualification/hold/command",
            method="POST",
            with_qualification_secret=True,
        )
        slow_status, slow_body = slow_call.result(timeout=timeout_s)
    _set_recorded_history_mode(coordinator_url, qualification_secret, "healthy")
    if command_status != HTTPStatus.OK or command_body.get("held") != "command":
        raise QualificationError(
            f"A command was not admitted while a history read was held in flight: {command_status} {command_body!r}"
        )
    if slow_status != HTTPStatus.OK or not slow_body.get("bars"):
        raise QualificationError(f"The held slow history read did not itself complete: {slow_status} {slow_body!r}")

    return {
        "healthy_bar_count": len(healthy_body["bars"]),
        "injected_unavailable_notice_codes": [notice.get("code") for notice in notices],
        "recovered_bar_count": len(recovered_body["bars"]),
        "command_admitted_while_history_read_in_flight": True,
        "held_history_read_bar_count": len(slow_body["bars"]),
    }


def _wait_for_paper_refusal(compose: ComposeCommand, project: str, timeout_s: float) -> dict[str, object]:
    """Require the poisoned real Paper process to exit before recording refusal."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        container_id = _container_id(compose, project, "alpaca-paper-clerk")
        state = compose.inspect(container_id).get("State")
        if isinstance(state, dict) and state.get("Running") is False:
            exit_code = state.get("ExitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                logs = compose.run(project, ["logs", "--tail", "30", "alpaca-paper-clerk"], check=False)
                refusal_text = "marker" in (logs.stdout + logs.stderr).lower()
                if not refusal_text:
                    raise QualificationError(
                        "Poisoned Paper process exited without a volume-marker refusal in its bounded logs."
                    )
                return {"exit_code": exit_code, "marker_refusal_text": refusal_text}
            raise QualificationError("Poisoned Paper process exited successfully instead of refusing its volume.")
        time.sleep(1)
    raise QualificationError("Poisoned Paper process did not exit within its bounded startup window.")


def _cleanup_qualification(
    compose: ComposeCommand, project: str, evidence: dict[str, Any]
) -> QualificationError | None:
    """Tear down one scoped project and make cleanup failure qualification-fatal.

    A successful fault matrix is not a successful qualification if its
    supposedly isolated containers or volumes could not be removed.  Preserve
    that failure in the durable evidence before returning it to the caller.
    """
    try:
        compose.run(
            project,
            ["--profile", "fleet-qualification", "down", "--volumes", "--remove-orphans"],
            timeout_s=60.0,
        )
    except (QualificationError, subprocess.CalledProcessError) as exc:
        logger.error("Scoped fleet qualification cleanup failed.", extra={"error": str(exc), "project": project})
        cleanup_failure = {"stage": "cleanup", "error_type": type(exc).__name__}
        if evidence.get("result") == "passed":
            evidence.update({"result": "failed", "failure": cleanup_failure})
        else:
            # Preserve the original bounded-stage failure while retaining the
            # teardown problem as a second, durable operator fact.
            evidence["cleanup_failure"] = cleanup_failure
        return QualificationError(
            "Scoped fleet qualification cleanup failed; its evidence is not a passing qualification."
        )
    return None


def run_host_qualification(
    *, keep: bool, timeout_s: float, evidence_path: Path | None, build_timeout_s: float = 120.0
) -> dict[str, Any]:
    """Exercise actual coordinator/agent roles on isolated Compose volumes."""
    bootstrap_discovered = ComposeCommand.discover()
    coordinator_port = _available_loopback_port()
    bootstrap = ComposeCommand(
        engine=bootstrap_discovered.engine,
        compose=bootstrap_discovered.compose,
        environment={
            "POSTGRES_PASSWORD": "qualification-postgres-password",
            "REDIS_PASSWORD": "qualification-redis-password",
            "POLYGON_API_KEY": "qualification-polygon-placeholder",
            "FLEET_COORDINATOR_PORT": str(coordinator_port),
        },
    )
    project = f"fleetqualification{uuid.uuid4().hex[:12]}"
    evidence: dict[str, Any] = {"project": project, "compose_files": [str(path) for path in COMPOSE_FILES]}
    started = False
    completed = False
    with tempfile.TemporaryDirectory(prefix="learn-ai-fleet-qualification-") as temporary:
        try:
            evidence["stage"] = "render"
            bootstrap.run(project, ["--profile", "fleet-qualification", "config"])
            started = True
            evidence["stage"] = "build"
            bootstrap.run(project, ["build", "fleet-coordinator"], timeout_s=build_timeout_s)
            started = True
            evidence["stage"] = "enrollment"
            compose = _host_ceremony(bootstrap, project, Path(temporary))
            evidence["stage"] = "support_startup"
            compose.run(
                project,
                ["--profile", "fleet-qualification", "up", "--detach", "fleet-db", "fleet-redis", "fleet-coordinator", "fleet-fake-paper-provider", "fleet-fake-live-provider", "fleet-fake-market-data"],
                timeout_s=90.0,
            )
            started = True
            coordinator_url = f"http://127.0.0.1:{coordinator_port}"
            _wait_for_http(f"{coordinator_url}/health", timeout_s)
            evidence["stage"] = "lane_startup"
            compose.run(project, ["--profile", "fleet-qualification", "up", "--detach", "alpaca-paper-clerk", "alpaca-live-clerk"], timeout_s=60.0)
            _wait_for_lanes(compose, project, timeout_s)

            evidence["stage"] = "topology_inspection"
            inspections = {service: compose.inspect(_container_id(compose, project, service)) for service in QUALIFICATION_SERVICES}
            mounts = {
                "coordinator": _mount_at(inspections["fleet-coordinator"], "/app/artifacts/fleet"),
                "paper": _mount_at(inspections["alpaca-paper-clerk"], "/app/artifacts/alpaca_clerk"),
                "live": _mount_at(inspections["alpaca-live-clerk"], "/app/artifacts/alpaca_clerk"),
            }
            sources = {role: mount.get("Source") for role, mount in mounts.items()}
            if len(set(sources.values())) != 3 or not all(isinstance(source, str) and source for source in sources.values()):
                raise QualificationError(f"Fleet volumes are not physically distinct: {sources!r}")
            _assert_coordinator_mount_isolation(
                inspections["fleet-coordinator"], {str(sources["paper"]), str(sources["live"])}
            )
            _assert_coordinator_secret_absence(inspections["fleet-coordinator"])
            resources = {service: _assert_limits(inspections[service], service) for service in QUALIFICATION_SERVICES}
            for service in LANE_SERVICES:
                _assert_private_agent_ports(inspections[service], service)

            token = "qualification-control-secret"
            directory_headers = {"X-Data-Plane-Control-Secret": token}
            directory = _wait_for_http(f"{coordinator_url}/api/broker-clerks", timeout_s, directory_headers)
            clerks = directory.get("clerks")
            if not isinstance(clerks, list) or len(clerks) != 2:
                raise QualificationError("Actual coordinator did not project both enrolled clerk registrations.")
            live_clerk = next((entry for entry in clerks if entry.get("display_label") == "Qualification live"), None)
            if not isinstance(live_clerk, dict):
                raise QualificationError("Actual coordinator directory does not contain the Live qualification lane.")
            live_id = str(live_clerk["clerk_id"])
            paper_clerk = next((entry for entry in clerks if entry.get("display_label") == "Qualification paper"), None)
            if not isinstance(paper_clerk, dict):
                raise QualificationError("Actual coordinator directory does not contain the Paper qualification lane.")
            paper_id = str(paper_clerk["clerk_id"])
            mutation_status, mutation_refusal = _request_json(
                f"{coordinator_url}/api/brokers/alpaca/clerks/{live_id}/orders",
                method="POST",
                headers=directory_headers,
            )
            if mutation_status < HTTPStatus.BAD_REQUEST:
                raise QualificationError("Live mutation route unexpectedly accepted an unbound qualification lane.")

            faults: dict[str, dict[str, Any]] = {}
            evidence["stage"] = "provider_outage"
            compose.run(project, ["kill", "fleet-fake-paper-provider"])
            paper_dependency_status, paper_dependency = _dependency_probe(compose, project, "alpaca-paper-clerk", "broker")
            live_dependency_status, live_dependency = _dependency_probe(compose, project, "alpaca-live-clerk", "broker")
            paper_market_status, paper_market = _dependency_probe(compose, project, "alpaca-paper-clerk", "market-data")
            if paper_dependency_status != HTTPStatus.SERVICE_UNAVAILABLE:
                raise QualificationError("Running Paper ASGI route did not refuse its killed qualification upstream.")
            if live_dependency_status != HTTPStatus.OK:
                raise QualificationError("Running Live ASGI route lost its independent qualification upstream.")
            if paper_market_status != HTTPStatus.OK:
                raise QualificationError("Running Paper ASGI market-status consumer did not remain read-only available.")
            faults["provider_outage"] = _fault_result(
                "passed",
                "Paper's declared Alpaca account read refused its killed upstream while Live's independent declared account read still succeeded.",
                _live_identity_after_paper_fault(
                    compose, project, timeout_s, directory_headers, live_id, coordinator_url
                ),
            )
            faults["provider_outage"]["paper_dependency"] = paper_dependency
            faults["provider_outage"]["live_dependency"] = live_dependency
            faults["provider_outage"]["paper_market_status_consumer"] = paper_market
            # Restore only the Paper test dependency before subsequent Paper
            # restart faults; Live remained independently available throughout.
            compose.run(project, ["--profile", "fleet-qualification", "up", "--detach", "fleet-fake-paper-provider"], timeout_s=30.0)

            evidence["stage"] = "credential_refusal_restart"
            compose.run(project, ["kill", "alpaca-paper-clerk"])
            compose.run(project, ["rm", "--force", "--stop", "alpaca-paper-clerk"])
            credential = compose.run(
                project,
                ["run", "--rm", "--no-deps", "-e", "FLEET_AGENT_SERVICE_TOKEN=qualification-refused-token", "alpaca-paper-clerk"],
                check=False,
                timeout_s=15.0,
            )
            if credential.returncode == 0:
                raise QualificationError("Real Paper restart accepted a refused agent token.")
            faults["credential_refusal_restart"] = _fault_result(
                "passed",
                f"Real Paper restart with a refused agent token returned {credential.returncode}.",
                _live_identity_after_paper_fault(
                    compose, project, timeout_s, directory_headers, live_id, coordinator_url
                ),
            )
            compose.run(project, ["--profile", "fleet-qualification", "up", "--detach", "--no-deps", "alpaca-paper-clerk"], timeout_s=30.0)
            _wait_for_lanes(compose, project, timeout_s)

            evidence["stage"] = "request_queue_saturation"
            request_probe = _capacity_probe(compose, project, "request")
            faults["request_queue_saturation"] = _fault_result(
                "passed",
                "Held ASGI request consumed Paper's only request slot and a second request received the middleware's typed 503.",
                _live_identity_after_paper_fault(
                    compose, project, timeout_s, directory_headers, live_id, coordinator_url
                ),
            )
            faults["request_queue_saturation"]["probe"] = request_probe
            evidence["stage"] = "stream_saturation"
            stream_probe = _capacity_probe(compose, project, "stream")
            faults["stream_saturation"] = _fault_result(
                "passed",
                "Held Paper SSE consumed the stream pool and a second stream received the middleware's typed 503.",
                _live_identity_after_paper_fault(
                    compose, project, timeout_s, directory_headers, live_id, coordinator_url
                ),
            )
            faults["stream_saturation"]["probe"] = stream_probe

            evidence["stage"] = "recorded_history"
            qualification_secret = str(compose.environment.get("FLEET_QUALIFICATION_PROBE_SECRET", "")) if compose.environment else ""
            if not qualification_secret:
                raise QualificationError("Compose environment lost its qualification probe secret before the recorded-history stage.")
            evidence["recorded_history"] = _recorded_history_ceremony(
                compose,
                project,
                coordinator_url=coordinator_url,
                control_headers=directory_headers,
                qualification_secret=qualification_secret,
                paper_clerk_id=paper_id,
                timeout_s=timeout_s,
            )

            evidence["stage"] = "volume_marker_poison_mismount_refusal"
            compose.run(project, ["kill", "alpaca-paper-clerk"])
            compose.run(project, ["rm", "--force", "--stop", "alpaca-paper-clerk"])
            compose.run(project, ["run", "--rm", "--no-deps", "fleet-qualification-enroller", "sh", "-ec", "printf '%s\\n' '{not-valid-json' > /app/artifacts/clerks/paper/.learn-ai-clerk-volume.json"])
            poison = compose.run(
                project,
                ["--profile", "fleet-qualification", "up", "--detach", "--no-deps", "alpaca-paper-clerk"],
                check=False,
                timeout_s=20.0,
            )
            poisoned = _wait_for_paper_refusal(compose, project, min(timeout_s, 20.0))
            faults["volume_marker_poison_mismount_refusal"] = _fault_result(
                "passed",
                f"Paper volume marker was poisoned before real role restart; compose up returned {poison.returncode} and the process exited nonzero.",
                _live_identity_after_paper_fault(
                    compose, project, timeout_s, directory_headers, live_id, coordinator_url
                ),
            )
            faults["volume_marker_poison_mismount_refusal"]["probe"] = poisoned
            live_health = _wait_for_http(f"{coordinator_url}/api/broker-clerks", timeout_s, directory_headers)
            live_account_status, live_account = _dependency_probe(
                compose, project, "alpaca-live-clerk", "broker"
            )
            if live_account_status != HTTPStatus.OK:
                raise QualificationError("Live declared Alpaca account read failed after Paper failure.")
            coordinator_check = compose.run(project, ["exec", "-T", "fleet-coordinator", "python", "/app/scripts/run_broker_fleet_compose_qualification.py", "--assert-no-custody-root", "/app/artifacts/fleet"])
            _assert_all_faults_passed(faults)
            evidence.update({"stage": "complete", "volume_sources": sources, "resources": resources, "directory_before_paper_fault": directory, "directory_after_paper_fault": live_health, "live_mutation_denial": mutation_refusal, "live_mutation_status": mutation_status, "live_account_read_after_paper_failure": live_account, "coordinator_custody_check": json.loads(coordinator_check.stdout), "paper_fault_matrix": faults, "bounded_exclusions": ["Qualification drives Alpaca's SDK URL-override seam and the supported Paper market-status consumer against fake endpoints; it does not bind a real account/profile or run a Live arming ceremony.", "The held-ASGI request and SSE probes prove middleware capacity admission, not an external provider stream protocol.", "Coordinator restart/outage qualification is Delivery E."], "result": "passed"})
            completed = True
            return evidence
        except (QualificationError, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
            evidence.update(
                {
                    "result": "failed",
                    "failure": {
                        "stage": evidence.get("stage", "unknown"),
                        "error_type": type(exc).__name__,
                    },
                }
            )
            raise
        finally:
            cleanup_error: QualificationError | None = None
            if started and not keep:
                cleanup_error = _cleanup_qualification(bootstrap, project, evidence)
            if evidence_path is not None:
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if cleanup_error is not None and completed:
                raise cleanup_error


def _assert_no_custody_root(root: Path) -> dict[str, object]:
    """Require the coordinator control root to contain only the fleet registry."""
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    unexpected = sorted(actual - _COORDINATOR_CONTROL_PATHS)
    if unexpected:
        raise QualificationError(f"Coordinator control root contains unapproved material: {unexpected}")
    database = root / "fleet" / "registry.db"
    if not database.is_file():
        raise QualificationError("Coordinator control root is missing its fleet registry database.")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()
    if tables != _FLEET_REGISTRY_TABLES:
        missing = sorted(_FLEET_REGISTRY_TABLES - tables)
        unknown = sorted(tables - _FLEET_REGISTRY_TABLES)
        raise QualificationError(
            f"Fleet registry table set is not exact; missing={missing!r}, unknown={unknown!r}."
        )
    return {"ok": True, "entries": sorted(actual), "tables": sorted(tables)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-role", choices=("fake-provider", "fake-market-data", "capacity-client", "dependency-client"))
    parser.add_argument("--capacity-kind", choices=("request", "stream", "command"))
    parser.add_argument("--dependency-kind", choices=("broker", "market-data"))
    parser.add_argument("--health-url")
    parser.add_argument("--call-url")
    parser.add_argument("--call-method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--call-body", help="A JSON object sent as the request body.")
    parser.add_argument(
        "--call-with-qualification-secret",
        action="store_true",
        help="Attach X-Fleet-Qualification-Secret from FLEET_QUALIFICATION_PROBE_SECRET.",
    )
    parser.add_argument("--assert-no-custody-root", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout-s", type=float, default=90.0)
    parser.add_argument("--build-timeout-s", type=float, default=120.0)
    parser.add_argument("--evidence-path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Dispatch a container role helper or run the host qualification ceremony."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.container_role:
        if args.container_role == "capacity-client":
            if args.capacity_kind is None:
                raise QualificationError("--capacity-kind is required for capacity-client.")
            return _run_capacity_client(args.capacity_kind)
        if args.container_role == "dependency-client":
            if args.dependency_kind is None:
                raise QualificationError("--dependency-kind is required for dependency-client.")
            return _run_dependency_client(args.dependency_kind)
        return {"fake-provider": _run_fake_provider, "fake-market-data": _run_fake_market_data}[args.container_role]()
    if args.health_url:
        status, _body = _request_json(args.health_url)
        return 0 if status == HTTPStatus.OK else 1
    if args.call_url:
        headers: dict[str, str] = {}
        if args.call_with_qualification_secret:
            secret = os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", "")
            if not secret:
                raise QualificationError("This container has no qualification probe secret.")
            headers["X-Fleet-Qualification-Secret"] = secret
        json_body = json.loads(args.call_body) if args.call_body else None
        status, body = _request_json(
            args.call_url, method=args.call_method, headers=headers, json_body=json_body
        )
        sys.stdout.write(json.dumps({"status": status, "body": body}, sort_keys=True) + "\n")
        return 0
    if args.assert_no_custody_root:
        sys.stdout.write(json.dumps(_assert_no_custody_root(args.assert_no_custody_root), sort_keys=True) + "\n")
        return 0
    try:
        evidence = run_host_qualification(
            keep=args.keep,
            timeout_s=args.timeout_s,
            evidence_path=args.evidence_path,
            build_timeout_s=args.build_timeout_s,
        )
    except (QualificationError, subprocess.CalledProcessError, json.JSONDecodeError, OSError) as exc:
        logger.error("Fleet Compose qualification failed.", extra={"error": str(exc)})
        return 1
    sys.stdout.write(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
