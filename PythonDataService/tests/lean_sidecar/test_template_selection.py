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


def test_reconciliation_template_definition_and_source() -> None:
    definition = TRUSTED_TEMPLATE_DEFINITIONS[TrustedTemplate.RECONCILIATION]

    # Manifest's brokerage_policy field is what the Phase 5a reconciler
    # UI displays — and what an auditor reads to know whether a run is
    # Engine-Lab-comparable.
    assert definition.brokerage_policy == "interactive_brokers", (
        "reconciliation template maps exactly to the interactive_brokers brokerage_policy"
    )
    assert definition.source == BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "reconciliation template stages the IBKR-pinned source"
    )
    # Regression catch: removing the SetBrokerageModel call makes the fee
    # reconciler silently start producing drift again.
    assert "SetBrokerageModel" in BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "regression catch: reconciliation source keeps the SetBrokerageModel call"
    )
    assert "InteractiveBrokersBrokerage" in BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "regression catch: reconciliation source pins InteractiveBrokersBrokerage"
    )
    assert "AccountType.Margin" in BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "regression catch: reconciliation source pins AccountType.Margin"
    )
    assert "fillForward=False" in BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "ADR invariant #13: reconciliation-grade subscriptions disable fill-forward"
    )
    assert "DataNormalizationMode.Raw" in BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "ADR invariant #14: reconciliation-grade subscriptions pin Raw normalization (matches Engine Lab)"
    )
    # LeanConfig.algorithm_type_name defaults to ``MyAlgorithm`` — if the
    # class is renamed, LEAN silently runs its image-baked default and the
    # run looks successful with empty output.
    assert "class MyAlgorithm" in BUY_AND_HOLD_RECONCILIATION_SOURCE, (
        "reconciliation source class is named MyAlgorithm (LeanConfig.algorithm_type_name default)"
    )
