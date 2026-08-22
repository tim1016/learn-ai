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
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract
from app.schemas.run_admission import ProgramBuildAdmissionFact, StrategyValidationAdmissionFact
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ResolvedSignalParameter,
    SealedBotProgram,
    SignalClockContract,
    SignalDataContract,
    seal_bot_program,
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
        if _semantic_hash(self.model_dump(mode="json", exclude={"receipt_hash"})) != self.receipt_hash:
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
    clears, so it stays a plain :class:`SignalProgramSealError`. A
    parameter set that no longer validates against the *currently*
    registered contract is a permanent legacy-data gap; no future retry of
    the same instance fixes it. Only this condition must clone a successor
    instance with explicit lineage instead of appending an inexact seal
    onto the original identity.
    """


def build_start_program_seal(
    binding: BrokerBotBinding,
    validation: StrategyValidationAdmissionFact,
    *,
    parameter_origins: dict[
        str, Literal["registered_default", "deploy_override", "deployment_symbol"]
    ]
    | None = None,
) -> SealedBotProgram | None:
    """Author a new v2 seal for a registered Signal Program.

    Non-program compatibility strategies return ``None``.  A registered
    program with incomplete validation or account identity fails closed.
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

    validated = registration.param_schema.model_validate(
        {**(binding.strategy_params or {}), "symbol": binding.symbol}
    )
    effective = validated.model_dump(mode="json")
    defaults = registration.param_schema().model_dump(mode="json")
    origins = parameter_origins or {}
    parameters: dict[str, ResolvedSignalParameter] = {}
    for name, value in effective.items():
        if name == "symbol":
            origin: Literal[
                "registered_default", "deploy_override", "deployment_symbol"
            ] = "deployment_symbol"
        else:
            origin = origins.get(
                name,
                "registered_default" if value == defaults.get(name) else "deploy_override",
            )
        parameters[name] = ResolvedSignalParameter(
            value=value,
            unit=contract.parameter_units[name],
            origin=origin,
        )

    configured = ConfiguredSignalProgramSeal(
        program_key=binding.strategy_key,
        program_version=contract.program_version,
        golden_trace_root=contract.golden_trace_root,
        parameters=parameters,
        parameters_match_validated_settings=_parameters_match(contract, effective),
        data=SignalDataContract(
            provider=contract.provider,
            symbol=binding.symbol.upper(),
            base_timeframe_ms=contract.base_timeframe_ms,
            decision_timeframe_ms=contract.decision_timeframe_ms,
        ),
        clock=SignalClockContract(
            use_rth=binding.use_rth,
            warmup_lookback_days=contract.warmup_lookback_days,
        ),
    )
    configured_hash = configured.semantic_hash()
    return seal_bot_program(
        strategy_instance_id=binding.strategy_instance_id,
        configured_signal=configured,
        configured_signal_hash=configured_hash,
        broker=binding.broker,
        sealed_account_id=binding.sealed_account_id,
        mode=binding.mode,
        action_plan=binding.action_plan.model_dump(mode="json"),
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
    addition — a persisted parameter set that no longer validates against
    the *currently* registered contract is distinguished from every other
    reason sealing can fail today (no sealed account yet, no current
    validation evidence): those remain transient and return ``None`` here,
    exactly like an un-registered program does, so the caller's existing
    generic ``PROGRAM_BUILD_UNPROVEN`` handling still applies and a later
    Resume attempt can still succeed once the precondition clears. A
    parameter mismatch instead raises
    :class:`LegacyProgramUnreconstructibleError`, because no future retry of
    *this* instance can fix it — the caller must clone a successor.
    """
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    if registration is None or registration.signal_program_factory is None:
        return None
    try:
        registration.param_schema.model_validate({**(binding.strategy_params or {}), "symbol": binding.symbol})
    except ValidationError as exc:
        raise LegacyProgramUnreconstructibleError(
            f"Strategy instance '{binding.strategy_instance_id}' persisted parameters no "
            f"longer validate against the currently registered '{binding.strategy_key}' contract."
        ) from exc
    try:
        return build_start_program_seal(binding, validation, parameter_origins=binding.strategy_param_origins)
    except SignalProgramSealError:
        return None


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
    return _semantic_hash(entries)


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
    return {**payload, "receipt_hash": _semantic_hash(payload)}


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


def _semantic_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
