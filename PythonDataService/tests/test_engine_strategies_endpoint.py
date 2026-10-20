"""Tests for the strategy registry and the list_engine_strategies handler.

This is the contract between the Engine Lab frontend strategy picker and
the Python backend registry.

These tests protect:
    1. The `orb` strategy is registered and wire-able end-to-end.
    2. The expected strategies remain registered.
    3. Each params_schema round-trips cleanly through JSON so the
       frontend's dynamic form renderer can consume it.

The handler is called directly rather than through a FastAPI TestClient,
because spinning up the full app pulls in Polygon/REST dependencies that
aren't needed to test the registry contract.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY

EXPECTED_STRATEGY_KEYS = {
    # Registry keys are the module names under ``app.engine.strategy.algorithms``.
    "ema_crossover_signal",
    "sma_crossover",
    "rsi_mean_reversion",
    "deployment_validation",
    "spy_strategy_a",
    "spy_strategy_b",
    "spy_strategy_c",
}


def _list_strategies():
    """Call the router's list_engine_strategies handler directly.

    Returns a list of dicts (the Pydantic StrategyInfo model_dumped).
    """
    from app.routers.engine import list_engine_strategies

    return [s.model_dump() for s in list_engine_strategies()]


def test_the_catalog_lists_the_registered_strategies_with_their_lean_twins():
    strategies = _list_strategies()
    names = {s["name"] for s in strategies}
    assert "deployment_validation" in names, "deployment_validation is registered"

    strategy = next(s for s in strategies if s["name"] == "deployment_validation")
    assert strategy["display_name"] == "Deployment Validation", "fixed-rule display name"
    assert "minute" in strategy["supported_resolutions"], "fixed-rule supports the minute resolution"
    assert strategy["lean_twin"] == "deployment_validation", "fixed-rule LEAN twin"
    props = strategy["params_schema"]["properties"]
    assert set(props) == {"symbol"}, "fixed-rule exposes only the symbol parameter"

    # Owner decision 7 (#2607): the note comes from DV's registry entry, never its sealed sources.
    notices = {s["name"]: s["experimental_notice"] for s in strategies}

    assert notices["deployment_validation"] == "Experimental validation only — not a trading strategy", (
        "#2607: DV carries the experimental notice from its registry entry"
    )
    assert {name for name, notice in notices.items() if notice is not None} == {"deployment_validation"}, (
        "#2607: DV is the only strategy that carries an experimental notice"
    )

    assert names >= EXPECTED_STRATEGY_KEYS, (
        f"registry regressed — at least one expected strategy is missing: {EXPECTED_STRATEGY_KEYS - names}"
    )

    by_name = {entry["name"]: entry for entry in strategies}
    assert by_name["ema_crossover_signal"]["lean_twin"] == "ema_crossover_signal", (
        "ema_signal advertises its matching LEAN validation template"
    )
    assert by_name["rsi_mean_reversion"]["lean_twin"] == "rsi_mean_reversion", (
        "rsi_mean_reversion advertises its matching LEAN validation template"
    )

    # Strategy Lab's LEAN-only run sends these settings and refuses the rest (#2904).
    for name, entry in by_name.items():
        registration = _STRATEGY_REGISTRY[name]
        assert entry["lean_parameter_names"] == list(registration.lean_parameter_names), (
            f"#2904: {name} names the settings its LEAN twin reads"
        )
        assert entry["lean_data_policy_parameter_names"] == list(registration.lean_data_policy_parameter_names), (
            f"#2904: {name} names the data-policy settings its LEAN twin reads"
        )
    # Both kinds must be on the wire, or the loop above proves nothing.
    assert by_name["ema_crossover_signal"]["lean_parameter_names"] == ["gap", "gap_bps", "rsi_min", "rsi_max"], (
        "#2904: ema_crossover_signal puts its LEAN parameter names on the wire"
    )
    assert by_name["rsi_mean_reversion"]["lean_data_policy_parameter_names"] == ["resolution_minutes"], (
        "#2904: rsi_mean_reversion puts its LEAN data-policy parameter names on the wire"
    )


def test_every_operational_harness_carries_an_experimental_notice():
    """A harness is never a trading strategy, so every one says so wherever it is shown."""
    for name, registration in _STRATEGY_REGISTRY.items():
        if registration.strategy_category == "operational_validation_harness":
            assert registration.experimental_notice, name


def test_deployment_validation_has_matching_spec_fixture():
    from app.routers.spec_strategy import list_fixtures

    fixtures = {f.name: f for f in list_fixtures()}
    assert "deployment_validation" in fixtures
    assert fixtures["deployment_validation"].path.endswith("deployment_validation.spec.json")


def test_an_engine_lab_backtest_refuses_a_parameter_its_strategy_hides(monkeypatch: pytest.MonkeyPatch):
    from dataclasses import replace

    from fastapi import HTTPException

    from app.schemas.engine_backtest import EngineBacktestRequest
    from app.services.engine_backtest_service import execute_engine_backtest

    # No registered strategy hides a parameter today, so the test hides one.
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    monkeypatch.setitem(_STRATEGY_REGISTRY, "sma_crossover", replace(registration, hidden_params={"long_window"}))
    request = EngineBacktestRequest(strategy_name="sma_crossover", params={"symbol": "SPY", "long_window": 40})

    with pytest.raises(HTTPException) as exc:
        execute_engine_backtest(request=request, on_phase=lambda _phase: None, on_log=lambda _line: None)

    assert exc.value.status_code == 400
    assert exc.value.detail["params_errors"][0]["loc"] == ["params", "long_window"]


def test_ema_signal_exposes_the_normalized_gap_floor_as_a_parameter() -> None:
    """The former ``ema_crossover_2_bps`` strategy is now a parameter mode.

    Its normalized-gap gate survives the signal/asset decoupling sweep as
    ``gap_bps`` on the canonical registration, so the knob stays
    reachable from Strategy Lab without a second registry entry whose only
    difference was one predicate.
    """
    strategy = next(s for s in _list_strategies() if s["name"] == "ema_crossover_signal")
    properties = strategy["params_schema"]["properties"]

    assert properties["gap_bps"]["default"] == 0.0
    assert properties["gap_bps"]["minimum"] == 0.0
    assert properties["gap_bps"]["maximum"] == 100.0
    # The validated LEAN-parity point is untouched by the fold.
    assert properties["gap"]["default"] == 0.20
    assert properties["rsi_min"]["default"] == 50.0
    assert properties["rsi_max"]["default"] == 70.0


def test_ema_signal_parameter_model_rejects_invalid_rsi_band() -> None:
    from pydantic import ValidationError

    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]

    with pytest.raises(ValidationError, match="rsi_min must be less than rsi_max"):
        registration.param_schema.model_validate(
            {"symbol": "SPY", "gap_bps": 2, "rsi_min": 70, "rsi_max": 50}
        )


def test_ema_signal_registry_build_forwards_the_normalized_gap_parameters() -> None:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    params = registration.param_schema.model_validate(
        {"symbol": "SPY", "gap": 0, "gap_bps": 4, "rsi_min": 52, "rsi_max": 68}
    )

    strategy = registration.build(params)

    assert strategy._gap_bps == Decimal("4")
    assert strategy._rsi_gate_bounds() == (Decimal("52"), Decimal("68"))


def test_the_catalog_advertises_json_params_recency_and_bar_cadence_to_the_picker():
    strategies = _list_strategies()
    by_name = {entry["name"]: entry for entry in strategies}

    # params_schema must be plain JSON the frontend can consume.
    for entry in strategies:
        dumped = json.dumps(entry["params_schema"])
        assert json.loads(dumped) == entry["params_schema"], f"{entry['name']}: params_schema round-trips through JSON"

    # Recency Chart eligibility — numeric-param strategies.
    for key in ("ema_crossover_signal", "sma_crossover", "rsi_mean_reversion"):
        assert by_name[key]["recency_supported"] is True, f"{key}: numeric-param strategies are recency-supported"

    assert by_name["ema_crossover_signal"]["strategy_bars"] == {
        "timespan": "minute",
        "multiplier": 15,
        "parameter": None,
    }, "the registry serves ema_crossover_signal's bar cadence, with no frontend name heuristics"
    assert by_name["sma_crossover"]["strategy_bars"] == {
        "timespan": "minute",
        "multiplier": 15,
        "parameter": "resolution_minutes",
    }, "the registry serves sma_crossover's bar cadence and the parameter that moves it"
