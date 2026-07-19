"""Slice 1A — strategy registry exposes ``instrument_surface``.

PRD #593 §"The instrument-surface registry flag" — the migrated EMA strategy
is the first policy-surface registration. Its Action Plan is consumed by the
live runtime; legacy strategies remain explicit.

Prior art: ``test_run_cli.test_lookup_sizing_surface_resolves_module_name_to_registry_key``.
"""

from __future__ import annotations

import pytest

from app.routers.engine import _STRATEGY_REGISTRY

_POLICY_STRATEGIES = {"ema_crossover_signal"}
_SINGLE_LONG_STOCK_ACTION_PLAN_STRATEGIES = {"deployment_validation", "ema_crossover_signal"}


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
