from __future__ import annotations

import ast
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
    assert spec.client_id == 12
    assert spec.decision_columns == []
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "Deployment Validation"


def test_deployment_validation_qc_shadow_copy_is_parseable() -> None:
    path = REPO_ROOT / "references" / "qc-shadow" / "DeploymentValidationAlgorithm.py"
    source = path.read_text(encoding="utf-8")

    ast.parse(source)

    assert "class DeploymentValidationAlgorithm" in source
    assert "START_AFTER = time(9, 45)" in source
    assert "STOP_AND_FLATTEN = time(15, 45)" in source
