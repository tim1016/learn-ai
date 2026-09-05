"""Structured sweep eligibility (PRD #1926 "Domain and eligibility")."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.sweep.eligibility import (
    REASON_NO_SIGNAL_PROGRAM,
    REASON_NON_NUMERIC_PUBLIC_PARAMETER,
    REASON_NOT_PRODUCTION_CANDIDATE,
    eligible_strategy_keys,
    non_numeric_parameters,
    sweep_eligibility,
)


def test_every_production_candidate_is_eligible_with_no_reasons() -> None:
    for key in ("ema_crossover_signal", "sma_crossover", "rsi_mean_reversion", "spy_strategy_a", "spy_strategy_b", "spy_strategy_c"):
        answer = sweep_eligibility(_STRATEGY_REGISTRY[key])
        assert answer.eligible, (key, answer)
        assert answer.reason_codes == ()
        assert answer.offending_parameters == ()


def test_the_operational_harness_is_excluded_by_category_not_by_a_list() -> None:
    answer = sweep_eligibility(_STRATEGY_REGISTRY["deployment_validation"])

    assert answer.eligible is False
    assert REASON_NOT_PRODUCTION_CANDIDATE in answer.reason_codes
    assert "deployment_validation" not in eligible_strategy_keys()


def test_a_non_numeric_public_parameter_excludes_and_is_named() -> None:
    class _MostlyNumeric(StrategyParamsBase):
        symbol: str = "SPY"
        fast_period: int = 5
        mode: Literal["a", "b"] = "a"
        label: str = "x"

    registration = replace(_STRATEGY_REGISTRY["sma_crossover"], param_schema=_MostlyNumeric)
    answer = sweep_eligibility(registration)

    assert answer.eligible is False
    assert answer.reason_codes == (REASON_NON_NUMERIC_PUBLIC_PARAMETER,)
    assert answer.offending_parameters == ("label", "mode")


def test_a_hidden_non_numeric_parameter_does_not_exclude() -> None:
    """The researcher cannot see or set a hidden parameter, so it cannot explain an exclusion."""

    class _WithHidden(StrategyParamsBase):
        symbol: str = "SPY"
        fast_period: int = 5
        trade_symbol: str = "SPY"

    registration = replace(_STRATEGY_REGISTRY["sma_crossover"], param_schema=_WithHidden, hidden_params={"trade_symbol"})

    assert sweep_eligibility(registration).eligible is True
    assert non_numeric_parameters(_WithHidden.model_json_schema()) == ("trade_symbol",)


def test_a_registration_without_a_signal_program_cannot_be_sized_and_is_excluded() -> None:
    registration = replace(_STRATEGY_REGISTRY["sma_crossover"], signal_program_factory=None)

    answer = sweep_eligibility(registration)

    assert answer.eligible is False
    assert answer.reason_codes == (REASON_NO_SIGNAL_PROGRAM,)
