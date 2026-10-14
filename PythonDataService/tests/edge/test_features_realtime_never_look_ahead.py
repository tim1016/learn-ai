"""``features_realtime`` is the half of the edge package a decision may read; it must not look ahead (G-12).

Three docstrings (``edge/__init__.py``, ``labels_oracle/forward_rv.py``,
``labels_oracle/hf_forward_rv.py``) say a guard keeps the oracle labels out of
the realtime features. This is that guard: a realtime feature that imports
the oracle package, or reads a later row with a negative ``shift``, fails
here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.engine.edge.features_realtime as features_realtime

_FEATURE_FILES = sorted(Path(features_realtime.__file__).parent.glob("*.py"))


def _imports_of(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _negative_shifts(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "shift":
            argument = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "periods"), None)
            if (
                isinstance(argument, ast.UnaryOp)
                and isinstance(argument.op, ast.USub)
                and isinstance(argument.operand, ast.Constant)
            ):
                lines.append(node.lineno)
    return lines


def test_the_guard_looks_at_the_realtime_feature_files() -> None:
    assert {path.name for path in _FEATURE_FILES} >= {"hf_realized_vol.py", "realized_vol.py", "iv30_constructor.py"}


@pytest.mark.parametrize("path", _FEATURE_FILES, ids=lambda path: path.name)
def test_a_realtime_feature_never_imports_the_oracle_labels_or_shifts_backward(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    assert [name for name in _imports_of(tree) if "labels_oracle" in name] == []
    assert _negative_shifts(tree) == []


def test_the_guard_catches_what_it_guards_against() -> None:
    tree = ast.parse("from app.engine.edge.labels_oracle.forward_rv import forward_rv\nx = s.shift(-5)\n")

    assert _imports_of(tree)[0].endswith("labels_oracle.forward_rv")
    assert _negative_shifts(tree) == [2]
