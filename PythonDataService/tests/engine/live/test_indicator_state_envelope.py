"""Tests for IndicatorStateEnvelope, IndicatorStatePayload, HydratePolicy, HydrationReceipt."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.engine.live.indicator_state import (
    HydratePolicy,
    HydrationReceipt,
    IndicatorStateEnvelope,
    ValidationResult,
)


def _valid_envelope_dict() -> dict:
    return {
        "schema_version": 1,
        "strategy_key": "spy_ema_crossover",
        "symbol": "SPY",
        "consolidator_period_min": 15,
        "last_consolidated_bar_end_ms": 1747166100000,
        "captured_at_ms": 1747166107842,
        "captured_reason": "force_flat",
        "code_sha": "abc123",
        "strategy_spec_sha": "def456",
        "payload": {"ema5": {"is_ready": True, "samples": 18}},
    }


def test_envelope_round_trip_via_json() -> None:
    env = IndicatorStateEnvelope.model_validate(_valid_envelope_dict())
    serialized = env.model_dump_json()
    parsed_back = IndicatorStateEnvelope.model_validate_json(serialized)
    assert parsed_back == env


def test_envelope_rejects_schema_version_other_than_1() -> None:
    bad = _valid_envelope_dict()
    bad["schema_version"] = 2
    with pytest.raises(ValidationError):
        IndicatorStateEnvelope.model_validate(bad)


def test_envelope_rejects_unknown_captured_reason() -> None:
    bad = _valid_envelope_dict()
    bad["captured_reason"] = "periodic"  # not in Literal["force_flat", "shutdown"]
    with pytest.raises(ValidationError):
        IndicatorStateEnvelope.model_validate(bad)


def test_envelope_payload_is_pass_through_dict() -> None:
    env_dict = _valid_envelope_dict()
    env_dict["payload"] = {"arbitrary": "shape", "decimals_as_strings": "1.234"}
    env = IndicatorStateEnvelope.model_validate(env_dict)
    assert env.payload == {"arbitrary": "shape", "decimals_as_strings": "1.234"}


def test_hydrate_policy_values() -> None:
    # Verify the three values match CLI flag values exactly.
    assert HydratePolicy.REQUIRE.value == "require"
    assert HydratePolicy.OPTIONAL.value == "optional"
    assert HydratePolicy.DISABLED.value == "disabled"


def test_hydrate_policy_from_string() -> None:
    assert HydratePolicy("require") is HydratePolicy.REQUIRE


def test_hydration_receipt_serializes_accepted_true() -> None:
    receipt = HydrationReceipt(
        schema_version=1,
        hydrated_at_ms=1747641007500,
        policy=HydratePolicy.REQUIRE,
        global_path="PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json",
        global_sha256="abc",
        accepted=True,
        strategy_key="spy_ema_crossover",
        symbol="SPY",
        consolidator_period_min=15,
        sidecar_last_consolidated_bar_end_ms=1747166100000,
        expected_prev_session_close_ms=1747166100000,
        calendar="NYSE",
        validation=ValidationResult.all_passed(),
    )
    j = json.loads(receipt.model_dump_json())
    assert j["accepted"] is True
    assert j["validation"]["failure_reason"] is None
    assert j["policy"] == "require"


def test_validation_result_all_passed_factory() -> None:
    vr = ValidationResult.all_passed()
    assert vr.schema_version_ok and vr.identity_ok and vr.calendar_ok
    assert vr.payload_shape_ok and vr.indicators_ready_ok and vr.lifecycle_flat_ok
    assert vr.failure_reason is None


def test_validation_result_failure_factory() -> None:
    vr = ValidationResult.failed("calendar_stale", calendar_ok=False)
    assert vr.failure_reason == "calendar_stale"
    assert vr.calendar_ok is False
    # Checks that did not fail remain True (or whatever the caller passed).
    assert vr.schema_version_ok is True
