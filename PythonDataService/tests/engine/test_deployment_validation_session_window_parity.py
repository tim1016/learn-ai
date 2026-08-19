"""Cross-implementation parity for deployment_validation's session window.

Formula: ``detection_start_ms = session_open_ms_utc(d) + 15min``,
``stop_and_flatten_ms = session_close_ms_utc(d) - 15min``.
Reference: NYSE regular-session and day-after-Thanksgiving early-close
hours, as encoded in the canonical calendar module — see
``tests/fixtures/golden/deployment-validation-session-window/attribution.md``.
Canonical implementation:
``app/engine/strategy/algorithms/deployment_validation.py::_session_decision_window_ms``.
Validated against: this file, at tolerance 0.

The QC shadow copy and the LEAN trusted template depend on LEAN's
``Exchange.Hours`` runtime and cannot be executed outside a LEAN sandbox in
this test suite. Their ``_session_window`` formulas are instead extracted
from source via ``ast`` (offset constants + the open/close +/- arithmetic
shape) and applied to this fixture's session open/close, proving they
reduce to the same boundaries as the canonical Python kernel. See the
fixture's attribution.md for exactly what this does and does not prove.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

from app.engine.strategy.algorithms.deployment_validation import _session_decision_window_ms
from app.lean_sidecar.trusted_samples.deployment_validation import DEPLOYMENT_VALIDATION_SOURCE

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = (
    REPO_ROOT / "PythonDataService" / "tests" / "fixtures" / "golden" / "deployment-validation-session-window"
)


def _load_fixture() -> dict:
    return json.loads((FIXTURE_DIR / "boundaries.json").read_text(encoding="utf-8"))


def test_canonical_kernel_matches_fixture_boundaries() -> None:
    fixture = _load_fixture()

    for case in (fixture["regular_session"], fixture["half_day_session"]):
        session_date = date.fromisoformat(case["session_date"])

        detection_start_ms, stop_and_flatten_ms = _session_decision_window_ms(session_date)

        assert detection_start_ms == case["detection_start_ms_utc"]
        assert stop_and_flatten_ms == case["stop_and_flatten_ms_utc"]


def _extract_offset_minutes(class_body: list[ast.stmt], attr_name: str) -> int:
    for node in class_body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == attr_name for target in node.targets):
            continue
        call = node.value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name) and call.func.id == "timedelta"
        assert [kw.arg for kw in call.keywords] == ["minutes"]
        return call.keywords[0].value.value
    raise AssertionError(f"{attr_name} not found in class body")


def _binop_operator_symbol(node: ast.expr) -> str:
    assert isinstance(node, ast.BinOp)
    if isinstance(node.op, ast.Add):
        return "+"
    if isinstance(node.op, ast.Sub):
        return "-"
    raise AssertionError(f"unexpected operator {ast.dump(node.op)}")


def _binop_offset_attr_name(node: ast.expr) -> str:
    assert isinstance(node, ast.BinOp)
    assert isinstance(node.right, ast.Attribute)
    return node.right.attr


def _extract_session_window_formula(cls: ast.ClassDef) -> tuple[str, int, str, int]:
    """Parse ``cls``'s ``_session_window`` method and return
    ``(detection_op, detection_offset_min, stop_op, stop_offset_min)``,
    verifying — not assuming — that its return statement is shaped
    ``(session_open <op> DETECTION_OFFSET, session_close <op> STOP_OFFSET)``.
    """
    method = next(
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_session_window"
    )
    return_node = next(node for node in ast.walk(method) if isinstance(node, ast.Return))
    assert isinstance(return_node.value, ast.Tuple)
    detection_expr, stop_expr = return_node.value.elts

    detection_op = _binop_operator_symbol(detection_expr)
    stop_op = _binop_operator_symbol(stop_expr)
    detection_offset_min = _extract_offset_minutes(cls.body, _binop_offset_attr_name(detection_expr))
    stop_offset_min = _extract_offset_minutes(cls.body, _binop_offset_attr_name(stop_expr))

    return detection_op, detection_offset_min, stop_op, stop_offset_min


def _apply(op: str, base_ms: int, offset_minutes: int) -> int:
    offset_ms = offset_minutes * 60_000
    return base_ms + offset_ms if op == "+" else base_ms - offset_ms


def _assert_formula_matches_fixture(cls: ast.ClassDef, fixture: dict) -> None:
    detection_op, detection_offset_min, stop_op, stop_offset_min = _extract_session_window_formula(cls)

    for case in (fixture["regular_session"], fixture["half_day_session"]):
        assert (
            _apply(detection_op, case["session_open_ms_utc"], detection_offset_min)
            == case["detection_start_ms_utc"]
        )
        assert (
            _apply(stop_op, case["session_close_ms_utc"], stop_offset_min) == case["stop_and_flatten_ms_utc"]
        )


def test_qc_shadow_copy_formula_matches_fixture_boundaries() -> None:
    fixture = _load_fixture()
    path = REPO_ROOT / "references" / "qc-shadow" / "DeploymentValidationAlgorithm.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "DeploymentValidationAlgorithm"
    )

    _assert_formula_matches_fixture(cls, fixture)


def test_lean_trusted_template_formula_matches_fixture_boundaries() -> None:
    fixture = _load_fixture()
    module = ast.parse(DEPLOYMENT_VALIDATION_SOURCE)
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "MyAlgorithm")

    _assert_formula_matches_fixture(cls, fixture)
