"""Strategy registry execution-surface contracts remain explicit.

PRD #593 §"The instrument-surface registry flag" introduced the policy surface.
Every live-runtime Action Plan and signal-intent binding is enumerated here so a
registry change cannot silently alter execution behavior.

Prior art: ``test_run_cli.test_lookup_sizing_surface_resolves_module_name_to_registry_key``.
"""

from __future__ import annotations

import pytest

from app.routers.engine import _STRATEGY_REGISTRY

_POLICY_STRATEGIES = {
    "ema_crossover_2_bps",
    "ema_crossover_signal",
    "rsi_mean_reversion",
    "sma_crossover",
    "spy_strategy_a",
    "spy_strategy_b",
    "spy_strategy_c",
}
_SINGLE_LONG_STOCK_ACTION_PLAN_STRATEGIES = {
    "deployment_validation",
    "ema_crossover_2_bps",
    "ema_crossover_signal",
    "rsi_mean_reversion",
    "sma_crossover",
    "spy_strategy_a",
    "spy_strategy_b",
    "spy_strategy_c",
}
_ACTION_PLAN_SIGNAL_INTENT_STRATEGIES = {
    "ema_crossover_2_bps",
    "ema_crossover_signal",
    "rsi_mean_reversion",
    "sma_crossover",
    "spy_strategy_a",
    "spy_strategy_b",
    "spy_strategy_c",
}
_SIGNAL_SYMBOL_INTENT_STRATEGIES = {"spy_ema_crossover"}


@pytest.mark.parametrize("strategy_key", sorted(_STRATEGY_REGISTRY.keys()))
def test_every_registered_strategy_declares_its_execution_surface(
    strategy_key: str,
) -> None:
    reg = _STRATEGY_REGISTRY[strategy_key]

    expected = "policy" if strategy_key in _POLICY_STRATEGIES else "explicit"
    assert reg.instrument_surface == expected


@pytest.mark.parametrize("strategy_key", sorted(_STRATEGY_REGISTRY.keys()))
def test_every_registered_strategy_declares_its_action_plan_contract(strategy_key: str) -> None:
    reg = _STRATEGY_REGISTRY[strategy_key]

    expected = "single_long_stock" if strategy_key in _SINGLE_LONG_STOCK_ACTION_PLAN_STRATEGIES else "none"
    assert reg.action_plan_contract == expected


@pytest.mark.parametrize("strategy_key", sorted(_STRATEGY_REGISTRY.keys()))
def test_every_registered_strategy_declares_its_signal_intent_binding(strategy_key: str) -> None:
    reg = _STRATEGY_REGISTRY[strategy_key]

    if strategy_key in _ACTION_PLAN_SIGNAL_INTENT_STRATEGIES:
        expected = "action_plan_stock"
    elif strategy_key in _SIGNAL_SYMBOL_INTENT_STRATEGIES:
        expected = "signal_symbol"
    else:
        expected = "none"
    assert reg.signal_intent_binding == expected
