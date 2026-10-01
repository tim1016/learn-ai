from __future__ import annotations

import json
from pathlib import Path

from app.engine.strategy.spec.schema import load_spec_from_path

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_deployment_validation_spec_fixture_loads() -> None:
    path = (
        REPO_ROOT
        / "PythonDataService"
        / "app"
        / "engine"
        / "strategy"
        / "spec"
        / "fixtures"
        / "deployment_validation.spec.json"
    )

    spec = load_spec_from_path(path)

    assert spec.symbols == ["SPY"]
    assert spec.resolution.period_minutes == 1
    assert spec.decision_columns == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["name"] == "Deployment Validation"
    assert "client_id" not in payload
