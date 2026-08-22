"""Hashing and action-plan boundary tests for the v2 Signal Program seal."""

from __future__ import annotations

import copy
from typing import get_args

import pytest
from pydantic import BaseModel

from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ExitEligibilityContract,
    NumericalProvenanceContract,
    ResolvedSignalParameter,
    SealedBotProgram,
    SignalBarIntegrityContract,
    SignalClockContract,
    SignalDataContract,
    SignalSeriesContract,
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
        protocol_version="signal-session-protocol/v1",
        parameter_schema_version="ema-crossover-signal-params/v1",
        golden_trace_root=_TRACE_ROOT,
        parameters={
            "gap": ResolvedSignalParameter(value=0.2, unit="quote_currency", origin="registered_default"),
        },
        parameters_match_validated_settings=True,
        data=SignalDataContract(
            provider="polygon",
            symbol="SPY",
            base_timeframe_ms=60_000,
            decision_timeframe_ms=900_000,
        ),
        clock=SignalClockContract(use_rth=True, warmup_lookback_days=5),
        signals=(
            SignalSeriesContract(name="ema_fast", indicator="ema", field="close", period=5, warmup_bars=5),
            SignalSeriesContract(name="rsi", indicator="rsi_wilders", field="close", period=14, warmup_bars=15),
        ),
        decision_streams=("ENTER", "EXIT"),
        bar_integrity=SignalBarIntegrityContract(),
        exit_eligibility=ExitEligibilityContract(countdown_decision_clocks=5, countdown_state_persistable=False),
        numerical_provenance=NumericalProvenanceContract(
            formula="EMA(5)/EMA(10) crossover with RSI(14) filter.",
            reference="Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs",
            canonical_implementation="app/engine/strategy/algorithms/ema_crossover_signal.py",
            validated_against="tests/engine/strategy/test_ema_signal_program.py",
            equivalence_level="bit_exact",
            tolerance_atol=1e-9,
            tolerance_rtol=0.0,
            parity_fixture_ids=("tests/fixtures/golden/ema-signal-session/v1/trace-corpus.json",),
        ),
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


def _mutated_leaf(value: object) -> object:
    """Return a different-but-JSON-shaped value for one scalar leaf.

    ``None`` covers optional fields (e.g. ``NumericalProvenanceContract``'s
    tolerances at ``equivalence_level="bit_exact"``); every other JSON leaf
    type gets a same-shape mutation so the payload stays structurally valid.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return f"{value}-mutated"
    if value is None:
        return "mutated-from-null"
    raise TypeError(f"unhandled leaf type for mutation: {type(value).__name__}")


def _leaf_paths(node: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    """Return every leaf key-path in a JSON-shaped ``dict``/``list``/scalar tree."""
    if isinstance(node, dict):
        paths: list[tuple[object, ...]] = []
        for key, child in node.items():
            paths.extend(_leaf_paths(child, (*prefix, key)))
        return paths
    if isinstance(node, list):
        paths = []
        for index, child in enumerate(node):
            paths.extend(_leaf_paths(child, (*prefix, index)))
        return paths
    return [prefix]


def _with_mutated_leaf(payload: object, path: tuple[object, ...]) -> object:
    """Deep-copy ``payload`` and replace the leaf at ``path`` with a mutated value."""
    mutated = copy.deepcopy(payload)
    cursor = mutated
    for key in path[:-1]:
        cursor = cursor[key]  # type: ignore[index]
    last = path[-1]
    cursor[last] = _mutated_leaf(cursor[last])  # type: ignore[index]
    return mutated


def _seal_family_models(root: type[BaseModel]) -> tuple[type[BaseModel], ...]:
    """Every model reachable from the outer seal, discovered by walking its
    own annotations rather than listed by hand.

    A hand-maintained tuple is the same rot the leaf walk below avoids: a
    nested contract added later would simply not be checked, and the omission
    looks like nothing at all. Deriving the family means a new sealed model is
    covered the moment it is reachable from ``SealedBotProgram``.
    """
    seen: dict[type[BaseModel], None] = {}

    def visit_annotation(annotation: object) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            visit(annotation)
            return
        # Recurse through every generic parameter -- a model can sit inside a
        # union inside a list inside an optional, at arbitrary depth.
        for arg in get_args(annotation):
            visit_annotation(arg)

    def visit(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen[model] = None
        for info in model.model_fields.values():
            visit_annotation(info.annotation)

    visit(root)
    return tuple(seen)


_SEAL_FAMILY_MODELS = _seal_family_models(SealedBotProgram)


def test_no_seal_family_field_is_excluded_from_its_json_dump() -> None:
    """Precondition the leaf-mutation tests below depend on.

    A field declared with ``Field(..., exclude=True)`` (or any other
    mechanism that drops it from ``model_dump``) would silently stop
    participating in the semantic hash while still looking, at the Python
    level, like part of the identity — and a leaf-mutation walk driven off
    the *dumped* payload can never discover a leaf that was never dumped.
    Iterating ``model_fields`` instead catches that case, and — like the
    leaf walk — is driven by the model's own declared field set rather than
    a hand-maintained name list, so a future field is covered automatically.
    """
    for model in _SEAL_FAMILY_MODELS:
        excluded = [name for name, info in model.model_fields.items() if info.exclude]
        assert not excluded, (
            f"{model.__name__} excludes {excluded} from its JSON dump — "
            "those fields would silently stop participating in the semantic hash"
        )


def test_mutating_any_semantic_leaf_changes_the_configured_signal_hash() -> None:
    """The field-sensitivity proof the sealed identity requires (issue: half the
    §11.1 semantic surface previously had no sealed field to mutate at all).

    Walks every leaf in the *sealed* payload — ``model_dump(mode="json")`` of
    ``ConfiguredSignalProgramSeal`` — rather than a hand-listed set of field
    names, so a semantic field added to the model later is exercised by this
    same test automatically instead of requiring the test to be remembered
    and updated in lockstep.
    """
    baseline = _configured_signal()
    baseline_payload = baseline.model_dump(mode="json")
    baseline_hash = semantic_payload_hash(baseline_payload)
    assert baseline_hash == baseline.semantic_hash()

    leaf_paths = _leaf_paths(baseline_payload)
    assert len(leaf_paths) >= 25, "fixture should exercise every nested contract, not just top-level scalars"

    for path in leaf_paths:
        mutated_payload = _with_mutated_leaf(baseline_payload, path)
        mutated_hash = semantic_payload_hash(mutated_payload)
        assert mutated_hash != baseline_hash, f"mutating leaf {path!r} did not change configured_signal_hash"


def test_mutating_any_bot_program_leaf_changes_the_bot_configuration_hash() -> None:
    """Same proof one layer up: the outer seal's own fields (mode, broker,
    quantity, carryover_policy, validation identity, ...) plus the nested
    ``configured_signal`` are all covered by walking the outer seal's own
    hashed payload, again driven off the model's own dumped field set.
    """
    sealed = _sealed_bot_program(action_plan=_action_plan())
    baseline_payload = sealed.model_dump(mode="json", exclude={"bot_configuration_hash"})
    baseline_hash = semantic_payload_hash(baseline_payload)
    assert baseline_hash == sealed.bot_configuration_hash

    leaf_paths = _leaf_paths(baseline_payload)
    assert len(leaf_paths) >= 30

    for path in leaf_paths:
        mutated_payload = _with_mutated_leaf(baseline_payload, path)
        mutated_hash = semantic_payload_hash(mutated_payload)
        assert mutated_hash != baseline_hash, f"mutating leaf {path!r} did not change bot_configuration_hash"
