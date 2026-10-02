"""Qualification, sealing, and legacy-preservation tests for Signal Programs."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.engine.strategy.program_sources import DECLARED_PROGRAM_SOURCE_PATHS
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.run_admission import (
    CORPUS_UNCOVERED_EXPLANATION,
    CORPUS_UNCOVERED_NEXT_STEP,
    StrategyValidationAdmissionFact,
)
from app.schemas.signal_program_seal import SealedBotProgram, semantic_payload_hash
from app.services import signal_program_admission as admission_module
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_carryover import configuration_hash
from app.services.signal_program_admission import (
    DEFAULT_QUALIFICATION_MANIFEST,
    ProgramBuildQualificationManifest,
    ProgramBuildQualificationReceipt,
    SignalProgramSealError,
    build_start_program_seal,
    imported_source_drift,
    prove_running_program_build,
    qualification_receipt_payload,
    record_imported_program_sources,
    running_wiring_digest,
)

_SID = "sealed-ema-1"
_NOW = 1_787_356_800_000


@pytest.fixture(autouse=True)
def _own_imported_source_digests():
    """Own the session-global anchor locally: restore the conftest-primed
    digest set after each test so a test that clears the dict (or patches
    ``_SERVICE_ROOT`` before re-anchoring) cannot poison every later drift
    assertion."""
    primed = dict(admission_module._IMPORTED_SOURCE_DIGESTS)
    yield
    admission_module._IMPORTED_SOURCE_DIGESTS.clear()
    admission_module._IMPORTED_SOURCE_DIGESTS.update(primed)


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
        "strategy_params": {
            "gap": 0.2,
            "gap_bps": 0.0,
            "rsi_min": 50.0,
            "rsi_max": 70.0,
        },
        "strategy_param_origins": {
            "gap": "deploy_override",
            "gap_bps": "deploy_override",
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


_LEGACY_DEFAULT_SEAL = Path(__file__).parents[1] / "fixtures" / "signal_program_seals" / "ema_default_before_2696.json"


def test_a_default_ema_seal_minted_before_the_lengths_became_parameters_still_proves_and_reseals_byte_identically() -> None:
    """The fixture is the seal master minted (81292e4a, before #2696) for ``_binding()`` with ``_validation()``.

    The EMA lengths and hold became parameters with identity-neutral
    defaults; a running bot sealed before that must keep proving, and a seal
    minted now at the reference lengths must hash exactly as it did.
    """
    legacy = SealedBotProgram.model_validate_json(_LEGACY_DEFAULT_SEAL.read_text(encoding="utf-8"))
    proof = prove_running_program_build(_binding().model_copy(update={"sealed_program": legacy}), verified_at_ms=_NOW)

    assert (proof.state, proof.corpus_coverage) == ("PROVEN", "COVERED"), proof.explanation
    binding = _binding()
    fresh = build_start_program_seal(binding, _validation(), parameter_origins=binding.strategy_param_origins)
    assert fresh is not None
    assert (fresh.configured_signal_hash, fresh.bot_configuration_hash) == (legacy.configured_signal_hash, legacy.bot_configuration_hash)


def test_a_seal_off_the_reference_lengths_records_the_extended_schema_and_provenance() -> None:
    binding = _binding(
        strategy_params={"gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0, "fast_period": 7},
        strategy_param_origins={"gap": "deploy_override", "gap_bps": "deploy_override", "rsi_min": "registered_default", "rsi_max": "registered_default", "fast_period": "deploy_override"},
    )
    seal = build_start_program_seal(binding, _validation(), parameter_origins=binding.strategy_param_origins)
    assert seal is not None

    configured = seal.configured_signal
    assert configured.parameter_schema_version == "ema-crossover-signal-params/v3"
    assert "EMA(fast)/EMA(slow)" in configured.numerical_provenance.formula
    proof = prove_running_program_build(binding.model_copy(update={"sealed_program": seal}), verified_at_ms=_NOW)
    assert proof.state == "PROVEN"


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


def test_ema_registered_defaults_are_covered_for_live_admission() -> None:
    """The UI's accepted Golden scope and runtime corpus must name one point."""
    parameters = {
        "gap": 0.2,
        "gap_bps": 0.0,
        "rsi_min": 50.0,
        "rsi_max": 70.0,
    }
    binding = _binding(
        strategy_params=parameters,
        strategy_param_origins={name: "deploy_override" for name in parameters},
    )

    seal = build_start_program_seal(
        binding,
        _validation(),
        parameter_origins=binding.strategy_param_origins,
    )

    assert seal is not None
    assert seal.configured_signal.parameters_match_validated_settings is True


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


def test_committed_receipt_matches_current_artifacts_and_golden_root() -> None:
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    manifest = ProgramBuildQualificationManifest.model_validate_json(
        DEFAULT_QUALIFICATION_MANIFEST.read_text(encoding="utf-8")
    )
    # The manifest is written sorted alphabetically by program_key
    # (scripts/run_signal_program_build_qualification.py) -- not indexed by
    # position, which "deployment_validation" (issue #1730 Slice 5's last
    # promotion) would silently break by sorting before
    # "ema_crossover_signal" and displacing it from receipts[0].
    committed = next(receipt for receipt in manifest.receipts if receipt.program_key == "ema_crossover_signal")

    generated = qualification_receipt_payload(
        program_key="ema_crossover_signal",
        contract=contract,
        qualified_at_ms=committed.qualified_at_ms,
        qualification_suite=committed.qualification_suite,
        # Recorded lineage is carried forward, not re-derived, so the committed
        # value is the expected value (see _prior_receipt_reuse in the runner).
        git_provenance=(
            None
            if committed.git_provenance is None
            else committed.git_provenance.model_dump(mode="json")
        ),
    )

    dumped = committed.model_dump(mode="json")
    if dumped.get("git_provenance") is None:
        dumped.pop("git_provenance", None)  # absent lineage is omitted, never null
    assert dumped == generated


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


def test_seal_signal_semantics_come_from_the_registry_contract() -> None:
    """Task requirement: the sealed semantics must match the registry contract
    they were derived from -- and be *derived*, not a second hand-authored
    copy. ``build_start_program_seal`` copies these five field values straight
    off ``SignalProgramContract``, so this equality holds by construction; the
    regression this guards is a future edit that starts re-deriving one of
    them from something else instead of the contract.
    """
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    binding = _sealed_binding()
    seal = binding.sealed_program
    assert seal is not None
    configured = seal.configured_signal

    assert configured.protocol_version == contract.protocol_version
    assert configured.parameter_schema_version == contract.parameter_schema_version
    assert configured.signals == contract.signals
    assert configured.decision_streams == contract.decision_streams
    assert configured.bar_integrity == contract.bar_integrity
    assert configured.exit_eligibility == contract.exit_eligibility
    assert configured.numerical_provenance == contract.numerical_provenance


def test_sealed_protocol_version_mismatch_fails_closed() -> None:
    """Sealed-completeness fix, sibling to the provider-mismatch test below:
    every newly widened sealed field must fail build-proof on mismatch too, at
    the same cadence as program_version/golden_trace_root/provider. Exercises
    one representative new field end to end through ``prove_running_program_build``'s
    real ``or`` chain, rather than only asserting the chain's source contains it.
    """
    binding = _sealed_binding()
    seal = binding.sealed_program
    assert seal is not None
    tampered_configured = seal.configured_signal.model_copy(update={"protocol_version": "stale-protocol/v0"})
    tampered_seal = seal.model_copy(update={"configured_signal": tampered_configured})
    binding = binding.model_copy(update={"sealed_program": tampered_seal})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"


def test_sealed_provider_identity_mismatch_fails_closed() -> None:
    """#1729 AC4: "provider" proof means the sealed qualification-lineage
    identity is present and unchanged against the currently
    registered contract -- not a live-feed parity gate. A seal claiming a
    different provider than the registry cannot prove its running build,
    at the same cadence as the program_version/golden_trace_root checks."""
    binding = _sealed_binding()
    seal = binding.sealed_program
    assert seal is not None
    assert seal.configured_signal.data.provider == "polygon"
    tampered_data = seal.configured_signal.data.model_copy(update={"provider": "not-the-qualified-lineage"})
    tampered_configured = seal.configured_signal.model_copy(update={"data": tampered_data})
    tampered_seal = seal.model_copy(update={"configured_signal": tampered_configured})
    binding = binding.model_copy(update={"sealed_program": tampered_seal})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"


def test_legacy_signal_instance_without_v2_seal_requires_fresh_deployment() -> None:
    proof = prove_running_program_build(_binding(), verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert "fresh deployment is required" in proof.explanation


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


def _sma_binding(resolution_minutes: int) -> BrokerBotBinding:
    return _binding(
        strategy_instance_id="sealed-sma-1",
        strategy_key="sma_crossover",
        strategy_params={"resolution_minutes": resolution_minutes},
        strategy_param_origins={"resolution_minutes": "deploy_override"},
        sealed_account_id="sim:sealed-sma-1",
    )


def _sma_validation() -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="sma_crossover",
        evidence_status="accepted",
        event_id="validation-sma-1",
        evidence_snapshot_sha256="b" * 64,
        verified_at_ms=_NOW,
        explanation="The exact validation snapshot was re-hashed.",
    )


def test_seal_records_the_cadence_the_program_will_actually_run() -> None:
    """The hashed decision cadence must describe the running decision stream.

    ``resolution_minutes`` is deploy-overridable and the registry factory
    clocks the session from the *resolved* value. Copying the contract's
    qualified cadence into the seal instead made the immutable attestation
    describe a different decision stream than the bot executes, with only a
    separate ``parameters_match_validated_settings`` flag hinting at it.
    """
    contract = _STRATEGY_REGISTRY["sma_crossover"].signal_program_contract
    assert contract is not None
    binding = _sma_binding(5)

    seal = build_start_program_seal(
        binding, _sma_validation(), parameter_origins=binding.strategy_param_origins
    )

    assert seal is not None
    assert seal.configured_signal.data.decision_timeframe_ms == 5 * 60_000
    assert seal.configured_signal.data.decision_timeframe_ms != contract.decision_timeframe_ms
    assert seal.configured_signal.parameters_match_validated_settings is False


def test_overridden_cadence_is_proven_but_stamped_uncovered() -> None:
    """A cadence the golden corpus never covered must not read as covered.

    ``golden_trace_root`` pins one decision *stream*. A program clocked at a
    different resolution reads different bars and reaches different
    decisions, so the qualification evidence does not describe it. Before the
    seal recorded the running cadence, both sides of this comparison came
    from the same contract constant and it could never fail. Since ADR 0054
    that is a coverage stamp on a PROVEN build, exactly like any other
    resolved value outside ``validated_settings``: the bytes match their
    receipt, the corpus does not describe this clock.
    """
    binding = _sma_binding(5)
    seal = build_start_program_seal(
        binding, _sma_validation(), parameter_origins=binding.strategy_param_origins
    )
    assert seal is not None
    binding = binding.model_copy(update={"sealed_program": seal})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "PROVEN"
    assert proof.corpus_coverage == "UNCOVERED"


def test_qualified_cadence_still_proves_its_running_build() -> None:
    """The guard above must not refuse a deploy that runs the qualified clock."""
    contract = _STRATEGY_REGISTRY["sma_crossover"].signal_program_contract
    assert contract is not None
    qualified_minutes = contract.decision_timeframe_ms // 60_000
    binding = _sma_binding(qualified_minutes)
    seal = build_start_program_seal(
        binding, _sma_validation(), parameter_origins=binding.strategy_param_origins
    )
    assert seal is not None
    binding = binding.model_copy(update={"sealed_program": seal})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "PROVEN"


def _rsi_binding(**params: object) -> BrokerBotBinding:
    return _binding(
        strategy_instance_id="sealed-rsi-1",
        strategy_key="rsi_mean_reversion",
        strategy_params=dict(params),
        strategy_param_origins={name: "deploy_override" for name in params},
        sealed_account_id="sim:sealed-rsi-1",
    )


def _rsi_validation() -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="rsi_mean_reversion",
        evidence_status="accepted",
        event_id="validation-rsi-1",
        evidence_snapshot_sha256="c" * 64,
        verified_at_ms=_NOW,
        explanation="The exact validation snapshot was re-hashed.",
    )


