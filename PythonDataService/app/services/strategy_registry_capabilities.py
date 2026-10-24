"""The strategy registry's account of itself: every entry and what it allows (#3508).

Each answer is derived from the one authority that owns the fact, never
re-decided here: sweep eligibility from ``app.research.sweep.eligibility``,
Golden Search from its declarations, and a bot's runtime from
``supported_alpaca_paper_strategy_keys``. The Live ceiling for an operational
validation harness is ADR 0044. An allowed action is not an approved one:
proof, qualification and admission stay with their own reads.
"""

from __future__ import annotations

from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration
from app.research.golden_search.declarations import unavailable_reason as golden_unavailable_reason
from app.research.sweep.eligibility import ineligibility_reason, sweep_eligibility
from app.schemas.strategy_registry import CapabilityAnswer, StrategyCapabilities, StrategyRegistryEntry
from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys

NO_RUNTIME_REASON = "No live-decision runtime is registered for this strategy, so no bot can run it."
HARNESS_LIVE_CEILING_REASON = (
    "An operational validation harness runs in Dry Run and Paper only; it stays outside Live."
)


def _answer(reason: str | None) -> CapabilityAnswer:
    return CapabilityAnswer(supported=reason is None, reason=reason or None)


def _sweep(registration: StrategyRegistration) -> CapabilityAnswer:
    reason = ineligibility_reason(sweep_eligibility(registration))
    # A refusal always carries words: an eligibility with no copy for its code still says it refuses.
    return _answer(None if reason is None else reason or "This strategy is not eligible for a sweep.")


def _golden(strategy_key: str) -> CapabilityAnswer:
    reason = golden_unavailable_reason(strategy_key)
    return _answer(None if reason is None else reason or "Golden Search cannot run this strategy.")


def _live(strategy_key: str, registration: StrategyRegistration) -> CapabilityAnswer:
    if strategy_key not in supported_alpaca_paper_strategy_keys():
        return _answer(NO_RUNTIME_REASON)
    if registration.strategy_category == "operational_validation_harness":
        return _answer(HARNESS_LIVE_CEILING_REASON)
    return _answer(None)


def _entry(strategy_key: str, registration: StrategyRegistration) -> StrategyRegistryEntry:
    contract = registration.signal_program_contract
    return StrategyRegistryEntry(
        strategy_key=strategy_key,
        display_name=registration.display_name,
        description=registration.description.strip(),
        strategy_category=registration.strategy_category,
        experimental_notice=registration.experimental_notice,
        catalog_visible=registration.catalog_visible,
        program_version=None if contract is None else contract.program_version,
        rule_statement=None if contract is None else contract.numerical_provenance.formula,
        validated_settings={} if contract is None else dict(contract.validated_settings),
        validated_symbols=[] if contract is None else list(contract.validated_symbols),
        parameter_units={} if contract is None else dict(contract.parameter_units),
        capabilities=StrategyCapabilities(
            sweep=_sweep(registration),
            golden=_golden(strategy_key),
            live=_live(strategy_key, registration),
        ),
    )


def strategy_registry_entries() -> list[StrategyRegistryEntry]:
    """Every registered strategy, hidden compatibility entries included, sorted by key."""
    return [_entry(key, _STRATEGY_REGISTRY[key]) for key in sorted(_STRATEGY_REGISTRY)]
