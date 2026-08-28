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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config import settings
from app.engine.strategy.params import decision_timeframe_ms_for
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

    schema_version: Literal[2] = 2
    program_key: str
    program_version: str
    golden_trace_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Issue #1735. Separate from ``artifact_digest`` so a mismatch says which
    # half moved; the receipt hash covers both, so neither can be edited in
    # isolation.
    wiring_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
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

    schema_version: Literal[2] = 2
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
    # not the one the contract was qualified at: `resolution_minutes` is
    # deploy-overridable on every program but the fixed-cadence EMA one, so
    # copying `contract.decision_timeframe_ms` into the hash made the
    # immutable attestation describe a different decision stream than the one
    # executing. `decision_timeframe_ms_for` is the one authority the registry
    # factories also build their sessions from, so the sealed cadence and the
    # running cadence cannot diverge.
    decision_timeframe_ms = decision_timeframe_ms_for(
        validated, qualified_ms=contract.decision_timeframe_ms
    )

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


# The remedy for wiring drift, stated once. Both the live proof and the
# frozen-run replay in `panel_projection_service` hand this to an operator;
# two copies of one instruction is two instructions that can disagree.
WIRING_DRIFT_NEXT_STEP = (
    "Re-run golden qualification for this program so its receipt covers the current wiring."
)


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
    failed = next((check for check in _seal_checks(binding, seal, contract) if not check.holds), None)
    if failed is not None:
        return _unproven(binding.strategy_key, verified_at_ms, explanation=failed.explanation)
    try:
        running_digest = running_artifact_digest(contract)
        # Hashed here, with the artifact digest, rather than after the receipt
        # lookup where it is used: both read files off disk and both can raise
        # on a source tree missing a declared path, and an admission check that
        # escapes as an internal error is not failing closed.
        running_wiring = running_wiring_digest(contract)
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
    # The wiring half is checked separately, and *after* the receipt lookup, so
    # the two drifts can never be confused. A drift in the artifacts above has
    # already failed closed by this point regardless of the toggle -- that is
    # the admission control this PRD was built around, and issue #1735's scope
    # note keeps it blocking. Only this newly-covered half is toggle-governed.
    wiring_matches = receipt.wiring_digest == running_wiring
    if not wiring_matches and settings.SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation="The running strategy wiring does not match its golden-qualification receipt.",
        )
    return ProgramBuildAdmissionFact(
        state="PROVEN",
        program_key=binding.strategy_key,
        program_version=configured.program_version,
        golden_trace_root=configured.golden_trace_root,
        running_artifact_digest=running_digest,
        qualification_receipt_hash=receipt.receipt_hash,
        verified_at_ms=verified_at_ms,
        wiring="MATCHED" if wiring_matches else "DRIFTED",
        evidence_refs=(
            f"signal-program-seal:{seal.bot_configuration_hash}",
            f"program-build-receipt:{receipt.receipt_hash}",
            f"program-build-digest:{running_digest}",
            f"program-wiring-digest:{running_wiring}",
        ),
        explanation=(
            "The running Signal Program build matches its golden qualification receipt."
            if wiring_matches
            else (
                "The running Signal Program math matches its golden qualification receipt, but "
                "the strategy wiring has changed since that receipt was minted."
            )
        ),
        next_step=(
            None
            if wiring_matches
            else WIRING_DRIFT_NEXT_STEP
        ),
    )


def _digest_paths(paths: tuple[str, ...]) -> str:
    """Hash a closed, ordered set of service-relative source files."""
    entries: list[dict[str, str]] = []
    for relative in paths:
        candidate = (_SERVICE_ROOT / relative).resolve()
        if _SERVICE_ROOT not in candidate.parents or not candidate.is_file():
            raise ValueError(f"invalid Signal Program artifact path: {relative}")
        entries.append({"path": relative, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()})
    return semantic_payload_hash(entries)


def running_artifact_digest(contract: SignalProgramContract) -> str:
    """Hash the closed executable artifact set named by the registry contract."""
    return _digest_paths(contract.artifact_paths)


def running_wiring_digest(contract: SignalProgramContract) -> str:
    """Hash the code that wires this program's parameters to its math.

    Hashed apart from :func:`running_artifact_digest` rather than folded into
    it, so a mismatch is attributable to one half or the other. That is what
    lets ``SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED`` warn about wiring drift
    while a drift in the already-covered artifacts keeps failing closed
    (issue #1735). Keeping the two apart also leaves every receipt minted
    before this existed byte-stable in its ``artifact_digest``.
    """
    return _digest_paths(contract.wiring_artifact_paths)


def qualification_receipt_payload(
    *,
    program_key: str,
    contract: SignalProgramContract,
    qualified_at_ms: int,
    qualification_suite: str,
) -> dict[str, Any]:
    """Return generator output for the committed qualification manifest."""
    payload: dict[str, Any] = {
        "schema_version": 2,
        "program_key": program_key,
        "program_version": contract.program_version,
        "golden_trace_root": contract.golden_trace_root,
        "artifact_digest": running_artifact_digest(contract),
        "wiring_digest": running_wiring_digest(contract),
        "qualification_suite": qualification_suite,
        "qualified_at_ms": qualified_at_ms,
    }
    return {**payload, "receipt_hash": semantic_payload_hash(payload)}


