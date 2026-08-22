"""Qualification, sealing, and legacy-preservation tests for Signal Programs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    LegacyMigrationLineageConflictError,
    alpaca_v1_action_plan,
)
from app.services.bot_carryover import configuration_hash
from app.services.signal_program_admission import (
    DEFAULT_QUALIFICATION_MANIFEST,
    LegacyProgramUnreconstructibleError,
    ProgramBuildQualificationManifest,
    SignalProgramSealError,
    build_start_program_seal,
    legacy_migration_clone_instance_id,
    prove_running_program_build,
    qualification_receipt_payload,
    reconstruct_legacy_program_seal,
)

_SID = "sealed-ema-1"
_NOW = 1_787_356_800_000


def _binding(**updates: object) -> BrokerBotBinding:
    values: dict[str, object] = {
        "strategy_instance_id": _SID,
        "strategy_key": "ema_crossover_signal",
        "broker": "alpaca",
        "symbol": "SPY",
        "use_rth": True,
        "mode": "dry_run",
        "quantity": 1,
        "carryover_policy": "FORBID",
        "action_plan": alpaca_v1_action_plan("SPY"),
        "strategy_params": {"gap": 0.2, "rsi_min": 50.0, "rsi_max": 70.0},
        "strategy_param_origins": {
            "gap": "deploy_override",
            "rsi_min": "registered_default",
            "rsi_max": "registered_default",
        },
        "sealed_account_id": f"sim:{_SID}",
        "run_id": "run-1",
        "created_at_ms": _NOW,
    }
    values.update(updates)
    return BrokerBotBinding.model_validate(values)


def _validation() -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="ema_crossover_signal",
        evidence_status="accepted",
        event_id="validation-ema-1",
        evidence_snapshot_sha256="a" * 64,
        verified_at_ms=_NOW,
        explanation="The exact validation snapshot was re-hashed.",
    )


def _sealed_binding() -> BrokerBotBinding:
    binding = _binding()
    seal = build_start_program_seal(
        binding,
        _validation(),
        parameter_origins=binding.strategy_param_origins,
    )
    assert seal is not None
    binding = binding.model_copy(update={"sealed_program": seal})
    proof = prove_running_program_build(binding, verified_at_ms=_NOW)
    assert proof.state == "PROVEN"
    return binding.model_copy(update={"program_build": proof})


def test_seal_resolves_units_origins_and_nested_authority_hashes() -> None:
    binding = _sealed_binding()
    seal = binding.sealed_program
    assert seal is not None

    assert seal.configured_signal.parameters["gap"].unit == "quote_currency"
    assert seal.configured_signal.parameters["gap"].origin == "deploy_override"
    assert seal.configured_signal.parameters["symbol"].origin == "deployment_symbol"
    assert seal.configured_signal.parameters_match_validated_settings is True
    assert seal.configured_signal_hash == seal.configured_signal.semantic_hash()

    other_account = seal.model_copy(update={"sealed_account_id": "sim:other"})
    assert other_account.configured_signal_hash == seal.configured_signal_hash
    assert other_account.bot_configuration_hash == seal.bot_configuration_hash
    # Validation catches the stale outer hash rather than silently accepting
    # an account mutation as the same bot identity.
    try:
        type(seal).model_validate(other_account.model_dump(mode="json"))
    except ValueError as exc:
        assert "bot configuration hash" in str(exc)
    else:  # pragma: no cover - guards the self-hash validator itself
        raise AssertionError("mutated outer seal unexpectedly validated")


def test_custom_gate_is_sealed_but_not_claimed_as_golden_validated() -> None:
    binding = _binding(
        strategy_params={"gap": 0.35, "rsi_min": 50.0, "rsi_max": 70.0},
    )

    seal = build_start_program_seal(binding, _validation(), parameter_origins=binding.strategy_param_origins)

    assert seal is not None
    assert seal.configured_signal.parameters["gap"].value == 0.35
    assert seal.configured_signal.parameters_match_validated_settings is False


def test_build_start_program_seal_fails_closed_on_missing_parameter_origin() -> None:
    """No inference in the Start path: an absent origin is a hard error, not a guess."""
    binding = _binding()

    with pytest.raises(SignalProgramSealError, match="explicit origin"):
        build_start_program_seal(binding, _validation())


def test_build_start_program_seal_defaults_every_untouched_parameter_without_an_origins_map() -> None:
    """A binding that never supplied ``strategy_params`` needs no origins map.

    Every effective value came straight from the registered schema default
    for *this* seal's own construction, not from a value-vs-default guess
    reconstructed from possibly-stale history -- so this is exempt from the
    completeness requirement covered by the tests above. This is the
    common fresh-deploy shape exercised by ``BotRunnerRegistry.deploy``
    when the caller supplies no explicit strategy params at all.
    """
    binding = _binding(strategy_params=None, strategy_param_origins=None)

    seal = build_start_program_seal(binding, _validation())

    assert seal is not None
    assert seal.configured_signal.parameters["gap"].origin == "registered_default"
    assert seal.configured_signal.parameters["rsi_min"].origin == "registered_default"
    assert seal.configured_signal.parameters["rsi_max"].origin == "registered_default"


def test_build_start_program_seal_fails_closed_on_partial_parameter_origins() -> None:
    binding = _binding()

    with pytest.raises(SignalProgramSealError, match="explicit origin"):
        build_start_program_seal(binding, _validation(), parameter_origins={"gap": "deploy_override"})


def test_reconstruct_legacy_program_seal_infers_origin_only_for_missing_entries() -> None:
    """The legacy-only inference path fills gaps but trusts recorded origins.

    A legacy binding whose ``gap`` happens to equal the *currently*
    registered default, but whose origin was in fact explicitly recorded
    as an override (e.g. by an earlier partial migration), must keep that
    recorded origin rather than have it overwritten by the value-vs-default
    guess.
    """
    defaults = _STRATEGY_REGISTRY["ema_crossover_signal"].param_schema().model_dump(mode="json")
    legacy_binding = _binding(
        strategy_params={"gap": defaults["gap"], "rsi_min": defaults["rsi_min"], "rsi_max": defaults["rsi_max"]},
        strategy_param_origins={"gap": "deploy_override"},
    )

    reconstructed = reconstruct_legacy_program_seal(legacy_binding, _validation())

    assert reconstructed is not None
    assert reconstructed.configured_signal.parameters["gap"].origin == "deploy_override"
    # rsi_min/rsi_max were never recorded, so they fall back to the
    # value-vs-current-default inference documented on
    # `_infer_legacy_parameter_origins`.
    assert reconstructed.configured_signal.parameters["rsi_min"].origin == "registered_default"


def test_committed_receipt_matches_current_artifacts_and_golden_root() -> None:
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    manifest = ProgramBuildQualificationManifest.model_validate_json(
        DEFAULT_QUALIFICATION_MANIFEST.read_text(encoding="utf-8")
    )

    generated = qualification_receipt_payload(
        program_key="ema_crossover_signal",
        contract=contract,
        qualified_at_ms=manifest.receipts[0].qualified_at_ms,
        qualification_suite=manifest.receipts[0].qualification_suite,
    )

    assert manifest.receipts[0].model_dump(mode="json") == generated


def test_tampered_qualification_receipt_fails_closed(tmp_path: Path) -> None:
    binding = _sealed_binding()
    payload = json.loads(DEFAULT_QUALIFICATION_MANIFEST.read_text(encoding="utf-8"))
    payload["receipts"][0]["artifact_digest"] = "f" * 64
    tampered = tmp_path / "tampered-build-receipts.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    proof = prove_running_program_build(
        binding,
        verified_at_ms=_NOW,
        manifest_path=tampered,
    )

    assert proof.state == "UNPROVEN"


def test_legacy_signal_instance_without_v2_seal_is_not_resumable() -> None:
    proof = prove_running_program_build(_binding(), verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert "must be cloned" in proof.explanation


def test_v2_seal_appends_without_rewriting_v1_identity_bytes(tmp_path: Path) -> None:
    repository = BotBindingRepository(
        tmp_path,
        instance_dir_for=lambda strategy_instance_id: tmp_path / "live_state" / strategy_instance_id,
    )
    binding = _sealed_binding()
    repository.record_launch(binding, launch_reason="deploy")
    instance_path = tmp_path / "live_state" / _SID / "strategy_instance.json"
    original_v1_bytes = instance_path.read_bytes()

    restored = repository.read(_SID)
    assert restored is not None
    assert restored.sealed_program == binding.sealed_program
    resumed = restored.model_copy(
        update={
            "run_id": "run-2",
            "created_at_ms": _NOW + 1,
            "program_build": binding.program_build,
        }
    )
    repository.record_launch(resumed, launch_reason="resume")

    assert instance_path.read_bytes() == original_v1_bytes
    assert configuration_hash(restored) == configuration_hash(binding)
    assert (tmp_path / "live_state" / _SID / "sealed_program_v2.json").is_file()
    assert (
        tmp_path
        / "live_state"
        / _SID
        / "program_build_evidence"
        / "run-2.json"
    ).is_file()


def _repository(tmp_path: Path) -> BotBindingRepository:
    return BotBindingRepository(
        tmp_path,
        instance_dir_for=lambda strategy_instance_id: tmp_path / "live_state" / strategy_instance_id,
    )


def test_reconstruct_legacy_program_seal_matches_a_fresh_build(tmp_path: Path) -> None:
    """PRD Sec 11.5 append case: reconstruction from persisted v1 fields is exact."""
    del tmp_path
    legacy_binding = _binding()  # sealed_program defaults to None, as any pre-seal record does.
    assert legacy_binding.sealed_program is None

    reconstructed = reconstruct_legacy_program_seal(legacy_binding, _validation())
    fresh = build_start_program_seal(
        legacy_binding,
        _validation(),
        parameter_origins=legacy_binding.strategy_param_origins,
    )

    assert reconstructed is not None
    assert reconstructed == fresh


def test_reconstruct_legacy_program_seal_raises_for_unprovable_parameters() -> None:
    """A gap the current schema no longer accepts cannot be reconstructed exactly."""
    unprovable = _binding(strategy_params={"gap": -1.0, "rsi_min": 50.0, "rsi_max": 70.0})

    with pytest.raises(LegacyProgramUnreconstructibleError):
        reconstruct_legacy_program_seal(unprovable, _validation())


def test_legacy_migration_seal_appends_to_same_instance_preserving_v1_bytes(tmp_path: Path) -> None:
    """PRD Sec 11.5 append case, exercised end to end through the repository.

    Deploy a v1-only instance (no seal), read it back the way Resume would,
    reconstruct its seal, and record a second (resume) launch carrying it.
    The append must land under the *same* ``strategy_instance_id`` and must
    not perturb a single byte of the original ``strategy_instance.json``.
    """
    repository = _repository(tmp_path)
    legacy_binding = _binding()
    repository.record_launch(legacy_binding, launch_reason="deploy")
    instance_path = tmp_path / "live_state" / _SID / "strategy_instance.json"
    original_v1_bytes = instance_path.read_bytes()
    assert not (tmp_path / "live_state" / _SID / "sealed_program_v2.json").is_file()

    restored = repository.read(_SID)
    assert restored is not None
    assert restored.sealed_program is None

    reconstructed = reconstruct_legacy_program_seal(restored, _validation())
    assert reconstructed is not None
    resumed = restored.model_copy(
        update={
            "run_id": "run-2",
            "created_at_ms": _NOW + 1,
            "sealed_program": reconstructed,
        }
    )
    proof = prove_running_program_build(resumed, verified_at_ms=_NOW + 1)
    assert proof.state == "PROVEN"
    resumed = resumed.model_copy(update={"program_build": proof})

    repository.record_launch(resumed, launch_reason="resume")

    assert instance_path.read_bytes() == original_v1_bytes
    seal_path = tmp_path / "live_state" / _SID / "sealed_program_v2.json"
    assert seal_path.is_file()
    assert json.loads(seal_path.read_text(encoding="utf-8"))["strategy_instance_id"] == _SID
    # The append landed on the exact same instance id — no clone was minted.
    assert not (tmp_path / "live_state" / legacy_migration_clone_instance_id(_SID)).exists()


def test_legacy_migration_clone_instance_id_is_deterministic_and_distinct() -> None:
    first = legacy_migration_clone_instance_id(_SID)
    second = legacy_migration_clone_instance_id(_SID)

    assert first == second
    assert first != _SID
    assert len(first) <= 128


def test_legacy_migration_clone_lineage_is_idempotent_across_repeated_resume_attempts(
    tmp_path: Path,
) -> None:
    """PRD Sec 11.5 clone case: resuming twice must not mint two clones."""
    repository = _repository(tmp_path)
    unprovable = _binding(strategy_params={"gap": -1.0, "rsi_min": 50.0, "rsi_max": 70.0})
    repository.record_launch(unprovable, launch_reason="deploy")
    instance_path = tmp_path / "live_state" / _SID / "strategy_instance.json"
    original_v1_bytes = instance_path.read_bytes()

    with pytest.raises(LegacyProgramUnreconstructibleError):
        reconstruct_legacy_program_seal(unprovable, _validation())
    clone_id = legacy_migration_clone_instance_id(unprovable.strategy_instance_id)

    first = repository.ensure_legacy_migration_clone_lineage(
        original_strategy_instance_id=unprovable.strategy_instance_id,
        clone_instance_id=clone_id,
        reason="Persisted v1 parameters no longer validate.",
        now_ms=_NOW,
    )
    # A second Resume attempt on the same broken instance later — a fresh
    # timestamp, the same deterministic clone id — must not mint a second
    # clone or silently overwrite the first record's evidence.
    second = repository.ensure_legacy_migration_clone_lineage(
        original_strategy_instance_id=unprovable.strategy_instance_id,
        clone_instance_id=clone_id,
        reason="Persisted v1 parameters no longer validate.",
        now_ms=_NOW + 999,
    )

    assert first == second
    assert first.created_at_ms == _NOW  # the second call's now_ms never wins.
    assert repository.read_legacy_migration_lineage(clone_id) == first
    # The original instance is untouched: no lineage recorded under its own
    # id, no v1 byte perturbed, and it remains readable ("inspectable").
    assert repository.read_legacy_migration_lineage(_SID) is None
    assert instance_path.read_bytes() == original_v1_bytes
    restored = repository.read(_SID)
    assert restored is not None
    assert restored.strategy_instance_id == _SID


def test_legacy_migration_clone_lineage_conflict_on_reused_clone_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    clone_id = legacy_migration_clone_instance_id(_SID)
    repository.ensure_legacy_migration_clone_lineage(
        original_strategy_instance_id=_SID,
        clone_instance_id=clone_id,
        reason="Persisted v1 parameters no longer validate.",
        now_ms=_NOW,
    )

    with pytest.raises(LegacyMigrationLineageConflictError):
        repository.ensure_legacy_migration_clone_lineage(
            original_strategy_instance_id="a-completely-different-instance",
            clone_instance_id=clone_id,
            reason="Persisted v1 parameters no longer validate.",
            now_ms=_NOW,
        )
