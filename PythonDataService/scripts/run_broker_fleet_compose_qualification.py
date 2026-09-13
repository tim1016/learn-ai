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
import sqlite3
import subprocess
import sys
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
        self, project: str, args: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Invoke Compose against only this repository's two fleet files."""
        command = [*self.compose, "--project-name", project]
        for compose_file in COMPOSE_FILES:
            command.extend(("--file", str(compose_file)))
        command.extend(args)
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=check,
            capture_output=True,
            text=True,
        )

    def inspect(self, container_id: str) -> dict[str, Any]:
        """Return one engine inspection document for a known project container."""
        result = subprocess.run(
            [self.engine, "inspect", container_id],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise QualificationError(f"Unexpected {self.engine} inspect payload for {container_id!r}.")
        return payload[0]


def _request_json(url: str, *, method: str = "GET", timeout_s: float = 3.0) -> tuple[int, dict[str, Any]]:
    """Call one test-only HTTP endpoint without inheriting proxy settings."""
    request = urllib.request.Request(url, method=method)
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


def _run_coordinator_probe() -> int:
    root = Path(os.environ.get("FLEET_CONTROL_DIR", "/app/artifacts/fleet"))
    root.mkdir(parents=True, exist_ok=True)
    database = root / "fleet-registry.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS fleet_registry_probe (id INTEGER PRIMARY KEY)")
    _serve_json(8000, _json_handler({"/health": {"ok": True, "role": "fleet_coordinator"}}))
    return 0


