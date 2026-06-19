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


@pytest.mark.parametrize(
    ("fixture", "expected_leg_count"),
    [
        ("valid_single_stock.json", 1),
        ("valid_single_option.json", 1),
        ("valid_vertical.json", 2),
        ("valid_iron_condor.json", 4),
    ],
)
def test_valid_fixtures_validate(fixture: str, expected_leg_count: int) -> None:
    plan = ActionPlan.model_validate(_load(fixture))

    assert len(plan.on_enter) == expected_leg_count


@pytest.mark.parametrize(
    ("fixture", "expected_marker"),
    [
        ("invalid_missing_underlying.json", "underlying"),
        ("invalid_qty_ratio_zero.json", "qty_ratio"),
        ("invalid_orphan_close_leg.json", "close_leg"),
        ("invalid_duplicate_leg_id.json", "duplicate"),
        ("invalid_unknown_selector.json", "selector"),
        ("invalid_absolute_expiry_missing_ms.json", "expiration_ms"),
    ],
)
def test_invalid_fixtures_reject(fixture: str, expected_marker: str) -> None:
    with pytest.raises(ValidationError, match=expected_marker):
        ActionPlan.model_validate(_load(fixture))
