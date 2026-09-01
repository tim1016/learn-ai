"""Verify every bundled LEAN template is registered and wired to a strategy."""

from __future__ import annotations

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trusted_samples.deployment_validation import (
    DEPLOYMENT_VALIDATION_SOURCE,
)
from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.lean_sidecar.trusted_samples.ema_crossover_2_bps import (
    EMA_CROSSOVER_2_BPS_SOURCE,
)
from app.lean_sidecar.trusted_samples.ema_crossover_signal import (
    EMA_CROSSOVER_SIGNAL_SOURCE,
)
from app.lean_sidecar.trusted_samples.rsi_mean_reversion import (
    RSI_MEAN_REVERSION_SOURCE,
)
from app.lean_sidecar.trusted_templates import (
    TRUSTED_TEMPLATE_DEFINITIONS,
    TrustedTemplate,
)
from app.services.strategy_lean_source_service import resolve_strategy_lean_source


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


def test_ema_crossover_two_bps_is_in_source_registry() -> None:
    definition = TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.EMA_CROSSOVER_2_BPS]

    assert definition.source is EMA_CROSSOVER_2_BPS_SOURCE
    assert definition.brokerage_policy == "interactive_brokers"


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


def test_rsi_mean_reversion_is_in_source_registry() -> None:
    definition = TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.RSI_MEAN_REVERSION]

    assert definition.source is RSI_MEAN_REVERSION_SOURCE
    assert definition.brokerage_policy == "interactive_brokers"


# ----------------------------------------------------------------------
# Wiring completeness. The per-template assertions above are hand-written,
# so before these a template could be added and silently never wired to a
# strategy -- `lean_twin` would stay None and the strategy would drop out of
# Strategy Lab's LEAN-validatable list with nothing failing.
#
# Deliberately NOT auto-derived: computing `lean_twin` from a name match
# would make "a template shares this strategy's name" mean "these two
# implementations produce the same trades", which is a numerical claim and
# needs a receipt (CLAUDE.md guiding philosophy #2), not a string compare.
# ----------------------------------------------------------------------

#: Templates that legitimately have no registry strategy pointing at them.
#: Every entry needs a reason; the default for a new template is to be
#: claimed by a strategy, not to be added here.
_TEMPLATES_WITHOUT_A_REGISTRY_STRATEGY: frozenset[TrustedTemplate] = frozenset(
    {
        # Infrastructure harnesses, not strategy twins: buy-and-hold probes
        # used to validate the sidecar itself and the IBKR fee/fill wiring.
        TrustedTemplate.TRUSTED_DEFAULT,
        TrustedTemplate.RECONCILIATION,
        # The legacy base source. `ema_crossover_signal` IS this object
        # (EMA_CROSSOVER_SIGNAL_SOURCE is EMA_CROSSOVER_SOURCE, asserted
        # above) and is the name strategies claim, so the base name being
        # unclaimed is the intended aliasing, not drift.
        TrustedTemplate.EMA_CROSSOVER,
        # Its strategy was retired, not lost: the former `ema_crossover_2_bps`
        # registration was folded into `ema_crossover_signal` as the `gap_bps`
        # parameter mode (see
        # test_engine_strategies_endpoint.test_ema_signal_exposes_the_normalized_gap_floor_as_a_parameter).
        # The template outlived it and is now reachable only through the
        # Engine Lab trusted-run surface, which takes `template` directly.
        # It cannot be claimed by `ema_crossover_signal` without asserting
        # that a 2-bps twin validates a strategy whose gate is configurable
        # -- a parity claim needing its own receipt, not a rename.
        TrustedTemplate.EMA_CROSSOVER_2_BPS,
    }
)


def test_every_declared_lean_twin_resolves_to_a_bundled_template() -> None:
    """A strategy may not point at a template that does not exist."""
    declared = {
        name: registration.lean_twin
        for name, registration in _STRATEGY_REGISTRY.items()
        if registration.lean_twin is not None
    }

    assert declared, "expected at least one strategy to declare a LEAN twin"

    for strategy_name, twin in declared.items():
        assert twin in set(TrustedTemplate), f"{strategy_name} declares unknown LEAN template {twin!r}"
        resolved = resolve_strategy_lean_source(strategy_name)
        assert resolved.template == twin
        assert "class MyAlgorithm(QCAlgorithm)" in resolved.source


