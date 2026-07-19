"""Authoritative definitions for every bundled LEAN trusted template."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from app.lean_sidecar.manifest import BrokeragePolicy
from app.lean_sidecar.trusted_samples.buy_and_hold import BUY_AND_HOLD_SOURCE
from app.lean_sidecar.trusted_samples.buy_and_hold_reconciliation import (
    BUY_AND_HOLD_RECONCILIATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.deployment_validation import DEPLOYMENT_VALIDATION_SOURCE
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.lean_sidecar.trusted_samples.ema_crossover_signal import EMA_CROSSOVER_SIGNAL_SOURCE


class TrustedTemplate(StrEnum):
    """Stable wire names for bundled LEAN algorithm sources."""

    TRUSTED_DEFAULT = "trusted_default"
    RECONCILIATION = "reconciliation"
    EMA_CROSSOVER = "ema_crossover"
    EMA_CROSSOVER_SIGNAL = "ema_crossover_signal"
    DEPLOYMENT_VALIDATION = "deployment_validation"


@dataclass(frozen=True)
class TrustedTemplateDefinition:
    """The source and brokerage semantics that must move together."""

    source: str
    brokerage_policy: BrokeragePolicy


TRUSTED_TEMPLATE_DEFINITIONS: Final[Mapping[TrustedTemplate, TrustedTemplateDefinition]] = MappingProxyType(
    {
        TrustedTemplate.TRUSTED_DEFAULT: TrustedTemplateDefinition(
            source=BUY_AND_HOLD_SOURCE,
            brokerage_policy="algorithm_default",
        ),
        TrustedTemplate.RECONCILIATION: TrustedTemplateDefinition(
            source=BUY_AND_HOLD_RECONCILIATION_SOURCE,
            brokerage_policy="interactive_brokers",
        ),
        TrustedTemplate.EMA_CROSSOVER: TrustedTemplateDefinition(
            source=EMA_CROSSOVER_SOURCE,
            brokerage_policy="algorithm_default",
        ),
        TrustedTemplate.EMA_CROSSOVER_SIGNAL: TrustedTemplateDefinition(
            source=EMA_CROSSOVER_SIGNAL_SOURCE,
            brokerage_policy="interactive_brokers",
        ),
        TrustedTemplate.DEPLOYMENT_VALIDATION: TrustedTemplateDefinition(
            source=DEPLOYMENT_VALIDATION_SOURCE,
            brokerage_policy="algorithm_default",
        ),
    }
)


def trusted_template_definition(template: TrustedTemplate) -> TrustedTemplateDefinition:
    """Return the one source/brokerage contract for a bundled template."""

    return TRUSTED_TEMPLATE_DEFINITIONS[template]