def test_overridden_parameter_proves_its_build_but_stamps_the_corpus_uncovered() -> None:
    """Coverage is a stamp on the proof, not a reason to withhold it.

    Any resolved value the corpus does not cover -- ``oversold`` here, but
    equally ``resolution_minutes`` or the symbol -- means the golden trace
    root does not describe this configuration. That is a fact about the
    *evidence*, not about the *bytes*: the artifact digest still matches its
    receipt, so the build is PROVEN and the fact says, separately, that the
    corpus does not cover it. Whether an uncovered point may start is the
    pure admission policy's decision (paper yes, anything else no), never
    this proof's.
    """
    binding = _rsi_binding(oversold=25.0)
    seal = build_start_program_seal(
        binding, _rsi_validation(), parameter_origins=binding.strategy_param_origins
    )
    assert seal is not None
    assert seal.configured_signal.data.decision_timeframe_ms == 15 * 60_000
    binding = binding.model_copy(update={"sealed_program": seal})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "PROVEN"
    assert proof.corpus_coverage == "UNCOVERED"
    assert proof.next_step == CORPUS_UNCOVERED_NEXT_STEP
    assert CORPUS_UNCOVERED_EXPLANATION in proof.explanation
    assert "program-corpus-coverage:UNCOVERED" in proof.evidence_refs


