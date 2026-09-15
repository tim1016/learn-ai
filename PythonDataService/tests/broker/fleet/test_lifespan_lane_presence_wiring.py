"""#2141: the lifespan seam that opens/closes lane presence is itself pinned.

``serve_lane_presence`` and ``stop_heartbeat`` (``fleet_boot.py``) are each
covered by direct unit tests, but nothing previously failed if the two call
sites that wire them into ``app.main.lifespan`` regressed — the exact gap
that let the paper-lane heartbeat deadlock ship twice (#2137 was the second
fix). Gating the outer-lifespan install behind a binding check (the
historical broken shape) would redden nothing at the unit level, because
``serve_lane_presence`` and ``stop_heartbeat`` still work correctly in
isolation; only a test that drives the real ``app.main.lifespan`` catches a
regression at the call site itself.

This boots the real application, as a subprocess (a fresh interpreter is the
only way to get ``FLEET_ROLE=clerk_agent`` into the module-level constants
``app.main`` fixes at import time), against a freshly enrolled — and
correctly matching — Alpaca lane so ``open_fleet_lane`` returns an *online*,
*unbound* boot: the exact "lane open, no binding installed" shape the 2026-
09-15 incident deadlocked on. The subprocess wraps
``fleet_boot.start_heartbeat``/``stop_heartbeat`` before importing
``app.main`` so every call, regardless of which of the two call sites in
``main.py`` (the outer-lifespan install, and the teardown ordering — first
inside ``_service_lifespan``'s own ``finally``, again idempotently through
``close_fleet_lane``) reaches it, is counted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters

_PROBE = """
import sys
sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]
import asyncio
import json

import app.broker.alpaca.clerk.fleet_boot as fleet_boot_module

_calls = {"start": 0, "stop": 0}
_state = {"boot": None, "alive_immediately_after_start": None}
_real_start = fleet_boot_module.start_heartbeat
_real_stop = fleet_boot_module.stop_heartbeat


def _wrapped_start(boot, *, interval_s):
    _calls["start"] += 1
    _state["boot"] = boot
    task = _real_start(boot, interval_s=interval_s)
    _state["alive_immediately_after_start"] = task is not None and not task.done()
    return task


async def _wrapped_stop(boot):
    _calls["stop"] += 1
    await _real_stop(boot)


fleet_boot_module.start_heartbeat = _wrapped_start
fleet_boot_module.stop_heartbeat = _wrapped_stop

from app.main import app, lifespan


async def _boot():
    async with lifespan(app):
        pass


try:
    asyncio.run(_boot())
    outcome = {
        "error": None,
        "start_calls": _calls["start"],
        "stop_calls": _calls["stop"],
        "alive_immediately_after_start": _state["alive_immediately_after_start"],
        "heartbeat_is_none_after_exit": (
            _state["boot"] is not None and _state["boot"].heartbeat is None
        ),
        "boot_was_online": _state["boot"] is not None and _state["boot"].online,
    }
except BaseException as exc:
    outcome = {"error": f"{type(exc).__name__}: {exc}"}

sys.stdout.write(json.dumps(outcome) + "\\n")
"""


def _boot_online_unbound_lane(tmp_path: Path) -> dict[str, str]:
    """Env for a real subprocess boot: an enrolled, matching, unbound lane."""
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

    service_root = Path(__file__).resolve().parents[3]
    return {
        **os.environ,
        "PYTHONPATH": str(service_root),
        "DATA_PLANE_CONTROL_SECRET": "",
        "FLEET_ROLE": "clerk_agent",
        "FLEET_CONTROL_DIR": str(control_dir),
        "FLEET_CLERK_ID": provisioned.clerk.clerk_id,
        "FLEET_WORKER_KEY": provisioned.clerk.worker_key,
        "FLEET_DEPLOYMENT_NAMESPACE": "compose:test",
        "FLEET_HEARTBEAT_INTERVAL_S": "0.05",
        "FLEET_MAX_INFLIGHT_REQUESTS": "1",
        "FLEET_MAX_INFLIGHT_STREAMS": "1",
        "FLEET_REQUEST_QUEUE_LIMIT": "0",
        "FLEET_REQUEST_QUEUE_TIMEOUT_MS": "0",
        "ALPACA_CLERK_DIR": str(volume_root),
        # Inside the volume, so the writable-root fence is satisfied.
        "IBKR_LIVE_RUNS_ROOT": str(volume_root / "live_runs" / "runs"),
        "IBKR_LIVE_BARS_ROOT": str(volume_root / "live_bars"),
        # Best-effort IBKR connect is orthogonal to this seam; disabling it
        # keeps the boot fast and free of a real gateway dependency.
        "IBKR_BROKER_ENABLED": "false",
    }, service_root


def test_lane_presence_beats_once_open_and_stops_at_both_teardown_sites(
    tmp_path: Path,
) -> None:
    """Drives the real ``app.main.lifespan`` for an online, unbound lane.

    Pins: (1) the outer-lifespan install calls ``start_heartbeat`` exactly
    once for a lane that opened online, with no binding installed at all —
    reddening if the call site is ever re-gated on binding state, the
    historical bug this test exists to catch twice-over; (2) teardown calls
    ``stop_heartbeat`` exactly twice, once from inside ``_service_lifespan``'s
    own ``finally`` and once (idempotently) from ``close_fleet_lane`` in
    ``lifespan``'s ``finally`` — reddening if either call site is deleted.
    """
    environment, service_root = _boot_online_unbound_lane(tmp_path)

    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=environment,
        cwd=service_root,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])

    assert outcome["error"] is None, outcome["error"]
    assert outcome["boot_was_online"] is True
    assert outcome["alive_immediately_after_start"] is True
    assert outcome["start_calls"] == 1
    assert outcome["stop_calls"] == 2
    assert outcome["heartbeat_is_none_after_exit"] is True
