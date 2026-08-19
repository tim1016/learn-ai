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
    assert spec.decision_columns == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["name"] == "Deployment Validation"
    assert "client_id" not in payload


def _assert_timedelta_minutes_call(node: ast.AST, minutes: int) -> None:
    assert isinstance(node, ast.Call)
    assert isinstance(node.func, ast.Name)
    assert node.func.id == "timedelta"
    assert [kw.arg for kw in node.keywords] == ["minutes"]
    assert node.keywords[0].value.value == minutes


def test_deployment_validation_qc_shadow_copy_is_parseable() -> None:
    """Regression for #1672: the session-boundary literals were replaced
    with calendar-derived offsets, so a half-day close still gets a real,
    reachable stop/flatten barrier (see
    docs/references/deployment-validation-consecutive-green.md)."""
    path = REPO_ROOT / "references" / "qc-shadow" / "DeploymentValidationAlgorithm.py"
    source = path.read_text(encoding="utf-8")

    module = ast.parse(source)
    cls = next(
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "DeploymentValidationAlgorithm"
    )
    assigns = {
        target.id: node.value
        for node in cls.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    _assert_timedelta_minutes_call(assigns["DETECTION_START_OFFSET"], 15)
    _assert_timedelta_minutes_call(assigns["STOP_AND_FLATTEN_OFFSET"], 15)