def test_registered_defaults_still_prove_their_running_build() -> None:
    """A deploy that resolved every validated value is covered, and says so."""
    binding = _rsi_binding()
    seal = build_start_program_seal(
        binding, _rsi_validation(), parameter_origins=binding.strategy_param_origins
    )
    assert seal is not None
    binding = binding.model_copy(update={"sealed_program": seal})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "PROVEN"
    assert proof.corpus_coverage == "COVERED"
    assert proof.next_step is None
    assert CORPUS_UNCOVERED_EXPLANATION not in proof.explanation
    assert "program-corpus-coverage:COVERED" in proof.evidence_refs


def test_build_proof_names_which_agreement_broke() -> None:
    """Each refusal must say which agreement failed, not one generic sentence.

    Every one of the seventeen terms in the previous ``or`` chain collapsed
    into "does not match this instance or registry contract", so an operator
    who overrode a parameter read exactly what one whose account identity had
    drifted read.
    """
    binding = _sealed_binding()
    seal = binding.sealed_program
    assert seal is not None
    drifted = seal.model_copy(update={"sealed_account_id": "sim:some-other-account"})
    binding = binding.model_copy(update={"sealed_program": drifted})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert proof.explanation is not None
    assert "different account" in proof.explanation


# --- #2696: the EMA lengths and the hold are parameters -----------------------

