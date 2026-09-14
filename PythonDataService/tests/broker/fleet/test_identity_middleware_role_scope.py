"""`FleetIdentityMiddleware` belongs only on a process that serves a lane.

The middleware exists to echo the identity *this runtime serves*. A
``fleet_coordinator`` serves no lane, so it has no identity to echo — but every
agent heartbeat pins ``X-Fleet-Clerk-Id`` on its way *to* the coordinator, so
installing it there turned each heartbeat into a warning describing the opposite
direction. With two lanes that is roughly twelve a minute, which buries the
warnings that mean something.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[3]

_MIDDLEWARE = "FleetIdentityMiddleware"


def _middleware_for_role(role: str) -> set[str]:
    environment = {**os.environ, "FLEET_ROLE": role}
    if role == "clerk_agent":
        environment.update(
            {
                "FLEET_MAX_INFLIGHT_REQUESTS": "1",
                "FLEET_MAX_INFLIGHT_STREAMS": "1",
                "FLEET_REQUEST_QUEUE_LIMIT": "0",
                "FLEET_REQUEST_QUEUE_TIMEOUT_MS": "0",
            }
        )
    inherited = [
        entry
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != SERVICE_ROOT / "tests"
    ]
    environment["PYTHONPATH"] = os.pathsep.join([str(SERVICE_ROOT), *inherited])
    probe = (
        "import sys; sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]; "
        "import json; from app.main import app; "
        "print(json.dumps(sorted({m.cls.__name__ for m in app.user_middleware})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        cwd=SERVICE_ROOT,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("role", ["combined", "clerk_agent"])
def test_a_lane_serving_role_installs_the_identity_echo(role: str) -> None:
    assert _MIDDLEWARE in _middleware_for_role(role)


def test_the_coordinator_does_not_install_the_identity_echo() -> None:
    """It serves no lane, so every pinned inbound request was only ever noise."""
    assert _MIDDLEWARE not in _middleware_for_role("fleet_coordinator")