@dataclass(frozen=True)
class _SealCheck:
    """One named agreement between a stored seal and the live registry.

    Replaces a seventeen-term ``or`` chain whose comments had drifted away
    from the conditions they described, and whose every failure collapsed
    into one sentence -- an operator who overrode a parameter read the same
    message as one whose account identity had drifted. Each row carries its
    own explanation, so the refusal says which agreement broke.
    """

    name: str
    holds: bool
    explanation: str


def _seal_checks(
    binding: BrokerBotBinding,
    seal: SealedBotProgram,
    contract: SignalProgramContract,
) -> tuple[_SealCheck, ...]:
    """Every agreement a sealed program must still hold to prove its build.

    Evaluated in order; the first failure is the reported reason. Adding a
    field to the seal means adding a row here, which is why this is a table
    rather than a boolean -- the previous shape made each widening one more
    clause in an expression nobody could scan, and two fields
    (``warmup_lookback_days``, ``base_timeframe_ms``) were widened onto the
    seal without ever being gated.
    """
    configured = seal.configured_signal
    return (
        _SealCheck(
            "strategy_instance_id",
            seal.strategy_instance_id == binding.strategy_instance_id,
            "The stored Signal Program seal belongs to a different strategy instance.",
        ),
        _SealCheck(
            "sealed_account_id",
            seal.sealed_account_id == binding.sealed_account_id,
            "The stored Signal Program seal was issued for a different account.",
        ),
        _SealCheck(
            "mode",
            seal.mode == binding.mode,
            "The stored Signal Program seal was issued for a different execution mode.",
        ),
        _SealCheck(
            "program_key",
            configured.program_key == binding.strategy_key,
            "The stored Signal Program seal names a different program than this instance runs.",
        ),
        _SealCheck(
            "program_version",
            configured.program_version == contract.program_version,
            "The registered program version has moved since this instance was sealed.",
        ),
        _SealCheck(
            "golden_trace_root",
            configured.golden_trace_root == contract.golden_trace_root,
            "The registered golden trace root has moved since this instance was sealed.",
        ),
        # #1729 AC4 "provider" proof: the sealed qualification-lineage identity
        # (PRD Sec 11.6) must still be present and unchanged against the
        # currently registered contract. Not a live-feed parity gate -- see
        # SignalDataContract.provider's docstring.
        _SealCheck(
            "data.provider",
            configured.data.provider == contract.provider,
            "The sealed qualification lineage no longer matches the registered contract.",
        ),
        # The sealed-semantics completeness fix (sibling to #1729): every field
        # widened onto the seal (PRD Sec 11.1) must still match the registered
        # contract, at the same cadence as the identity checks above.
        _SealCheck(
            "protocol_version",
            configured.protocol_version == contract.protocol_version,
            "The registered session protocol has moved since this instance was sealed.",
        ),
        _SealCheck(
            "parameter_schema_version",
            configured.parameter_schema_version == contract.parameter_schema_version,
            "The registered parameter schema has moved since this instance was sealed.",
        ),
        _SealCheck(
            "signals",
            configured.signals == contract.signals,
            "The registered signal semantics have moved since this instance was sealed.",
        ),
        _SealCheck(
            "decision_streams",
            configured.decision_streams == contract.decision_streams,
            "The registered decision streams have moved since this instance was sealed.",
        ),
        _SealCheck(
            "bar_integrity",
            configured.bar_integrity == contract.bar_integrity,
            "The registered bar-integrity contract has moved since this instance was sealed.",
        ),
        _SealCheck(
            "exit_eligibility",
            configured.exit_eligibility == contract.exit_eligibility,
            "The registered exit-eligibility rule has moved since this instance was sealed.",
        ),
        _SealCheck(
            "numerical_provenance",
            configured.numerical_provenance == contract.numerical_provenance,
            "The registered numerical provenance has moved since this instance was sealed.",
        ),
        _SealCheck(
            "data.base_timeframe_ms",
            configured.data.base_timeframe_ms == contract.base_timeframe_ms,
            "The sealed source-bar cadence no longer matches the registered contract.",
        ),
        _SealCheck(
            "clock.warmup_lookback_days",
            configured.clock.warmup_lookback_days == contract.warmup_lookback_days,
            "The sealed warmup requirement no longer matches the registered contract.",
        ),
        # `golden_trace_root` pins one decision *stream*, produced by specific
        # math at a specific cadence over specific symbols. Any resolved value
        # outside `validated_settings`/`validated_symbols` means the corpus
        # does not describe the running program, so it cannot prove its build.
        # This subsumes the decision cadence: `resolution_minutes` is itself a
        # validated setting on every tunable program, so gating cadence alone
        # refused a 30-minute override while admitting an overridden RSI
        # threshold against evidence gathered at another one.
        _SealCheck(
            "parameters_match_validated_settings",
            configured.parameters_match_validated_settings,
            "This instance resolved parameters the golden qualification corpus does not cover.",
        ),
    )


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
    "running_wiring_digest",
]