_DEFAULT_GATES = {"gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}
_NON_DEFAULT_LENGTHS = {"fast_period": 7, "slow_period": 20, "hold_bars": 8}


def _lengths_seal(**lengths: int) -> tuple[BrokerBotBinding, SealedBotProgram]:
    parameters = {**_DEFAULT_GATES, **lengths}
    binding = _binding(
        strategy_params=parameters,
        strategy_param_origins={name: "deploy_override" for name in parameters},
    )
    seal = build_start_program_seal(binding, _validation(), parameter_origins=binding.strategy_param_origins)
    assert seal is not None
    return binding.model_copy(update={"sealed_program": seal}), seal


def test_explicit_default_lengths_seal_exactly_like_the_pre_lengths_point() -> None:
    _binding_with_lengths, explicit = _lengths_seal(fast_period=5, slow_period=10, hold_bars=5)
    _binding_without, omitted = _lengths_seal()
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None

    assert explicit.configured_signal_hash == omitted.configured_signal_hash
    assert set(explicit.configured_signal.parameters) == {"symbol", *_DEFAULT_GATES}
    assert explicit.configured_signal.signals == contract.signals
    assert explicit.configured_signal.exit_eligibility == contract.exit_eligibility
    assert explicit.configured_signal.parameters_match_validated_settings is True


def test_seal_records_the_series_and_hold_its_own_lengths_build() -> None:
    _bound, seal = _lengths_seal(**_NON_DEFAULT_LENGTHS)
    configured = seal.configured_signal

    assert {series.name: (series.period, series.warmup_bars) for series in configured.signals} == {
        "ema_fast": (7, 7),
        "ema_slow": (20, 20),
        "rsi": (14, 15),
    }
    assert configured.exit_eligibility.countdown_decision_clocks == 8
    assert {name: (configured.parameters[name].value, configured.parameters[name].unit) for name in _NON_DEFAULT_LENGTHS} == {
        "fast_period": (7, "decision_bars"),
        "slow_period": (20, "decision_bars"),
        "hold_bars": (8, "decision_bars"),
    }
    assert all(configured.parameters[name].origin == "deploy_override" for name in _NON_DEFAULT_LENGTHS)
    # The corpus covers 5/10/5 only; the gates alone matching must not claim it.
    assert configured.parameters_match_validated_settings is False


def test_non_default_lengths_prove_their_build_but_stamp_the_corpus_uncovered() -> None:
    binding, _seal = _lengths_seal(**_NON_DEFAULT_LENGTHS)

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "PROVEN"
    assert proof.corpus_coverage == "UNCOVERED"


@pytest.mark.parametrize(
    ("field", "explanation"),
    [("signals", "signal semantics"), ("exit_eligibility", "exit-eligibility rule")],
)
def test_seal_claiming_the_default_series_or_hold_for_other_lengths_fails_closed(field: str, explanation: str) -> None:
    """A seal is checked against the contract its *own* parameters resolve.

    Comparing to the static default would refuse every honest non-default
    seal and admit this one, which attests to series the bot does not run.
    """
    binding, seal = _lengths_seal(**_NON_DEFAULT_LENGTHS)
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    tampered = seal.configured_signal.model_copy(update={field: getattr(contract, field)})
    binding = binding.model_copy(update={"sealed_program": seal.model_copy(update={"configured_signal": tampered})})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert explanation in proof.explanation


def test_seal_whose_parameters_no_longer_validate_fails_closed() -> None:
    binding, seal = _lengths_seal(**_NON_DEFAULT_LENGTHS)
    parameters = dict(seal.configured_signal.parameters)
    parameters["fast_period"] = parameters["fast_period"].model_copy(update={"value": 25})  # not below slow 20
    tampered = seal.configured_signal.model_copy(update={"parameters": parameters})
    binding = binding.model_copy(update={"sealed_program": seal.model_copy(update={"configured_signal": tampered})})

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert "no longer validate" in proof.explanation


# --- Issue #1735: the wiring half of the build digest -------------------------


def _rewritten_manifest(tmp_path: Path, program_key: str, **fields: str) -> Path:
    """A structurally valid manifest with one receipt's fields rewritten.

    Re-hashes the receipt rather than only overwriting a digest. A receipt
    whose ``receipt_hash`` no longer matches its payload fails the manifest's
    own validation, so the proof would reach UNPROVEN through the
    unreadable-evidence path and the comparison under test would never run --
    the test would pass for the wrong reason.
    """
    payload = json.loads(DEFAULT_QUALIFICATION_MANIFEST.read_text(encoding="utf-8"))
    for receipt in payload["receipts"]:
        if receipt["program_key"] == program_key:
            receipt.update(fields)
            receipt["receipt_hash"] = semantic_payload_hash(
                {name: value for name, value in receipt.items() if name != "receipt_hash"}
            )
    path = tmp_path / "rewritten-build-receipts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_committed_receipts_cover_the_wiring_of_every_sealed_program() -> None:
    """Every receipt's wiring digest matches the wiring on disk right now.

    This is the check that would have failed before #1735: an edit to a
    program's factory moved no digest at all, so a stale receipt still read
    PROVEN.
    """
    manifest = ProgramBuildQualificationManifest.model_validate_json(
        DEFAULT_QUALIFICATION_MANIFEST.read_text(encoding="utf-8")
    )

    for receipt in manifest.receipts:
        contract = _STRATEGY_REGISTRY[receipt.program_key].signal_program_contract
        assert contract is not None
        assert receipt.wiring_digest == running_wiring_digest(contract), receipt.program_key


def test_wiring_drift_is_reported_but_admitted_while_the_toggle_is_off(tmp_path: Path) -> None:
    manifest_path = _rewritten_manifest(tmp_path, "ema_crossover_signal", wiring_digest="a" * 64)

    proof = prove_running_program_build(
        _sealed_binding(), verified_at_ms=_NOW, manifest_path=manifest_path
    )

    assert proof.state == "PROVEN"
    assert proof.wiring == "DRIFTED"
    assert proof.next_step is not None
    # The operator has to be able to see *which* wiring is running, not just
    # that something drifted.
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    assert f"program-wiring-digest:{running_wiring_digest(contract)}" in proof.evidence_refs


def test_wiring_drift_fails_closed_once_the_toggle_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED", True)
    manifest_path = _rewritten_manifest(tmp_path, "ema_crossover_signal", wiring_digest="a" * 64)

    proof = prove_running_program_build(
        _sealed_binding(), verified_at_ms=_NOW, manifest_path=manifest_path
    )

    assert proof.state == "UNPROVEN"


def test_matching_wiring_proves_and_says_so(tmp_path: Path) -> None:
    proof = prove_running_program_build(_sealed_binding(), verified_at_ms=_NOW)

    assert proof.state == "PROVEN"
    assert proof.wiring == "MATCHED"
    assert proof.next_step is None


@pytest.mark.parametrize("enforced", [False, True])
def test_artifact_drift_still_fails_closed_whatever_the_toggle_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforced: bool
) -> None:
    """Issue #1735's scope note, encoded.

    The toggle governs only the newly-added wiring coverage. Drift in the
    already-covered artifacts is the admission control this gate exists
    for, and stays blocking in both toggle positions -- otherwise turning
    the toggle off would quietly widen what can start.
    """
    monkeypatch.setattr(settings, "SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED", enforced)
    manifest_path = _rewritten_manifest(
        tmp_path, "ema_crossover_signal", artifact_digest="b" * 64
    )

    proof = prove_running_program_build(
        _sealed_binding(), verified_at_ms=_NOW, manifest_path=manifest_path
    )

    assert proof.state == "UNPROVEN"


