"""Verify ema_crossover is registered alongside trusted_default and reconciliation."""

from __future__ import annotations

from app.lean_sidecar.trusted_samples.deployment_validation import (
    DEPLOYMENT_VALIDATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.services.lean_sidecar_service import (
    _BROKERAGE_POLICY_FOR_TEMPLATE,
    _SOURCE_FOR_TEMPLATE,
)


def test_ema_crossover_is_in_source_registry() -> None:
    assert "ema_crossover" in _SOURCE_FOR_TEMPLATE
    assert _SOURCE_FOR_TEMPLATE["ema_crossover"] is EMA_CROSSOVER_SOURCE


def test_ema_crossover_brokerage_policy_is_algorithm_default() -> None:
    assert _BROKERAGE_POLICY_FOR_TEMPLATE["ema_crossover"] == "algorithm_default"


def test_deployment_validation_is_in_source_registry() -> None:
    assert "deployment_validation" in _SOURCE_FOR_TEMPLATE
    assert _SOURCE_FOR_TEMPLATE["deployment_validation"] is DEPLOYMENT_VALIDATION_SOURCE


def test_deployment_validation_brokerage_policy_is_algorithm_default() -> None:
    assert _BROKERAGE_POLICY_FOR_TEMPLATE["deployment_validation"] == "algorithm_default"


def test_existing_templates_still_registered() -> None:
    """Regression guard: don't break existing templates."""
    assert "trusted_default" in _SOURCE_FOR_TEMPLATE
    assert "reconciliation" in _SOURCE_FOR_TEMPLATE
