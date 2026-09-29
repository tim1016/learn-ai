"""A hold is a hold: no owner copy says "paused" (PRD #2560 story 28, #2567).

The owner reads hold and recovery prose verbatim -- the vocabulary's hold
explanations on Home and the bot page, and each uncertainty's headline,
explanation and impact in Activity's order records. Pause was retired
(#2540); a hold that reads "New submits are paused" names a control the
owner no longer has. The copy is authored in many modules, so this scans
every prose string literal they author rather than trusting a list.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.broker.v2panel.vocabulary import OPERATOR_COPY

_APP = Path(__file__).resolve().parents[3] / "app"
# Every module family that authors hold, recovery or uncertainty prose.
_COPY_ROOTS = (
    _APP / "broker" / "alpaca" / "clerk",
    _APP / "broker" / "v2panel",
    _APP / "services" / "broker_v2_panel",
)
# Prose only: a lowercase "pause" word. Stored identifiers such as the manual
# ticket state ``PAUSED_UNKNOWN`` are codes, rendered through the label pipe.
_PAUSED = re.compile(r"\b[Pp]aus(?:e|ed|es|ing)\b")


def _prose_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        # A bare identifier ("pause" in the retired action-id Literal) is not prose.
        and " " in node.value
    ]


def test_no_vocabulary_copy_says_paused() -> None:
    offenders = {
        code: copy
        for code, copy in OPERATOR_COPY.items()
        if _PAUSED.search(copy.label) or _PAUSED.search(copy.explanation)
    }
    assert offenders == {}


@pytest.mark.parametrize("root", _COPY_ROOTS, ids=lambda root: str(root.relative_to(_APP)))
def test_no_authored_hold_or_recovery_prose_says_paused(root: Path) -> None:
    offenders = [
        f"{path.relative_to(_APP)}:{line}: {text!r}"
        for path in sorted(root.rglob("*.py"))
        for line, text in _prose_literals(path)
        if _PAUSED.search(text)
    ]
    assert offenders == []