def _run_lane_probe() -> int:
    lane = os.environ.get("FLEET_PROBE_LANE")
    if lane not in {"paper", "live"}:
        raise QualificationError("FLEET_PROBE_LANE must be paper or live for a lane probe.")
    root = Path(os.environ.get("ALPACA_CLERK_DIR", "/app/artifacts/alpaca_clerk"))
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("live_runs", "live_bars", "broker_captures"):
        (root / relative).mkdir(exist_ok=True)
    marker = {
        "deployment_namespace": os.environ.get("FLEET_DEPLOYMENT_NAMESPACE"),
        "lane": lane,
        "root": str(root),
    }
    (root / "fleet-lane-probe.json").write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
    with sqlite3.connect(root / "custody-probe.sqlite") as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS lane_probe (lane TEXT NOT NULL)")
        connection.execute("INSERT INTO lane_probe(lane) VALUES (?)", (lane,))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                payload = {"ok": True, "lane": lane}
            elif self.path == "/read":
                provider_status, provider = _request_json(os.environ["FLEET_FAKE_BROKER_URL"] + "/read")
                market_status, market = _request_json(os.environ["FLEET_FAKE_MARKET_DATA_URL"] + "/read")
                if provider_status != HTTPStatus.OK or market_status != HTTPStatus.OK:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                payload = {"lane": lane, "marker": marker, "provider": provider, "market": market}
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/mutate":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = json.dumps({"reason": "live_mutation_disabled"}, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    _serve_json(8000, Handler)
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
    result = compose.run(
        project,
        [
            "exec",
            "-T",
            service,
            "python",
            "/app/scripts/run_broker_fleet_compose_qualification.py",
            "--call-url",
            f"http://localhost:8000{path}",
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


def run_host_qualification(*, keep: bool, timeout_s: float, evidence_path: Path | None) -> dict[str, Any]:
    """Build a scoped Compose project, assert isolation, then recoverably clean it."""
    compose = ComposeCommand.discover()
    project = f"fleetqualification{uuid.uuid4().hex[:12]}"
    evidence: dict[str, Any] = {"project": project, "compose_files": [str(path) for path in COMPOSE_FILES]}
    started = False
    try:
        compose.run(project, ["--profile", "fleet-qualification", "config"])
        compose.run(project, ["--profile", "fleet-qualification", "up", "--build", "--detach"])
        started = True
        _wait_for_lanes(compose, project, timeout_s)

        inspections = {
            service: compose.inspect(_container_id(compose, project, service))
            for service in QUALIFICATION_SERVICES
        }
        coordinator_mount = _mount_at(inspections["fleet-coordinator"], "/app/artifacts/fleet")
        paper_mount = _mount_at(inspections["alpaca-paper-clerk"], "/app/artifacts/alpaca_clerk")
        live_mount = _mount_at(inspections["alpaca-live-clerk"], "/app/artifacts/alpaca_clerk")
        sources = {"coordinator": coordinator_mount.get("Source"), "paper": paper_mount.get("Source"), "live": live_mount.get("Source")}
        if len(set(sources.values())) != 3 or not all(isinstance(source, str) and source for source in sources.values()):
            raise QualificationError(f"Fleet volumes are not physically distinct: {sources!r}")
        if any(mount.get("RW") is not True for mount in (coordinator_mount, paper_mount, live_mount)):
            raise QualificationError("Fleet control and lane volumes must be independently writable.")
        _assert_coordinator_secret_absence(inspections["fleet-coordinator"])
        resources = {service: _assert_limits(inspections[service], service) for service in QUALIFICATION_SERVICES}
        for service in LANE_SERVICES:
            _assert_private_agent_ports(inspections[service], service)

        status, paper_read = _exec_probe(compose, project, "alpaca-paper-clerk", "/read")
        if status != HTTPStatus.OK or paper_read.get("provider", {}).get("provider") != "paper":
            raise QualificationError("Paper lane did not read from its own fake provider.")
        status, live_read_before = _exec_probe(compose, project, "alpaca-live-clerk", "/read")
        if status != HTTPStatus.OK or live_read_before.get("provider", {}).get("provider") != "live":
            raise QualificationError("Live lane did not read from its own fake provider.")
        if live_read_before.get("market", {}).get("market_data") != "read_only":
            raise QualificationError("Live lane did not retain the supported read-only market-data dependency.")
        status, refusal = _exec_probe(compose, project, "alpaca-live-clerk", "/mutate", method="POST")
        if status != HTTPStatus.FORBIDDEN or refusal.get("reason") != "live_mutation_disabled":
            raise QualificationError("The test topology unexpectedly enabled a Live mutation.")

        compose.run(project, ["kill", "alpaca-paper-clerk"])
        compose.run(project, ["rm", "--force", "--stop", "alpaca-paper-clerk"])
        status, live_read_after = _exec_probe(compose, project, "alpaca-live-clerk", "/read")
        if status != HTTPStatus.OK or live_read_after.get("provider", {}).get("provider") != "live":
            raise QualificationError("Live read failed after the Paper process was killed.")
        coordinator_check = compose.run(
            project,
            ["exec", "-T", "fleet-coordinator", "python", "/app/scripts/run_broker_fleet_compose_qualification.py", "--assert-no-custody-root", "/app/artifacts/fleet"],
        )
        evidence.update(
            {
                "volume_sources": sources,
                "resources": resources,
                "paper_read": paper_read,
                "live_read_before_paper_kill": live_read_before,
                "live_read_after_paper_kill": live_read_after,
                "coordinator_custody_check": json.loads(coordinator_check.stdout),
                "live_mutation_refusal": refusal,
                "result": "passed",
            }
        )
        return evidence
    finally:
        if evidence_path is not None:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if started and not keep:
            try:
                compose.run(project, ["--profile", "fleet-qualification", "down", "--volumes", "--remove-orphans"])
            except subprocess.CalledProcessError as exc:
                logger.error("Scoped fleet qualification cleanup failed: %s", exc.stderr)


def _assert_no_custody_root(root: Path) -> dict[str, object]:
    """Prove a coordinator volume has only its inert registry probe artifact."""
    names = sorted(path.name.lower() for path in root.iterdir())
    forbidden = [name for name in names if any(term in name for term in _CUSTODY_TERMS)]
    if forbidden:
        raise QualificationError(f"Coordinator control root contains lane custody material: {forbidden}")
    return {"ok": True, "entries": names}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-role", choices=("coordinator", "lane", "fake-provider", "fake-market-data"))
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
        return {"coordinator": _run_coordinator_probe, "lane": _run_lane_probe, "fake-provider": _run_fake_provider, "fake-market-data": _run_fake_market_data}[args.container_role]()
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