def test_an_unreadable_wiring_path_fails_closed_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission must return UNPROVEN, never escape as an internal error.

    Hashing the wiring reads files off disk and raises on a source tree
    missing a declared path, exactly as the artifact digest does. Computed
    outside the fail-closed handler it would propagate out of Start/Resume as
    a 500 instead of a refusal an operator can read.
    """
    # Sealed first: ``_sealed_binding`` proves the build itself, so breaking
    # the contract before that point fails in the fixture, not the assertion.
    binding = _sealed_binding()
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    contract = registration.signal_program_contract
    assert contract is not None
    monkeypatch.setitem(
        _STRATEGY_REGISTRY,
        "ema_crossover_signal",
        replace(
            registration,
            signal_program_contract=replace(
                contract, wiring_artifact_paths=("app/engine/strategy/programs/deleted_by_a_bad_deploy.py",)
            ),
        ),
    )

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert "unreadable" in proof.explanation


_GIT_PROVENANCE = {"commit_sha": "a" * 40, "dirty": False}


def test_receipt_hash_covers_git_provenance_when_present() -> None:
    """A receipt minted with git provenance binds it under the receipt hash,
    so recorded lineage cannot be edited in isolation any more than the
    digests can."""
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    payload = qualification_receipt_payload(
        program_key="ema_crossover_signal",
        contract=contract,
        qualified_at_ms=1,
        qualification_suite="tests/engine/strategy/test_x.py::test_y",
        git_provenance=_GIT_PROVENANCE,
    )

    receipt = ProgramBuildQualificationReceipt.model_validate(payload)

    assert receipt.git_provenance is not None
    assert receipt.git_provenance.commit_sha == "a" * 40
    assert receipt.git_provenance.dirty is False
    tampered = {**payload, "git_provenance": {**_GIT_PROVENANCE, "dirty": True}}
    with pytest.raises(ValidationError, match="receipt hash does not match"):
        ProgramBuildQualificationReceipt.model_validate(tampered)


def test_receipt_without_git_provenance_keeps_its_pre_provenance_hash() -> None:
    """Receipts minted before git provenance existed hash without the field.
    The committed manifest's receipts must stay valid byte-for-byte, so an
    absent provenance is omitted from the payload rather than serialized as
    null."""
    contract = _STRATEGY_REGISTRY["ema_crossover_signal"].signal_program_contract
    assert contract is not None
    payload = qualification_receipt_payload(
        program_key="ema_crossover_signal",
        contract=contract,
        qualified_at_ms=1,
        qualification_suite="tests/engine/strategy/test_x.py::test_y",
    )

    assert "git_provenance" not in payload
    receipt = ProgramBuildQualificationReceipt.model_validate(payload)
    assert receipt.git_provenance is None


# -- #2450: the proof names the code that is actually running --


def _ema_contract():
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    contract = registration.signal_program_contract
    assert contract is not None
    return contract


def _copied_source_tree(tmp_path: Path, *, drift_artifact: bool = False) -> Path:
    """A faithful copy of the proof's source files under ``tmp_path``.

    With ``drift_artifact``, one artifact file is edited after the copy --
    the bytes a ``git pull`` lands on the bind mount while the process keeps
    running the code it imported.
    """
    contract = _ema_contract()
    root = tmp_path.resolve()
    for relative in (*contract.artifact_paths, *contract.wiring_artifact_paths):
        source = admission_module._SERVICE_ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    if drift_artifact:
        drifted = root / "app/engine/strategy/algorithms/ema_crossover_signal.py"
        drifted.write_text("# drifted after import\n" + drifted.read_text())
    return root


def test_code_changed_on_disk_after_import_refuses_the_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#2450: a proof computed from disk used to stay PROVEN even when the disk
    held bytes the process never imported -- the deploy window between a
    ``git pull`` and the restart. The pull also brings the new bytes\' own
    qualification receipt, so the disk-side digests match it perfectly and
    the proof certified code this process is not running. The startup anchor
    refuses until the restart makes memory and disk one again."""
    record_imported_program_sources()  # the startup anchor, against the real tree
    binding = _sealed_binding()  # sealed and proven against the real tree, before the pull lands
    drifted_root = _copied_source_tree(tmp_path, drift_artifact=True)
    monkeypatch.setattr(admission_module, "_SERVICE_ROOT", drifted_root)
    # The pulled tree ships its own receipt for its own bytes.
    receipt = qualification_receipt_payload(
        program_key="ema_crossover_signal",
        contract=_ema_contract(),
        qualified_at_ms=_NOW,
        qualification_suite="drifted-tree-suite",
    )
    drifted_manifest = tmp_path / "drifted-receipts.json"
    drifted_manifest.write_text(json.dumps({"schema_version": 2, "receipts": [receipt]}))

    proof = prove_running_program_build(
        binding, verified_at_ms=_NOW, manifest_path=drifted_manifest
    )

    assert proof.state == "UNPROVEN"
    assert "Restart needed" in proof.explanation
    assert "artifact" in proof.explanation
    assert "Restart the service" in proof.next_step


