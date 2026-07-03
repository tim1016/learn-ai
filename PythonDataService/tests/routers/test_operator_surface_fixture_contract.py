"""Freshness guard for Frontend live-instance status fixtures.

The Frontend imports committed JSON snapshots from
``Frontend/src/testing/operator_surface_fixtures``. This test re-captures
the same deterministic ASGI route output and fails when the JSON snapshots
are stale.
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
    expected_ledger_run_ids = {"steady": "run-steady", "stopped": "run-stopped"}
    for name, scenario in scenarios.items():
        assert scenario.name == name
        assert scenario.strategy_instance_id == "spy_ema_paper"
        assert scenario.ledger_run_id == expected_ledger_run_ids[name]
        assert scenario.ledger_created_at_ms == 100
        assert scenario.daemon_url == "http://daemon"


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
