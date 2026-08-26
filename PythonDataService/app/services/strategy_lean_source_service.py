"""Resolve versioned LEAN validation source without contacting the launcher."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trusted_templates import TrustedTemplate, trusted_template_definition


class StrategyLeanSourceNotFoundError(LookupError):
    """The requested strategy has no registered LEAN source."""


@dataclass(frozen=True)
class StrategyLeanSource:
    strategy_name: str
    template: str
    source: str
    source_sha256: str


def resolve_strategy_lean_source(strategy_name: str) -> StrategyLeanSource:
    """Return the registered source without probing any LEAN runtime."""

    registration = _STRATEGY_REGISTRY.get(strategy_name)
    if registration is None:
        raise StrategyLeanSourceNotFoundError(f"Unknown strategy: {strategy_name}")
    if registration.lean_twin is None:
        raise StrategyLeanSourceNotFoundError(
            f"Strategy '{strategy_name}' has no registered LEAN validation source"
        )
    try:
        template = TrustedTemplate(registration.lean_twin)
    except ValueError as exc:
        raise StrategyLeanSourceNotFoundError(
            f"Strategy '{strategy_name}' references unknown LEAN template '{registration.lean_twin}'"
        ) from exc
    source = trusted_template_definition(template).source
    return StrategyLeanSource(
        strategy_name=strategy_name,
        template=template.value,
        source=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )
