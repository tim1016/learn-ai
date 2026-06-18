"""Slice 1B — cross-language fixture-driven validation tests.

These exercise the same JSON files the Frontend Vitest suite uses, so
cross-language validation parity is *tested*, not assumed. Add a new
fixture here only when the frontend will also exercise it.

Layout: ``PythonDataService/tests/fixtures/action_plan/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.action_plan import ActionPlan

# Container path. The fixtures directory is bind-mounted under /app/tests/.
_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "action_plan"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_valid_single_stock_fixture_validates() -> None:
    plan = ActionPlan.model_validate(_load("valid_single_stock.json"))

    assert plan.on_enter[0].leg_id == "spy_long"
    assert plan.on_enter[0].instrument.underlying == "SPY"
    assert plan.on_exit[0].entry_leg_id == "spy_long"


@pytest.mark.parametrize(
    ("fixture", "expected_marker"),
    [
        ("invalid_missing_underlying.json", "underlying"),
        ("invalid_qty_ratio_zero.json", "qty_ratio"),
        ("invalid_orphan_close_leg.json", "close_leg"),
        ("invalid_duplicate_leg_id.json", "duplicate"),
    ],
)
def test_invalid_fixtures_reject(fixture: str, expected_marker: str) -> None:
    with pytest.raises(ValidationError, match=expected_marker):
        ActionPlan.model_validate(_load(fixture))
