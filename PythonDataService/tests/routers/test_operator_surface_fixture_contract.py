"""Freshness guard for Frontend operator-surface fixtures.

The Frontend imports committed JSON snapshots from
``Frontend/src/testing/operator_surface_fixtures``. This test re-captures
the same deterministic ASGI route output and fails when the committed
``operator_surface`` JSON snapshots are stale.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.capture_operator_surface_fixture import (
    capture_operator_surface_fixtures,
    operator_surface_fixture_scenarios,
)

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "Frontend"
    / "src"
    / "testing"
    / "operator_surface_fixtures"
)
_EXPECTED_FIXTURE_NAMES = {"steady", "stopped"}


def test_operator_surface_fixture_scenario_set_is_explicit() -> None:
    scenarios = operator_surface_fixture_scenarios()
    generated_names = set(scenarios)
    committed_names = {path.stem for path in _FIXTURE_DIR.glob("*.json")}

    assert generated_names == _EXPECTED_FIXTURE_NAMES
    assert committed_names == _EXPECTED_FIXTURE_NAMES
    expected_scenarios = {
        "steady": {
            "ledger_run_id": "fixture-active-ledger-042",
            "strategy_instance_id": "fixture_steady_bot",
            "ledger_created_at_ms": 4_242,
            "daemon_url": "http://fixture-daemon-steady",
        },
        "stopped": {
            "ledger_run_id": "fixture-evidence-ledger-314",
            "strategy_instance_id": "fixture_stopped_bot",
            "ledger_created_at_ms": 3_141,
            "daemon_url": "http://fixture-daemon-stopped",
        },
    }
    for name, expected in expected_scenarios.items():
        scenario = scenarios[name]
        assert scenario.name == name
        assert {
            "ledger_run_id": scenario.ledger_run_id,
            "strategy_instance_id": scenario.strategy_instance_id,
            "ledger_created_at_ms": scenario.ledger_created_at_ms,
            "daemon_url": scenario.daemon_url,
        } == expected
        if scenario.process is not None and scenario.process.get("state") == "running":
            assert scenario.process.get("run_id") == scenario.ledger_run_id


@pytest.mark.asyncio
async def test_frontend_operator_surface_fixtures_match_python_status_route() -> None:
    captured = await capture_operator_surface_fixtures()
    assert set(captured) == _EXPECTED_FIXTURE_NAMES

    committed = {
        name: json.loads((_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
        for name in _EXPECTED_FIXTURE_NAMES
    }

    assert committed == captured, (
        "Frontend operator-surface fixtures are stale. Regenerate with "
        "`PYTHONPATH=PythonDataService "
        "PythonDataService/.venv/bin/python "
        "PythonDataService/scripts/capture_operator_surface_fixture.py` "
        "and commit the updated JSON snapshots."
    )
