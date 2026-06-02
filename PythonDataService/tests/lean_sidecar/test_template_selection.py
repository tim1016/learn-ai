"""Phase 5b — service-layer tests for the trusted-template selector.

These tests don't spin up the launcher; they assert the pure-Python
mappings between ``TrustedRunRequest.template`` and the manifest's
``brokerage_policy`` + the source string we stage. The end-to-end
"reconciliation template produces a clean fee report" assertion lives
in the E2E suite gated on a real LEAN image.
"""

from __future__ import annotations

import pytest

from app.lean_sidecar.data_policy import BarsSpec, DataPolicy
from app.lean_sidecar.trusted_samples.buy_and_hold import BUY_AND_HOLD_SOURCE
from app.lean_sidecar.trusted_samples.buy_and_hold_reconciliation import (
    BUY_AND_HOLD_RECONCILIATION_SOURCE,
)
from app.services.lean_sidecar_service import (
    _BROKERAGE_POLICY_FOR_TEMPLATE,
    _SOURCE_FOR_TEMPLATE,
    TrustedRunRequest,
)


def _default_data_policy() -> DataPolicy:
    return DataPolicy(
        source="synthetic",
        symbol="SPY",
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )


def _request(**overrides) -> TrustedRunRequest:
    base = {
        "run_id": "ut_template",
        "start_ms_utc": 1_736_121_600_000,
        "end_ms_utc": 1_736_467_200_000,
        "starting_cash": 100_000.0,
        "data_policy": _default_data_policy(),
    }
    base.update(overrides)
    return TrustedRunRequest(**base)


def test_trusted_default_template_is_the_dataclass_default() -> None:
    """The Phase 1/4c API must keep working unchanged — old callers
    that never sent a template field should keep their pre-Phase-5b
    behavior (LEAN default brokerage)."""
    req = _request()
    assert req.template == "trusted_default"


def test_template_maps_default_to_algorithm_default_policy() -> None:
    assert _BROKERAGE_POLICY_FOR_TEMPLATE["trusted_default"] == "algorithm_default"


def test_template_maps_reconciliation_to_interactive_brokers_policy() -> None:
    """Manifest's brokerage_policy field is what the Phase 5a reconciler
    UI displays — and what an auditor reads to know whether a run is
    Engine-Lab-comparable. Reconciliation template must map exactly
    to ``interactive_brokers``."""
    assert _BROKERAGE_POLICY_FOR_TEMPLATE["reconciliation"] == "interactive_brokers"


def test_default_template_stages_legacy_buy_and_hold_source() -> None:
    assert _SOURCE_FOR_TEMPLATE["trusted_default"] == BUY_AND_HOLD_SOURCE


def test_reconciliation_template_stages_ibkr_pinned_source() -> None:
    assert _SOURCE_FOR_TEMPLATE["reconciliation"] == BUY_AND_HOLD_RECONCILIATION_SOURCE


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


@pytest.mark.parametrize("template", ["trusted_default", "reconciliation", "ema_crossover", "deployment_validation"])
def test_request_accepts_known_templates(template: str) -> None:
    req = _request(template=template)
    assert req.template == template
