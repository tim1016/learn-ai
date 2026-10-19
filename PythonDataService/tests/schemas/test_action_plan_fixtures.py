"""Action Plans as they are written down: JSON files validated as stored.

Layout: ``PythonDataService/tests/fixtures/action_plan/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
