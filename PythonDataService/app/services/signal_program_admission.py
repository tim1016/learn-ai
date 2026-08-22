"""Seal configured Signal Programs and prove their running build at admission.

This module is the single deep boundary for three related questions:

* what semantic program did the user configure;
* what bot/account/validation choice was bound to it; and
* do the bytes loaded by this process have a golden-qualification receipt for
  that exact program version and trace root?

The legacy bot configuration hash is deliberately outside this module.  The
v2 seal is append-only evidence and never rewrites v1 identity bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract
from app.schemas.run_admission import ProgramBuildAdmissionFact, StrategyValidationAdmissionFact
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ParameterOrigin,
    ResolvedSignalParameter,
    SealedBotProgram,
    SignalClockContract,
    SignalDataContract,
    seal_bot_program,
    semantic_payload_hash,
)
from app.services.bot_binding_repository import BrokerBotBinding

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUALIFICATION_MANIFEST = _SERVICE_ROOT / "app/data/signal_program_build_receipts.json"


class ProgramBuildQualificationReceipt(BaseModel):
    """Golden-job output binding executable bytes to trace semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    program_key: str
    program_version: str
    golden_trace_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_suite: str
    qualified_at_ms: int = Field(ge=0)
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> ProgramBuildQualificationReceipt:
        if semantic_payload_hash(self.model_dump(mode="json", exclude={"receipt_hash"})) != self.receipt_hash:
            raise ValueError("qualification receipt hash does not match its payload")
        return self


