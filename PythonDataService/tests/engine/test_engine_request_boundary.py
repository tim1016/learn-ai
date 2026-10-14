"""The engine backtest request refuses what can only fail late or hold the engine gate (#3219).

Each of these used to pass the schema: ``to_date="9999-12-31"`` walked every
day to an ``OverflowError`` after about 21 s with the one-backtest gate held,
``Infinity`` passed ``ge=0``, ``initial_cash=0`` failed every run at its first
equity point, and ``2024-02-30`` matched the date pattern and answered 200
``success=false`` or 400 by the data path. The wire shape is unchanged: the
OpenAPI contract check (``scripts/export_openapi_contract.py --check``) still
passes, because these are validators, not new bounds.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.engine_backtest import EngineBacktestRequest


def _request(**overrides: Any) -> EngineBacktestRequest:
    return EngineBacktestRequest.model_validate({"strategy_name": "ema_crossover_signal", **overrides})


def test_a_well_formed_request_is_accepted() -> None:
    request = _request(from_date="2024-02-29", to_date="2024-03-15", initial_cash=50_000.0, commission_per_order=1.0)

    assert (request.from_date, request.to_date, request.initial_cash) == ("2024-02-29", "2024-03-15", 50_000.0)


@pytest.mark.parametrize("field", ["from_date", "to_date", "warmup_from_date"])
def test_a_date_that_matches_the_pattern_but_is_not_on_the_calendar_is_refused(field: str) -> None:
    overrides: dict[str, Any] = {"from_date": "2024-03-01", "to_date": "2024-03-15", field: "2024-02-30"}

    with pytest.raises(ValidationError, match="not a calendar date"):
        _request(**overrides)


def test_a_window_ending_past_the_last_schedulable_date_is_refused_before_any_schedule_is_built() -> None:
    with pytest.raises(ValidationError, match="last date the NYSE calendar can schedule"):
        _request(from_date="2024-01-02", to_date="9999-12-31")


@pytest.mark.parametrize("field", ["commission_per_order", "slippage_per_share", "initial_cash"])
@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_a_money_field_that_is_not_finite_is_refused(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _request(**{field: value})


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_starting_cash_must_be_greater_than_zero(value: float) -> None:
    with pytest.raises(ValidationError):
        _request(initial_cash=value)


def test_a_params_rule_the_deploy_path_enforces_refuses_the_backtest_too() -> None:
    """MACD fast >= slow was refused at Deploy only; Engine Lab built the strategy and failed in initialize."""
    from fastapi import HTTPException

    from app.engine.strategy.registry import _STRATEGY_REGISTRY
    from app.services.engine_backtest_service import execute_engine_backtest

    key = "spy_strategy_a"
    assert _STRATEGY_REGISTRY[key].params_refusal is not None
    request = EngineBacktestRequest.model_validate(
        {"strategy_name": key, "params": {"symbol": "SPY", "macd_fast": 30, "macd_slow": 26}}
    )

    with pytest.raises(HTTPException) as refused:
        execute_engine_backtest(request=request, on_phase=lambda _phase: None, on_log=lambda _line: None)

    assert refused.value.status_code == 400
    assert "macd_fast (30) must be less than macd_slow (26)" in str(refused.value.detail["params_refusal"])
