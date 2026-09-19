"""Every clerk role's Polygon key stays present-but-empty (issue #2204, FR-015).

Renders the deployed dev and production fleet compose topologies with
`compose config` (mirroring `scripts/render_fleet_topology.py`'s own
posture -- secrets substituted with the tracked, non-secret
`deploy/fleet/ci-render.placeholders` values) and inspects each *rendered
service's own* `FLEET_ROLE` value, never its service name: dev names the
coordinator role `python-service` (``FLEET_ROLE=fleet_coordinator``);
production names it `fleet-coordinator`. A service-name-keyed assertion would
pass on one topology and silently miss a regression on the other.

Unlike `render_fleet_topology.py`'s committed snapshot -- which deliberately
drops every environment *value*, secret or not, so the file is safe to
commit -- this test renders live and never persists anything it reads. It is
marked ``slow`` (a live Compose render, not a fast unit check) and skips
outright when neither Docker nor Podman is on the host, exactly like
`scripts/run_broker_fleet_compose_qualification.py`'s own engine discovery.

The Compose qualification overlay (`compose.fleet.qualification.yaml`) is
deliberately out of scope: PRD #2201 §11.6 records that qualification's
placeholder key is a known, accepted exception, not a topology this
invariant governs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLACEHOLDERS = REPOSITORY_ROOT / "deploy" / "fleet" / "ci-render.placeholders"

_ENV_FILE_DEFAULTS = {
    "FLEET_LIVE_ENV_FILE": "./deploy/fleet/env/live.env.example",
    "FLEET_PAPER_ENV_FILE": "./deploy/fleet/env/paper.env.example",
    "FLEET_COORDINATOR_ENV_FILE": "./deploy/fleet/env/coordinator.env.example",
}

pytestmark = pytest.mark.slow


def _compose_engine() -> tuple[str, ...] | None:
    if shutil.which("docker") is not None:
        return ("docker", "compose")
    if shutil.which("podman") is not None:
        return ("podman", "compose")
    return None


def _require_compose_engine() -> tuple[str, ...]:
    """The engine to render with, or a loud failure on CI.

    A check that can silently never run is worse than no check: CI runners
    are expected to carry Docker or Podman, so a CI run with neither is a
    broken runner, not an environment this test politely defers to. Only a
    developer's own host -- where neither engine may be installed at all --
    still gets the quiet skip.
    """
    engine = _compose_engine()
    if engine is not None:
        return engine
    if os.environ.get("CI"):
        pytest.fail(
            "neither Docker nor Podman is available on this CI runner; "
            "this topology check cannot silently skip in CI."
        )
    pytest.skip("neither Docker nor Podman is available on this host")


def _render(
    engine: tuple[str, ...], compose_files: tuple[str, ...], *, profiles: tuple[str, ...] = ()
) -> dict[str, dict[str, Any]]:
    """The rendered ``services`` mapping for one compose file combination."""
    command = [
        *engine,
        "--project-name",
        "learn-ai-history-batch-topology-check",
        "--project-directory",
        str(REPOSITORY_ROOT),
        "--env-file",
        str(PLACEHOLDERS),
    ]
    for name in compose_files:
        command += ["--file", str(REPOSITORY_ROOT / name)]
    for profile in profiles:
        command += ["--profile", profile]
    command += ["config", "--format", "json"]
    render_environment = {**os.environ}
    # The Python test gate runs with DATA_PLANE_CONTROL_SECRET="" (an
    # explicit empty-string override so the app's own tests never see a
    # spurious 403) -- Compose's `${VAR:?...}` mandatory-non-empty check
    # treats an inherited empty string as still missing, so that override
    # must not shadow the tracked placeholder's non-empty value here.
    render_environment.pop("DATA_PLANE_CONTROL_SECRET", None)
    for key, default in _ENV_FILE_DEFAULTS.items():
        render_environment.setdefault(key, default)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPOSITORY_ROOT,
        env=render_environment,
        check=False,
    )
    assert completed.returncode == 0, (
        f"rendering {compose_files} failed:\n{completed.stderr}"
    )
    return json.loads(completed.stdout).get("services") or {}


def _assert_role_split_honors_the_credential_boundary(services: dict[str, dict[str, Any]]) -> None:
    """The invariant, applied to whichever service names this topology used."""
    roles_seen: set[str] = set()
    for name, spec in services.items():
        environment = spec.get("environment") or {}
        role = environment.get("FLEET_ROLE")
        if role is None:
            continue
        roles_seen.add(role)
        polygon_key = environment.get("POLYGON_API_KEY")
        if role == "clerk_agent":
            assert polygon_key == "", (
                f"service {name!r} (FLEET_ROLE=clerk_agent) must carry a "
                f"present-but-empty POLYGON_API_KEY, found {polygon_key!r}"
            )
        elif role == "fleet_coordinator":
            assert polygon_key not in (None, ""), (
                f"service {name!r} (FLEET_ROLE=fleet_coordinator) must own a "
                "usable POLYGON_API_KEY configuration"
            )
    # A vacuous pass (no role matched at all) would hide a rename regression.
    assert {"clerk_agent", "fleet_coordinator"} <= roles_seen


def test_dev_topology_role_split_honors_the_credential_boundary() -> None:
    engine = _require_compose_engine()
    services = _render(engine, ("compose.yaml", "compose.fleet.dev.yaml"))
    _assert_role_split_honors_the_credential_boundary(services)


def test_production_topology_role_split_honors_the_credential_boundary() -> None:
    engine = _require_compose_engine()
    services = _render(engine, ("compose.yaml", "compose.fleet.yaml"), profiles=("fleet",))
    _assert_role_split_honors_the_credential_boundary(services)


def test_dev_and_production_name_the_coordinator_role_differently() -> None:
    """The exact regression this file guards against: a service-name-keyed
    assertion would silently stop checking one of these two topologies."""
    engine = _require_compose_engine()
    dev_services = _render(engine, ("compose.yaml", "compose.fleet.dev.yaml"))
    prod_services = _render(engine, ("compose.yaml", "compose.fleet.yaml"), profiles=("fleet",))

    def _coordinator_name(services: dict[str, dict[str, Any]]) -> str:
        (name,) = (
            name
            for name, spec in services.items()
            if (spec.get("environment") or {}).get("FLEET_ROLE") == "fleet_coordinator"
        )
        return name

    assert _coordinator_name(dev_services) == "python-service"
    assert _coordinator_name(prod_services) == "fleet-coordinator"
