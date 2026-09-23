"""ADR 0062 Decision 2: nothing opens on the clerk volume before its gate.

A boot against a volume whose marker disagrees with the registry must leave
the volume byte-identical: no profiles database, no installation lock file,
not even the ``broker_configuration/`` directory the two of them share.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet.volume import marker_path
from app.broker.fleet_composition import production_provider_adapters

_PROBE = (
    # Defends against tests/operator shadowing the stdlib operator module (see
    # test_a2_alpaca_lane.py's `_route_paths_for_role`, which scrubs the same PYTHONPATH entry).
    "import sys; sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]; "
    "import asyncio, json; "
    "from app.main import app, lifespan\n"
    "async def _boot():\n"
    "    try:\n"
    "        async with lifespan(app):\n"
    "            return {'refused': None}\n"
    "    except BaseException as exc:\n"
    "        return {'refused': type(exc).__name__}\n"
    "sys.stdout.write(json.dumps(asyncio.run(_boot())) + '\\n')"
)


def test_a_mismatched_volume_marker_leaves_no_writer_artifact(tmp_path: Path) -> None:
    """The gate refuses before the profiles DB and the installation lock exist."""
    control_dir = tmp_path / "control"
    volume_root = tmp_path / "volumes" / "paper"
    volume_root.mkdir(parents=True)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    try:
        provisioned = service.provision_clerk(
            broker="alpaca",
            display_label="Paper",
            volume_root=volume_root,
            attestation_id="learn-ai-alpaca-paper",
            deployment_namespace="compose:test",
        )
    finally:
        service.close()

    # Poison the marker exactly as a restored-from-another-lane volume would:
    # the registry expects the provisioned volume_id, the volume claims another.
    marker = json.loads(marker_path(volume_root).read_text(encoding="utf-8"))
    marker["volume_id"] = "vol_ffffffffffffffffffffffff"
    marker_path(volume_root).write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )

    service_root = Path(__file__).resolve().parents[3]
    environment = {
        **os.environ,
        "PYTHONPATH": str(service_root),
        "DATA_PLANE_CONTROL_SECRET": "",
        "FLEET_ROLE": "clerk_agent",
        "FLEET_CONTROL_DIR": str(control_dir),
        "FLEET_CLERK_ID": provisioned.clerk.clerk_id,
        "FLEET_WORKER_KEY": provisioned.clerk.worker_key,
        "FLEET_DEPLOYMENT_NAMESPACE": "compose:test",
        "FLEET_MAX_INFLIGHT_REQUESTS": "1",
        "FLEET_MAX_INFLIGHT_STREAMS": "1",
        "FLEET_REQUEST_QUEUE_LIMIT": "0",
        "FLEET_REQUEST_QUEUE_TIMEOUT_MS": "0",
        "ALPACA_CLERK_DIR": str(volume_root),
        # Inside the volume, so _fence_writable_roots cannot mask the gate.
        "IBKR_LIVE_RUNS_ROOT": str(volume_root / "live_runs"),
        "IBKR_LIVE_BARS_ROOT": str(volume_root / "live_bars"),
    }
    before = sorted(p.relative_to(volume_root) for p in volume_root.rglob("*"))
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=environment,
        cwd=service_root,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome["refused"] == "ClerkVolumeIdentityMismatch"

    # The proof: the gate ran before anything opened on the volume — not a
    # single path under it, writer artifact or otherwise, was added or removed.
    after = sorted(p.relative_to(volume_root) for p in volume_root.rglob("*"))
    assert after == before
