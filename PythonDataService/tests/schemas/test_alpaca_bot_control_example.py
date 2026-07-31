"""Cross-stack fixture validation for the static Clerk diagnostic gallery."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from app.routers.alpaca_bot_control_examples import _load_fixtures
from app.schemas.alpaca_bot_control_example import ScenarioId

_EXPECTED_SCENARIOS = set(get_args(ScenarioId))


def test_all_committed_diagnostic_fixtures_validate_against_python_contract() -> None:
    """The 15 local documents remain Pydantic-validated backend examples."""
    fixtures = _load_fixtures()

    assert {fixture.scenario_id for fixture in fixtures} == _EXPECTED_SCENARIOS
    assert len(fixtures) == len(_EXPECTED_SCENARIOS)


@pytest.mark.parametrize("fixture", sorted(_EXPECTED_SCENARIOS))
def test_each_scenario_is_a_separate_committed_document(fixture: str) -> None:
    root = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "alpaca-bot-control" / "v1"

    assert (root / f"{fixture}.json").is_file()
