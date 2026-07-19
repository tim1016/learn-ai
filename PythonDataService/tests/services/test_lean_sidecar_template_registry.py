"""Verify the legacy and migrated EMA templates are registered."""

from __future__ import annotations

from app.lean_sidecar.trusted_samples.deployment_validation import (
    DEPLOYMENT_VALIDATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.lean_sidecar.trusted_samples.ema_crossover_signal import (
    EMA_CROSSOVER_SIGNAL_SOURCE,
)
from app.lean_sidecar.trusted_templates import (
    TRUSTED_TEMPLATE_DEFINITIONS,
    TrustedTemplate,
)


def test_ema_crossover_is_in_source_registry() -> None:
    assert TrustedTemplate.EMA_CROSSOVER in TRUSTED_TEMPLATE_DEFINITIONS
    assert TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.EMA_CROSSOVER].source is EMA_CROSSOVER_SOURCE


def test_ema_crossover_brokerage_policy_is_algorithm_default() -> None:
    assert TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.EMA_CROSSOVER].brokerage_policy == "algorithm_default"


def test_ema_crossover_signal_is_in_source_registry() -> None:
    assert TrustedTemplate.EMA_CROSSOVER_SIGNAL in TRUSTED_TEMPLATE_DEFINITIONS
    assert TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.EMA_CROSSOVER_SIGNAL].source is EMA_CROSSOVER_SIGNAL_SOURCE
    assert EMA_CROSSOVER_SIGNAL_SOURCE is EMA_CROSSOVER_SOURCE


def test_ema_crossover_signal_brokerage_policy_is_interactive_brokers() -> None:
    assert (
        TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.EMA_CROSSOVER_SIGNAL].brokerage_policy
        == "interactive_brokers"
    )


def test_deployment_validation_is_in_source_registry() -> None:
    assert TrustedTemplate.DEPLOYMENT_VALIDATION in TRUSTED_TEMPLATE_DEFINITIONS
    assert TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.DEPLOYMENT_VALIDATION].source is DEPLOYMENT_VALIDATION_SOURCE


def test_deployment_validation_brokerage_policy_is_algorithm_default() -> None:
    assert (
        TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.DEPLOYMENT_VALIDATION].brokerage_policy
        == "algorithm_default"
    )


def test_existing_templates_still_registered() -> None:
    """Regression guard: don't break existing templates."""
    assert TrustedTemplate.TRUSTED_DEFAULT in TRUSTED_TEMPLATE_DEFINITIONS
    assert TrustedTemplate.RECONCILIATION in TRUSTED_TEMPLATE_DEFINITIONS
