"""Hashing and action-plan boundary tests for the v2 Signal Program seal."""

from __future__ import annotations

import pytest

from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    SealedBotProgram,
    SignalClockContract,
    SignalDataContract,
    seal_bot_program,
    semantic_payload_hash,
)

_TRACE_ROOT = "a" * 64
_SNAPSHOT_SHA = "b" * 64


def _action_plan() -> ActionPlan:
    return ActionPlan(
        on_enter=[
            StockEntryLeg(
                leg_id="primary",
                instrument=StockInstrument(kind="stock", underlying="SPY"),
                position="long",
                qty_ratio=1,
            )
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="primary")],
    )


def _configured_signal() -> ConfiguredSignalProgramSeal:
    return ConfiguredSignalProgramSeal(
        program_key="ema_crossover_signal",
        program_version="1",
        golden_trace_root=_TRACE_ROOT,
        parameters={},
        parameters_match_validated_settings=True,
        data=SignalDataContract(
            provider="polygon",
            symbol="SPY",
            base_timeframe_ms=60_000,
            decision_timeframe_ms=900_000,
        ),
        clock=SignalClockContract(use_rth=True, warmup_lookback_days=5),
    )


def _sealed_bot_program(*, action_plan: ActionPlan | dict[str, object]) -> SealedBotProgram:
    configured = _configured_signal()
    return seal_bot_program(
        strategy_instance_id="sealed-test-1",
        configured_signal=configured,
        configured_signal_hash=configured.semantic_hash(),
        broker="alpaca",
        sealed_account_id="sim:sealed-test-1",
        mode="dry_run",
        action_plan=action_plan,
        quantity=1,
        carryover_policy="FORBID",
        validation_event_id="validation-1",
        validation_snapshot_sha256=_SNAPSHOT_SHA,
        sealed_at_ms=1_787_356_800_000,
    )


def test_semantic_hash_raises_type_error_for_unsupported_value_not_recursion_error() -> None:
    """A non-serializable value must raise TypeError, never recurse to RecursionError.

    Regression for the default handler returning (instead of raising) the
    TypeError: ``json.dumps`` fed the returned exception object back into
    ``default`` again, recursing until ``RecursionError``.
    """
    with pytest.raises(TypeError, match="unsupported semantic value"):
        semantic_payload_hash({"bad": {1, 2, 3}})


def test_action_plan_field_accepts_model_and_matches_equivalent_dict_hash() -> None:
    """``action_plan`` typed as ``ActionPlan`` must hash identically to the raw dict.

    Proves the field-type change from ``dict[str, Any]`` to ``ActionPlan``
    does not perturb ``bot_configuration_hash``: constructing the seal from
    an ``ActionPlan`` instance and from its equivalent ``model_dump(mode="json")``
    dict must produce byte-identical serialized payloads and hashes.
    """
    plan = _action_plan()
    plan_dict = plan.model_dump(mode="json")

    sealed_from_model = _sealed_bot_program(action_plan=plan)
    sealed_from_dict = _sealed_bot_program(action_plan=plan_dict)

    assert sealed_from_model.action_plan == plan
    assert sealed_from_model.model_dump(mode="json") == sealed_from_dict.model_dump(mode="json")
    assert sealed_from_model.bot_configuration_hash == sealed_from_dict.bot_configuration_hash


def test_sealed_bot_program_round_trips_through_persisted_json() -> None:
    """A persisted seal's JSON must still parse back into ``SealedBotProgram``."""
    sealed = _sealed_bot_program(action_plan=_action_plan())

    restored = SealedBotProgram.model_validate_json(sealed.model_dump_json())

    assert restored == sealed
    assert isinstance(restored.action_plan, ActionPlan)