def test_every_strategy_twin_template_is_claimed_by_a_strategy() -> None:
    """A bundled strategy twin must be reachable from the registry.

    Fails when a template is added without wiring it to a strategy. The
    escape hatch is deliberate and documented, not silent: put it in
    ``_TEMPLATES_WITHOUT_A_REGISTRY_STRATEGY`` with a reason.
    """
    claimed = {
        registration.lean_twin
        for registration in _STRATEGY_REGISTRY.values()
        if registration.lean_twin is not None
    }

    unclaimed = {
        template
        for template in TrustedTemplate
        if template.value not in claimed and template not in _TEMPLATES_WITHOUT_A_REGISTRY_STRATEGY
    }

    assert not unclaimed, (
        f"bundled LEAN templates claimed by no strategy: {sorted(t.value for t in unclaimed)}. "
        "Set lean_twin on the strategy they validate, or document the exemption in "
        "_TEMPLATES_WITHOUT_A_REGISTRY_STRATEGY."
    )


def test_exemption_list_does_not_name_a_claimed_template() -> None:
    """The escape hatch must not outlive the gap it documents."""
    claimed = {
        registration.lean_twin
        for registration in _STRATEGY_REGISTRY.values()
        if registration.lean_twin is not None
    }

    stale = {t.value for t in _TEMPLATES_WITHOUT_A_REGISTRY_STRATEGY if t.value in claimed}

    assert not stale, f"exempted templates are now claimed; drop them from the exemption list: {sorted(stale)}"


#: Registered strategies that have no LEAN validation twin, and why.
#:
#: This list is the answer to "why is `lean_twin` None here?" -- it makes the
#: absence a reviewed decision instead of a silent default. It is what the two
#: tests above cannot cover: they detect a template that drifted loose from a
#: strategy, but nothing in the code can distinguish "twin deliberately not
#: built" from "twin forgotten". Only an explicit list can.
_STRATEGIES_WITHOUT_A_LEAN_TWIN: frozenset[str] = frozenset(
    {
        # No QCAlgorithm twin authored yet. Each needs its own trusted sample
        # mirroring its spec, plus a reconciliation receipt under
        # docs/references/reconciliations/, before it can claim one.
        "sma_crossover",
        "spy_strategy_a",
        "spy_strategy_b",
        "spy_strategy_c",
    }
)


def test_every_strategy_either_has_a_twin_or_documents_why_not() -> None:
    """A new strategy must consciously declare its LEAN parity status.

    Without this, `lean_twin=None` is indistinguishable from an oversight --
    which is exactly how rsi_mean_reversion sat un-twinned while its Python
    side was fully sealed and validated.
    """
    untwinned = {
        name
        for name, registration in _STRATEGY_REGISTRY.items()
        if registration.lean_twin is None and name not in _STRATEGIES_WITHOUT_A_LEAN_TWIN
    }

    assert not untwinned, (
        f"strategies with no LEAN twin and no documented reason: {sorted(untwinned)}. "
        "Author a trusted sample and set lean_twin, or add the strategy to "
        "_STRATEGIES_WITHOUT_A_LEAN_TWIN with the reason."
    )


def test_untwinned_list_does_not_name_a_twinned_strategy() -> None:
    """The escape hatch must not outlive the gap it documents."""
    stale = {
        name
        for name in _STRATEGIES_WITHOUT_A_LEAN_TWIN
        if (registration := _STRATEGY_REGISTRY.get(name)) is not None and registration.lean_twin is not None
    }

    assert not stale, f"these strategies now have twins; drop them from the list: {sorted(stale)}"


def test_untwinned_list_does_not_name_an_unregistered_strategy() -> None:
    """A renamed or deleted strategy must not linger as a phantom exemption."""
    phantom = {name for name in _STRATEGIES_WITHOUT_A_LEAN_TWIN if name not in _STRATEGY_REGISTRY}

    assert not phantom, f"unknown strategies in the exemption list: {sorted(phantom)}"