class ProgramBuildQualificationManifest(BaseModel):
    """Closed committed set of currently qualified Signal Program builds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    receipts: tuple[ProgramBuildQualificationReceipt, ...]


class SignalProgramSealError(ValueError):
    """A new instance cannot produce a complete semantic v2 seal."""


class LegacyProgramUnreconstructibleError(SignalProgramSealError):
    """A legacy instance's persisted v1 parameters cannot seal exactly.

    PRD Sec 11.5 draws a hard line here: every other missing-precondition
    case (no sealed account yet, no current validation evidence) is
    transient — the same Resume attempt succeeds once the precondition
    clears, so it stays a plain :class:`SignalProgramSealError`. Two
    conditions are permanent legacy-data gaps instead — no future retry of
    the same instance fixes either one, so both clone a successor instance
    with explicit lineage rather than append an inexact seal onto the
    original identity:

    * the persisted parameter set no longer validates against the
      *currently* registered contract; or
    * a persisted parameter has no factual origin — it was supplied at
      deploy time (so it is not the schema default by omission) but no
      origin was ever recorded for it, and guessing from a value-vs-
      current-default comparison is exactly the unsound inference this
      error exists to refuse (see :func:`_legacy_parameter_origins`).
    """


def build_start_program_seal(
    binding: BrokerBotBinding,
    validation: StrategyValidationAdmissionFact,
    *,
    parameter_origins: dict[str, ParameterOrigin]
    | None = None,
) -> SealedBotProgram | None:
    """Author a new v2 seal for a registered Signal Program.

    Non-program compatibility strategies return ``None``.  A registered
    program with incomplete validation or account identity fails closed.

    This function never *infers* an origin by comparing an effective value
    to the *currently* registered default, because a value matching
    today's default does not prove it was never an explicit override — the
    default may have drifted since deploy time. Two sources decide origin
    instead, both factual for *this exact* seal:

    * a parameter name absent from ``binding.strategy_params`` altogether
      was never supplied by the caller for this binding — Pydantic filled
      it from this exact ``param_schema``'s default, right now, so
      ``"registered_default"`` is a fact about this seal's own
      construction, not a guess reconstructed from possibly-stale history;
    * a parameter name present in ``binding.strategy_params`` was supplied
      by the caller, so its origin is ambiguous (an explicit choice that
      happens to equal today's default looks identical to an unset one) and
      ``parameter_origins`` must carry an explicit entry for it — a missing
      entry fails closed with :class:`SignalProgramSealError`.

    Fresh deploys through ``paper_deploy_service`` always supply a complete
    ``parameter_origins`` mapping. A caller reconstructing a pre-v2 instance
    with no recorded origins must build a complete mapping first — see
    :func:`_legacy_parameter_origins`, used only by
    :func:`reconstruct_legacy_program_seal`.
    """
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    if registration is None or registration.signal_program_factory is None:
        return None
    contract = registration.signal_program_contract
    if contract is None:
        raise SignalProgramSealError("registered Signal Program has no qualification contract")
    if binding.sealed_account_id is None:
        raise SignalProgramSealError("Signal Program seal requires an exact account identity")
    if validation.event_id is None or validation.evidence_snapshot_sha256 is None:
        raise SignalProgramSealError("Signal Program seal requires immutable validation evidence")

    requested = binding.strategy_params or {}
    validated = registration.param_schema.model_validate({**requested, "symbol": binding.symbol})
    effective = validated.model_dump(mode="json")
    origins = parameter_origins or {}
    parameters: dict[str, ResolvedSignalParameter] = {}
    for name, value in effective.items():
        if name == "symbol":
            origin: Literal[
                "registered_default", "deploy_override", "deployment_symbol"
            ] = "deployment_symbol"
        elif name not in requested:
            origin = "registered_default"
        elif name in origins:
            origin = origins[name]
        else:
            raise SignalProgramSealError(
                f"Signal Program seal requires an explicit origin for parameter '{name}'"
            )
        parameters[name] = ResolvedSignalParameter(
            value=value,
            unit=contract.parameter_units[name],
            origin=origin,
        )

    # The cadence the seal attests to must be the cadence the bot will run,
    # not the one the contract was qualified at. `resolution_minutes` is a
    # deploy-overridable parameter for every program except the fixed-cadence
    # EMA one, and the registry factory derives the session clock from the
    # *resolved* value -- so copying `contract.decision_timeframe_ms` into the
    # hash made the immutable attestation describe a different decision stream
    # than the one executing. `parameters_match_validated_settings` records
    # that an override happened, but a flag beside a wrong hashed field does
    # not make the field right.
    #
    # Read back from the constructed program rather than recomputing
    # `resolution_minutes * 60_000` here: the factory is the authority on how
    # parameters become a decision clock, and a second copy of that arithmetic
    # in this module is exactly the drift the seal exists to detect.
    running_program = registration.signal_program_factory(validated)
    decision_timeframe_ms = running_program.session.timeframe_ms

    configured = ConfiguredSignalProgramSeal(
        program_key=binding.strategy_key,
        program_version=contract.program_version,
        protocol_version=contract.protocol_version,
        parameter_schema_version=contract.parameter_schema_version,
        golden_trace_root=contract.golden_trace_root,
        parameters=parameters,
        parameters_match_validated_settings=_parameters_match(contract, effective),
        data=SignalDataContract(
            provider=contract.provider,
            symbol=binding.symbol.upper(),
            base_timeframe_ms=contract.base_timeframe_ms,
            decision_timeframe_ms=decision_timeframe_ms,
        ),
        clock=SignalClockContract(
            use_rth=binding.use_rth,
            warmup_lookback_days=contract.warmup_lookback_days,
        ),
        # Copied straight from the registry contract — the same objects, not
        # a re-derivation — so these can never fall out of sync with it.
        signals=contract.signals,
        decision_streams=contract.decision_streams,
        bar_integrity=contract.bar_integrity,
        exit_eligibility=contract.exit_eligibility,
        numerical_provenance=contract.numerical_provenance,
    )
    configured_hash = configured.semantic_hash()
    return seal_bot_program(
        strategy_instance_id=binding.strategy_instance_id,
        configured_signal=configured,
        configured_signal_hash=configured_hash,
        broker=binding.broker,
        sealed_account_id=binding.sealed_account_id,
        mode=binding.mode,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
        carryover_policy=binding.carryover_policy,
        validation_event_id=validation.event_id,
        validation_snapshot_sha256=validation.evidence_snapshot_sha256,
        sealed_at_ms=binding.created_at_ms,
    )


_LEGACY_CLONE_ID_SUFFIX_LEN = 12


def legacy_migration_clone_instance_id(strategy_instance_id: str) -> str:
    """Deterministically derive the one PRD Sec 11.5 clone id for an instance.

    Pure and stable: the same source instance always yields the same clone
    id, so a repeated Resume attempt against an unreconstructible legacy
    instance can never mint a second clone — the caller's create-once write
    (:meth:`BotBindingRepository.ensure_legacy_migration_clone_lineage`)
    collapses onto the same path every time. The suffix is a content hash of
    the source id rather than a counter or timestamp, which keeps this
    function callable from a pure preview with no storage access.
    """
    digest = hashlib.sha256(strategy_instance_id.encode("utf-8")).hexdigest()[:_LEGACY_CLONE_ID_SUFFIX_LEN]
    suffix = f"-legacy2-{digest}"
    budget = max(1, 128 - len(suffix))
    return f"{strategy_instance_id[:budget]}{suffix}"


def reconstruct_legacy_program_seal(
    binding: BrokerBotBinding,
    validation: StrategyValidationAdmissionFact,
) -> SealedBotProgram | None:
    """Attempt to append an exact v2 seal to an instance that predates it.

    PRD Sec 11.5: a missing seal on an *existing* strategy instance means it
    was deployed before the v2 seal format existed, not that it is a fresh
    Start. This mirrors :func:`build_start_program_seal` exactly, with one
    addition — two permanent legacy-data gaps are distinguished from every
    other reason sealing can fail today (no sealed account yet, no current
    validation evidence): those remain transient and return ``None`` here,
    exactly like an un-registered program does, so the caller's existing
    generic ``PROGRAM_BUILD_UNPROVEN`` handling still applies and a later
    Resume attempt can still succeed once the precondition clears. Either a
    persisted parameter set that no longer validates against the
    *currently* registered contract, or a persisted parameter with no
    factual origin, instead raises
    :class:`LegacyProgramUnreconstructibleError`, because no future retry of
    *this* instance can fix it — the caller must clone a successor.

    Pre-v2 instances never recorded ``strategy_param_origins``, so this is
    also the one place a complete origin mapping is assembled from
    whatever facts are available — see :func:`_legacy_parameter_origins`,
    which refuses (rather than guesses) when a parameter has none.
    """
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    if registration is None or registration.signal_program_factory is None:
        return None
    try:
        validated = registration.param_schema.model_validate(
            {**(binding.strategy_params or {}), "symbol": binding.symbol}
        )
    except ValidationError as exc:
        raise LegacyProgramUnreconstructibleError(
            f"Strategy instance '{binding.strategy_instance_id}' persisted parameters no "
            f"longer validate against the currently registered '{binding.strategy_key}' contract."
        ) from exc
    origins = _legacy_parameter_origins(
        strategy_instance_id=binding.strategy_instance_id,
        strategy_key=binding.strategy_key,
        effective=validated.model_dump(mode="json"),
        requested=binding.strategy_params or {},
        recorded_origins=binding.strategy_param_origins,
    )
    try:
        return build_start_program_seal(binding, validation, parameter_origins=origins)
    except SignalProgramSealError:
        return None


def _legacy_parameter_origins(
    *,
    strategy_instance_id: str,
    strategy_key: str,
    effective: dict[str, Any],
    requested: dict[str, Any],
    recorded_origins: dict[str, ParameterOrigin]
    | None,
) -> dict[str, ParameterOrigin]:
    """Build a complete, *factual* origin map for a pre-v2 instance, or refuse.

    Called only from :func:`reconstruct_legacy_program_seal`. Every entry
    here is a fact about this exact instance, never an inference from
    comparing an effective value to today's registered default — a value
    matching the current default does not prove it was never an explicit
    deploy-time override, because the default can drift after deploy time
    (and an old override can later be adopted as the new default). Two
    factual sources fill this map:

    * ``recorded_origins`` already carries an explicit entry for the
      parameter — an earlier partial migration, or a post-seal deploy,
      recorded it — so that recorded origin is used verbatim; or
    * the parameter name is genuinely absent from ``requested``
      (``binding.strategy_params``) — the caller never supplied it, so
      Pydantic filled it from *this exact* ``param_schema``'s default just
      now, the same way :func:`build_start_program_seal` treats an
      unsupplied parameter on a fresh deploy. That is a fact about this
      seal's own construction, not a guess reconstructed from history.

    A parameter present in ``requested`` with no recorded origin has no
    factual source at all: it was supplied explicitly at some past deploy,
    but which value-vs-default choice that was is lost. Guessing from
    today's default is precisely the unsound inference this function
    exists to refuse, so it raises
    :class:`LegacyProgramUnreconstructibleError` instead — routing the
    caller to the clone path (PRD Sec 11.5) rather than sealing a guess as
    exact identity.
    """
    recorded = recorded_origins or {}
    origins: dict[str, ParameterOrigin] = {}
    unresolved: list[str] = []
    for name in effective:
        if name == "symbol":
            continue
        if name in recorded:
            origins[name] = recorded[name]
        elif name not in requested:
            origins[name] = "registered_default"
        else:
            unresolved.append(name)
    if unresolved:
        raise LegacyProgramUnreconstructibleError(
            f"Strategy instance '{strategy_instance_id}' has no recorded origin for "
            f"parameter(s) {sorted(unresolved)} of the currently registered "
            f"'{strategy_key}' contract. Each was supplied explicitly at some past "
            "deploy, but its deploy-time origin was never recorded and cannot be "
            "reconstructed from today's registered default."
        )
    return origins


def prove_running_program_build(
    binding: BrokerBotBinding,
    *,
    verified_at_ms: int,
    manifest_path: Path = DEFAULT_QUALIFICATION_MANIFEST,
) -> ProgramBuildAdmissionFact:
    """Re-hash loaded artifacts and compare one closed qualification receipt."""
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    if registration is None or registration.signal_program_factory is None:
        return ProgramBuildAdmissionFact(
            state="NOT_APPLICABLE",
            program_key=binding.strategy_key,
            verified_at_ms=verified_at_ms,
            explanation="This compatibility strategy has no registered Signal Program.",
        )
    contract = registration.signal_program_contract
    seal = binding.sealed_program
    if contract is None or seal is None:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation=(
                "The instance has no complete v2 Signal Program seal. Legacy bytes remain inspectable, "
                "but this instance must be cloned before it can Resume."
            ),
        )
    configured = seal.configured_signal
    if (
        seal.strategy_instance_id != binding.strategy_instance_id
        or seal.sealed_account_id != binding.sealed_account_id
        or seal.mode != binding.mode
        or configured.program_key != binding.strategy_key
        or configured.program_version != contract.program_version
        or configured.golden_trace_root != contract.golden_trace_root
        # #1729 AC4 "provider" proof: the sealed qualification-lineage
        # identity (PRD Sec 11.6) must still be present and unchanged
        # against the currently registered contract. This is not a
        # live-feed parity gate — see SignalDataContract.provider's
        # docstring — just an identity check at the same cadence as the
        # program_version/golden_trace_root checks above.
        or configured.data.provider != contract.provider
        # The decision cadence is not a descriptive field: `golden_trace_root`
        # pins one decision *stream*, and a program clocked at a different
        # resolution reads different bars and reaches different decisions, so
        # the qualification corpus does not describe it at all. The seal now
        # records the cadence the program will really run (see
        # `build_start_program_seal`), which makes this comparison meaningful
        # -- previously both sides were copied from the same contract constant
        # and it could never fail. A deploy that overrides `resolution_minutes`
        # is therefore UNPROVEN rather than silently PROVEN against evidence
        # gathered at another cadence.
        # The sealed-semantics completeness fix (sibling to #1729): every
        # field newly widened onto the seal (PRD Sec 11.1) must still match
        # the currently registered contract, at the same cadence as the
        # checks above — a stale or hand-edited seal on any of these must
        # fail build-proof just as surely as a stale program_version would.
        or configured.protocol_version != contract.protocol_version
        or configured.parameter_schema_version != contract.parameter_schema_version
        or configured.signals != contract.signals
        or configured.decision_streams != contract.decision_streams
        or configured.bar_integrity != contract.bar_integrity
        or configured.exit_eligibility != contract.exit_eligibility
        or configured.numerical_provenance != contract.numerical_provenance
        or configured.data.decision_timeframe_ms != contract.decision_timeframe_ms
    ):
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation="The stored Signal Program seal does not match this instance or registry contract.",
        )
    try:
        running_digest = running_artifact_digest(contract)
        manifest = ProgramBuildQualificationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation=f"Program qualification evidence is unreadable: {type(exc).__name__}.",
        )
    receipt = next(
        (
            candidate
            for candidate in manifest.receipts
            if candidate.program_key == binding.strategy_key
            and candidate.program_version == configured.program_version
            and candidate.golden_trace_root == configured.golden_trace_root
            and candidate.artifact_digest == running_digest
        ),
        None,
    )
    if receipt is None:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation="The running artifact digest has no compatible golden-qualification receipt.",
        )
    return ProgramBuildAdmissionFact(
        state="PROVEN",
        program_key=binding.strategy_key,
        program_version=configured.program_version,
        golden_trace_root=configured.golden_trace_root,
        running_artifact_digest=running_digest,
        qualification_receipt_hash=receipt.receipt_hash,
        verified_at_ms=verified_at_ms,
        evidence_refs=(
            f"signal-program-seal:{seal.bot_configuration_hash}",
            f"program-build-receipt:{receipt.receipt_hash}",
            f"program-build-digest:{running_digest}",
        ),
        explanation="The running Signal Program build matches its golden qualification receipt.",
    )


def running_artifact_digest(contract: SignalProgramContract) -> str:
    """Hash the closed executable artifact set named by the registry contract."""
    entries: list[dict[str, str]] = []
    for relative in contract.artifact_paths:
        candidate = (_SERVICE_ROOT / relative).resolve()
        if _SERVICE_ROOT not in candidate.parents or not candidate.is_file():
            raise ValueError(f"invalid Signal Program artifact path: {relative}")
        entries.append({"path": relative, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()})
    return semantic_payload_hash(entries)


def qualification_receipt_payload(
    *,
    program_key: str,
    contract: SignalProgramContract,
    qualified_at_ms: int,
    qualification_suite: str,
) -> dict[str, Any]:
    """Return generator output for the committed qualification manifest."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "program_key": program_key,
        "program_version": contract.program_version,
        "golden_trace_root": contract.golden_trace_root,
        "artifact_digest": running_artifact_digest(contract),
        "qualification_suite": qualification_suite,
        "qualified_at_ms": qualified_at_ms,
    }
    return {**payload, "receipt_hash": semantic_payload_hash(payload)}


def _parameters_match(contract: SignalProgramContract, effective: dict[str, Any]) -> bool:
    return (
        str(effective.get("symbol", "")).upper() in contract.validated_symbols
        and all(effective.get(name) == value for name, value in contract.validated_settings.items())
    )


def _unproven(
    program_key: str,
    verified_at_ms: int,
    *,
    explanation: str,
) -> ProgramBuildAdmissionFact:
    return ProgramBuildAdmissionFact(
        state="UNPROVEN",
        program_key=program_key,
        verified_at_ms=verified_at_ms,
        explanation=explanation,
        next_step="Run golden qualification for these bytes, or deploy a newly sealed compatible instance.",
    )


__all__ = [
    "DEFAULT_QUALIFICATION_MANIFEST",
    "LegacyProgramUnreconstructibleError",
    "ProgramBuildQualificationManifest",
    "ProgramBuildQualificationReceipt",
    "SignalProgramSealError",
    "build_start_program_seal",
    "legacy_migration_clone_instance_id",
    "prove_running_program_build",
    "qualification_receipt_payload",
    "reconstruct_legacy_program_seal",
    "running_artifact_digest",
]
