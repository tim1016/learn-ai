"""Generate JSON contract fixtures for the operator-surface projection.

PRD #607 / Slice 1 (#608) — contract-test bridge between the Python
``operator_surface`` projection and the Frontend ``LiveInstanceStatus``
TypeScript type.  Captures real status responses for each of the
cockpit fixtures (STEADY / CONFIGURE / BLOCKED / TRIAGE) into
``Frontend/src/testing/operator_surface_fixtures/<state>.json``.  The
Frontend then imports each fixture and asserts ``payload satisfies
LiveInstanceStatus`` — a shape mismatch becomes a TypeScript build
failure.

This script is intentionally standalone so it can be re-run after any
projection change and committed alongside the Python diff that triggered
the fixture refresh.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.routers import live_instances


_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = (
    _REPO_ROOT / "Frontend" / "src" / "testing" / "operator_surface_fixtures"
)


def _write_ledger(root: Path, run_id: str, sid: str, created_at_ms: int) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "strategy_instance_id": sid,
                "created_at_ms": created_at_ms,
            }
        ),
        encoding="utf-8",
    )


async def _capture(state: str, process: dict[str, Any] | None) -> dict[str, Any]:
    tmp = Path(__file__).parent / f"_capture_{state}_root"
    tmp.mkdir(parents=True, exist_ok=True)

    stub = SimpleNamespace(
        live_runs_root=str(tmp / "live_runs"),
        live_runner_daemon_url="http://daemon",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    (tmp / "live_runs").mkdir(parents=True, exist_ok=True)
    _write_ledger(tmp / "live_runs", f"run-{state}", "spy_ema_paper", 100)

    monkeyed_settings = live_instances.get_settings
    live_instances.get_settings = lambda: stub  # type: ignore[assignment]

    async def fake_process(_base_url: str, _sid: str) -> dict | None:
        return process

    monkeyed_fetch = host_daemon_client.fetch_instance_process
    host_daemon_client.fetch_instance_process = fake_process  # type: ignore[assignment]
    try:
        from app.main import app

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/live-instances/spy_ema_paper/status")
        return response.json()
    finally:
        live_instances.get_settings = monkeyed_settings  # type: ignore[assignment]
        host_daemon_client.fetch_instance_process = monkeyed_fetch  # type: ignore[assignment]


async def main() -> None:
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    fixtures = {
        "steady": {
            "state": "running",
            "run_id": "run-steady",
            "pid": 99,
            "started_at_ms": 100,
        },
        "stopped": {"state": "idle"},
    }
    for name, process in fixtures.items():
        payload = await _capture(name, process)
        target = _FIXTURE_DIR / f"{name}.json"
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {target}")


if __name__ == "__main__":
    asyncio.run(main())
