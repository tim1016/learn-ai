"""Generate JSON contract fixtures for the operator-surface projection.

PRD #607 / Slice 1 (#608) — contract-test bridge between the Python
``/api/live-instances/{id}/status`` route and the Frontend
``LiveInstanceStatus`` TypeScript type.  Captures real status responses
for the cockpit fixtures into
``Frontend/src/testing/operator_surface_fixtures/<state>.json``.
Frontend imports those JSON snapshots directly; pytest also re-captures
the route output and compares it to the committed JSON so stale fixtures
fail CI.

This script is intentionally standalone so it can be re-run after any
projection change and committed alongside the Python diff that triggered
the fixture refresh.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.engine.live import host_daemon_client
from app.engine.live.daemon_transport import DaemonResult
from app.routers import live_instances

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_DIR = (
    _REPO_ROOT / "Frontend" / "src" / "testing" / "operator_surface_fixtures"
)
_FIXTURE_NOW_MS = 1_782_000_000_000
_FIXTURE_ROOT_TOKEN = "__OPERATOR_SURFACE_FIXTURE_ROOT__"
_STRATEGY_INSTANCE_ID = "spy_ema_paper"
_DAEMON_URL = "http://daemon"


@dataclass(frozen=True)
class OperatorSurfaceFixtureScenario:
    name: str
    ledger_run_id: str
    process: dict[str, Any] | None
    strategy_instance_id: str = _STRATEGY_INSTANCE_ID
    ledger_created_at_ms: int = 100
    daemon_url: str = _DAEMON_URL


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


def _sanitize_fixture_payload(payload: Any, root: Path) -> Any:
    if isinstance(payload, dict):
        return {key: _sanitize_fixture_payload(value, root) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_fixture_payload(value, root) for value in payload]
    if isinstance(payload, str):
        return payload.replace(str(root), _FIXTURE_ROOT_TOKEN)
    return payload


def operator_surface_fixture_scenarios() -> dict[str, OperatorSurfaceFixtureScenario]:
    """Return the committed fixture scenarios by stable fixture name."""
    return {
        "steady": OperatorSurfaceFixtureScenario(
            name="steady",
            ledger_run_id="run-steady",
            process={
                "state": "running",
                "run_id": "run-steady",
                "pid": 99,
                "started_at_ms": 100,
            },
        ),
        "stopped": OperatorSurfaceFixtureScenario(
            name="stopped",
            ledger_run_id="run-stopped",
            process={"state": "idle"},
        ),
    }


@contextmanager
def _patched_status_route(root: Path, scenario: OperatorSurfaceFixtureScenario) -> Iterator[None]:
    stub = SimpleNamespace(
        live_runs_root=str(root / "live_runs"),
        live_runner_daemon_url=scenario.daemon_url,
        live_runner_host_start_command="python -m app.engine.live.host_daemon",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )

    monkeyed_settings = live_instances.get_settings
    monkeyed_now_ms = live_instances._now_ms
    monkeyed_fetch = host_daemon_client.fetch_instance_process

    async def fake_process(base_url: str, sid: str) -> tuple[DaemonResult, dict | None]:
        assert base_url == scenario.daemon_url
        assert sid == scenario.strategy_instance_id
        return DaemonResult(kind="CONNECTED"), scenario.process

    live_instances.get_settings = lambda: stub  # type: ignore[assignment]
    live_instances._now_ms = lambda: _FIXTURE_NOW_MS  # type: ignore[assignment]
    host_daemon_client.fetch_instance_process = fake_process  # type: ignore[assignment]
    try:
        yield
    finally:
        live_instances.get_settings = monkeyed_settings  # type: ignore[assignment]
        live_instances._now_ms = monkeyed_now_ms  # type: ignore[assignment]
        host_daemon_client.fetch_instance_process = monkeyed_fetch  # type: ignore[assignment]


async def _capture(scenario: OperatorSurfaceFixtureScenario) -> dict[str, Any]:
    with TemporaryDirectory(prefix=f"operator-surface-{scenario.name}-") as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "live_runs").mkdir(parents=True, exist_ok=True)
        _write_ledger(
            tmp / "live_runs",
            scenario.ledger_run_id,
            scenario.strategy_instance_id,
            scenario.ledger_created_at_ms,
        )

        with _patched_status_route(tmp, scenario):
            from app.main import app

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    f"/api/live-instances/{scenario.strategy_instance_id}/status"
                )
            response.raise_for_status()
            return _sanitize_fixture_payload(response.json(), tmp)


async def capture_operator_surface_fixtures() -> dict[str, dict[str, Any]]:
    captured_fixtures: dict[str, dict[str, Any]] = {}
    for name, scenario in operator_surface_fixture_scenarios().items():
        captured_fixtures[name] = await _capture(scenario)
    return captured_fixtures


def write_operator_surface_fixtures(fixtures: dict[str, dict[str, Any]]) -> None:
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in fixtures.items():
        target = _FIXTURE_DIR / f"{name}.json"
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {target}")


async def main() -> None:
    write_operator_surface_fixtures(await capture_operator_surface_fixtures())


if __name__ == "__main__":
    asyncio.run(main())
