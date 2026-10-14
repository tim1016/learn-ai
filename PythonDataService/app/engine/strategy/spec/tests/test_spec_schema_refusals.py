"""Spec values that can only fail inside ``engine.run`` are refused where the spec is read (F2-2, F2-3).

Reproduced before: ``TimeOfDay.after="45am"`` answered ``success=false`` with
``invalid literal for int() with base 10``, ``tz="Mars/Phobos"`` with
``No time zone found``, MACD ``fast >= slow`` with the engine's own message,
and a spec whose ``FixedContracts`` entry never fires ran to ``total_trades 0``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.engine.strategy.spec as spec_package
from app.engine.strategy.spec import schema as S
from app.engine.strategy.spec.evaluator import SpecAlgorithm

_FIXTURES = Path(spec_package.__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.spec.json").read_text(encoding="utf-8"))


def _time_of_day(**values: str) -> dict:
    return {"kind": "TimeOfDay", **values}


@pytest.mark.parametrize("value", ["45am", "9:30", "24:00", "09:60", "0930", ""])
def test_a_clock_time_that_is_not_hh_mm_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="HH:MM"):
        S.TimeOfDay.model_validate(_time_of_day(after=value))
    with pytest.raises(ValidationError, match="HH:MM"):
        S.TimeOfDay.model_validate(_time_of_day(before=value))


def test_a_time_zone_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValidationError, match="IANA time zone"):
        S.TimeOfDay.model_validate(_time_of_day(after="09:45", tz="Mars/Phobos"))


def test_a_well_formed_time_of_day_is_accepted() -> None:
    node = S.TimeOfDay.model_validate(_time_of_day(after="09:45", before="15:45", tz="America/Chicago"))

    assert (node.after, node.before, node.tz) == ("09:45", "15:45", "America/Chicago")


def test_macd_with_fast_not_shorter_than_slow_is_refused_and_the_defaults_are_checked_too() -> None:
    base = {"id": "m", "kind": "MACD", "period": 26}

    S.IndicatorBlock.model_validate({**base, "fast_period": 12})
    with pytest.raises(ValidationError, match="must be less than period"):
        S.IndicatorBlock.model_validate({**base, "fast_period": 30})
    with pytest.raises(ValidationError, match="must be less than period"):
        S.IndicatorBlock.model_validate({"id": "m", "kind": "MACD", "period": 10})  # default fast is 12


def test_a_fixed_contracts_spec_is_refused_at_construction_not_when_an_entry_fires() -> None:
    payload = copy.deepcopy(_fixture("sma_crossover"))
    payload["entry"]["size"] = {"kind": "FixedContracts", "value": 1}
    spec = S.StrategySpec.model_validate(payload)

    with pytest.raises(NotImplementedError, match="FixedContracts"):
        SpecAlgorithm(spec)