def test_a_unchanged_disk_after_import_still_proves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#2450's companion: after the restart the running code is the code on
    disk, the digests match, and admission proceeds exactly as before."""
    record_imported_program_sources()
    binding = _sealed_binding()
    fresh_root = _copied_source_tree(tmp_path)
    monkeypatch.setattr(admission_module, "_SERVICE_ROOT", fresh_root)

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "PROVEN"
    assert proof.wiring == "MATCHED"


def test_the_snapshot_anchors_and_imports_every_declared_source() -> None:
    """#2450: a declared module that a program imports only lazily could
    otherwise load bytes a later ``git pull`` landed mid-flight. The startup
    anchor hashes every declared source and forces its import."""
    record_imported_program_sources()

    assert set(admission_module._IMPORTED_SOURCE_DIGESTS) == DECLARED_PROGRAM_SOURCE_PATHS
    assert all(relative[:-3].replace("/", ".") in sys.modules for relative in DECLARED_PROGRAM_SOURCE_PATHS)
    assert imported_source_drift(_ema_contract()) is None


def test_a_cached_program_module_and_newer_disk_bytes_refuse_the_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#2450 review (Codex + CodeRabbit): ``import app.main`` pulls in the
    strategy registry -- and with it the registered program modules -- long
    before the lifespan anchor ran. A ``git pull`` landing in that window let
    the old anchor hash the *new* disk bytes while ``import_module`` handed
    back the *cached old* module: the recorded digest then described code
    this process was never running, and a receipt minted for the pulled tree
    proved PROVEN against it. The anchor must refuse to record a disk digest
    for a module that is already cached -- it cannot know which bytes memory
    holds."""
    binding = _sealed_binding()  # sealed and proven against the real tree, before the pull lands
    admission_module._IMPORTED_SOURCE_DIGESTS.clear()  # a process whose anchor has not run yet
    root = _copied_source_tree(tmp_path)
    # Scope the anchor's work to exactly the two modules under control by
    # standing in a one-program registry whose contract declares only them;
    # both are pure leaves (standard-library imports only), so each can be
    # loaded from the copy without dragging the rest of the tree along.
    minimal_contract = replace(
        _ema_contract(),
        artifact_paths=("app/engine/strategy/signal_intent.py",),
        wiring_artifact_paths=("app/engine/strategy/normalized_gap.py",),
    )
    monkeypatch.setattr(
        admission_module,
        "_STRATEGY_REGISTRY",
        {
            "ema_crossover_signal": replace(
                _STRATEGY_REGISTRY["ema_crossover_signal"],
                signal_program_contract=minimal_contract,
            )
        },
    )
    # The process imported both declared modules from THIS tree earlier --
    # the exact state ``app.main``'s import chain leaves behind.
    for name, relative in (
        ("app.engine.strategy.signal_intent", "app/engine/strategy/signal_intent.py"),
        ("app.engine.strategy.normalized_gap", "app/engine/strategy/normalized_gap.py"),
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
        spec = importlib.util.spec_from_file_location(name, root / relative)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    # ...then the pull rewrote one of them after that import.
    (root / "app/engine/strategy/signal_intent.py").write_text(
        "# pulled after the import\n", encoding="utf-8"
    )
    monkeypatch.setattr(admission_module, "_SERVICE_ROOT", root)
    # The pulled tree ships its own receipt for its own bytes.
    receipt = qualification_receipt_payload(
        program_key="ema_crossover_signal",
        contract=minimal_contract,
        qualified_at_ms=_NOW,
        qualification_suite="cached-module-suite",
    )
    manifest = tmp_path / "cached-module-receipts.json"
    manifest.write_text(json.dumps({"schema_version": 2, "receipts": [receipt]}))

    proof = prove_running_program_build(binding, verified_at_ms=_NOW, manifest_path=manifest)

    assert proof.state == "UNPROVEN"
    assert "unreadable" in proof.explanation or "Restart needed" in proof.explanation


def test_a_source_read_that_fails_during_the_drift_comparison_refuses_with_a_restart_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2450 review (Codex P2): the drift comparison re-reads the declared
    sources off disk, and a checkout that removes or replaces one mid-``git
    pull`` makes that read raise from inside ``imported_source_drift``. A
    Start/Resume in that window must get an UNPROVEN refusal with the
    restart next_step -- never an internal error escaping the admission
    boundary."""

    def _torn_tree(contract: object) -> str:
        raise OSError("source vanished mid-pull")

    binding = _sealed_binding()
    monkeypatch.setattr(admission_module, "imported_source_drift", _torn_tree)

    proof = prove_running_program_build(binding, verified_at_ms=_NOW)

    assert proof.state == "UNPROVEN"
    assert "Restart needed" in proof.explanation
    assert "Restart the service" in proof.next_step
