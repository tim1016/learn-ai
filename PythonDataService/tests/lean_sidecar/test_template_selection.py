"""Phase 5b — service-layer tests for the trusted-template selector.

These tests don't spin up the launcher; they assert the pure-Python
mappings between ``TrustedRunRequest.template`` and the manifest's
``brokerage_policy`` + the source string we stage. The end-to-end
"reconciliation template produces a clean fee report" assertion lives
in the E2E suite gated on a real LEAN image.
"""

from __future__ import annotations

from app.lean_sidecar.trusted_samples.buy_and_hold_reconciliation import (
    BUY_AND_HOLD_RECONCILIATION_SOURCE,
)
from app.lean_sidecar.trusted_templates import (
    TRUSTED_TEMPLATE_DEFINITIONS,
    TrustedTemplate,
)


def test_template_maps_reconciliation_to_interactive_brokers_policy() -> None:
    """Manifest's brokerage_policy field is what the Phase 5a reconciler
    UI displays — and what an auditor reads to know whether a run is
    Engine-Lab-comparable. Reconciliation template must map exactly
    to ``interactive_brokers``."""
    assert TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.RECONCILIATION].brokerage_policy == "interactive_brokers"


def test_reconciliation_template_stages_ibkr_pinned_source() -> None:
    assert TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.RECONCILIATION].source == BUY_AND_HOLD_RECONCILIATION_SOURCE


def test_reconciliation_source_explicitly_pins_ibkr_brokerage() -> None:
    """Regression catch: if someone edits the reconciliation template
    and accidentally removes the SetBrokerageModel call, the fee
    reconciler will silently start producing drift again. This test
    asserts the source string contains the pin verbatim."""
    assert "SetBrokerageModel" in BUY_AND_HOLD_RECONCILIATION_SOURCE
    assert "InteractiveBrokersBrokerage" in BUY_AND_HOLD_RECONCILIATION_SOURCE
    assert "AccountType.Margin" in BUY_AND_HOLD_RECONCILIATION_SOURCE


def test_reconciliation_source_keeps_filldforward_false() -> None:
    """ADR invariant #13: reconciliation-grade subscriptions must
    disable fill-forward. Catch a future edit that removes the flag."""
    assert "fillForward=False" in BUY_AND_HOLD_RECONCILIATION_SOURCE


def test_reconciliation_source_keeps_raw_normalization_mode() -> None:
    """ADR invariant #14: reconciliation-grade subscriptions pin
    normalization mode. Raw is what matches Engine Lab."""
    assert "DataNormalizationMode.Raw" in BUY_AND_HOLD_RECONCILIATION_SOURCE


def test_reconciliation_source_class_name_is_my_algorithm() -> None:
    """LeanConfig.algorithm_type_name defaults to ``MyAlgorithm`` — if
    we rename the class, LEAN silently runs its image-baked default
    and the run looks successful with empty output."""
    assert "class MyAlgorithm" in BUY_AND_HOLD_RECONCILIATION_SOURCE
