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
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config import settings
from app.engine.strategy.params import StrategyParamsBase, decision_timeframe_ms_for
from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract, StrategyRegistration
from app.research.golden_search.qualifications import (
    Coverage,
    QualificationLookup,
    load_qualification_evidence,
    params_sha256,
    registry_point_matches,
    resolve_coverage,
)
from app.schemas.run_admission import (
    QUALIFICATION_COVERED,
    QUALIFICATION_NOT_REVERIFIED,
    QUALIFICATION_UNJUDGEABLE,
    REGISTRY_POINT_COVERED,
    ProgramBuildAdmissionFact,
    StrategyValidationAdmissionFact,
    proven_build_copy,
)
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ParameterOrigin,
    ProgramBuildGitProvenance,
    ResolvedSignalParameter,
    SealedBotProgram,
    SignalClockContract,
    SignalDataContract,
    seal_bot_program,
    semantic_payload_hash,
    strip_absent_git_provenance,
)
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.program_source_anchor import (
    _IMPORTED_SOURCE_DIGESTS,
    record_imported_program_sources,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

logger = logging.getLogger(__name__)

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
    qualified_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    # Recorded lineage of the qualified bytes (see ProgramBuildGitProvenance).
    # Optional so the receipts minted before it existed keep their committed
    # hashes: an absent provenance is omitted from the hashed payload, never
    # serialized as null into it.
    git_provenance: ProgramBuildGitProvenance | None = None
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> ProgramBuildQualificationReceipt:
        payload = strip_absent_git_provenance(self.model_dump(mode="json", exclude={"receipt_hash"}))
        if semantic_payload_hash(payload) != self.receipt_hash:
            raise ValueError("qualification receipt hash does not match its payload")
        return self


class ProgramBuildQualificationManifest(BaseModel):
    """Closed committed set of currently qualified Signal Program builds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    receipts: tuple[ProgramBuildQualificationReceipt, ...]


class SignalProgramSealError(ValueError):
    """A new instance cannot produce a complete semantic v2 seal.

    The message is the owner-facing cause; ``next_step`` its remedy when the
    cause names one (H11: an unreadable validation store must say so, never
    be reported as a missing seal).
    """

    def __init__(self, cause: str, *, next_step: str | None = None) -> None:
        super().__init__(cause)
        self.next_step = next_step


def build_start_program_seal(
    binding: BrokerBotBinding,
    validation: StrategyValidationAdmissionFact,
    *,
    parameter_origins: dict[str, ParameterOrigin]
    | None = None,
    coverage: Coverage | None = None,
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
    ``parameter_origins`` mapping. Historical seals remain readable; new
    deployment always seals a fresh identity with current explicit choices.

    ``coverage`` is :func:`resolve_admission_coverage`'s answer for this
    binding, read before this synchronous call (#2696). When the registry's
    validated point does not cover the parameters but a ready Golden Search
    qualification does, the seal pins that qualification's id; the proof
    re-verifies exactly that id, never whichever version is default later.
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
        # The validation fact already names why it has no event: that is the
        # cause the owner can act on, not the seal it prevents.
        raise SignalProgramSealError(validation.explanation, next_step=validation.next_step)

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

    registry_point = registry_point_matches(contract, effective)
    qualification_id = (
        coverage.qualification_id
        if not registry_point and coverage is not None and coverage.state == "COVERED"
        else None
    )
    configured = ConfiguredSignalProgramSeal(
        program_key=binding.strategy_key,
        program_version=contract.program_version,
        protocol_version=contract.protocol_version,
        parameter_schema_version=contract.resolved_parameter_schema_version(validated),
        golden_trace_root=contract.golden_trace_root,
        parameters=parameters,
        parameters_match_validated_settings=registry_point,
        data=SignalDataContract(
            provider=contract.provider,
            symbol=binding.symbol.upper(),
            base_timeframe_ms=contract.base_timeframe_ms,
            decision_timeframe_ms=decision_timeframe_ms,
        ),
        clock=SignalClockContract(
            use_rth=binding.use_rth,
            # Resolved for these parameters too: the history the longest
            # series they build needs, never less than the default (#2841).
            warmup_lookback_days=contract.resolved_warmup_lookback_days(validated),
        ),
        # Copied straight from the registry contract — the same objects, not
        # a re-derivation — so these can never fall out of sync with it.
        # Series, exit rule, parameter-schema version and provenance are
        # resolved for these parameters: an indicator period or a hold is a
        # parameter, and a seal at the default point stays byte-identical
        # (#2696, #2796).
        signals=contract.resolved_signals(validated),
        decision_streams=contract.decision_streams,
        bar_integrity=contract.bar_integrity,
        exit_eligibility=contract.resolved_exit_eligibility(validated),
        numerical_provenance=contract.resolved_numerical_provenance(validated),
        qualification_id=qualification_id,
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


def unsealed_program_build(
    program_key: str,
    verified_at_ms: int,
    failure: SignalProgramSealError,
) -> ProgramBuildAdmissionFact:
    """The build fact for a new instance whose program could not be sealed.

    Carries the seal failure's own cause instead of the generic "no complete
    v2 seal", which described a symptom and sent owners to re-qualify code
    that was fine (H11).
    """
    return _unproven(
        program_key,
        verified_at_ms,
        explanation=f"This bot's program could not be prepared to start: {failure}",
        **({"next_step": failure.next_step} if failure.next_step else {}),
    )


def prove_running_program_build(
    binding: BrokerBotBinding,
    *,
    verified_at_ms: int,
    manifest_path: Path = DEFAULT_QUALIFICATION_MANIFEST,
    coverage: Coverage | None = None,
) -> ProgramBuildAdmissionFact:
    """Re-hash loaded artifacts and compare one closed qualification receipt.

    Corpus coverage is the registry's validated point, or the Golden Search
    qualification the seal pins (#2696): ``coverage`` is
    :func:`resolve_admission_coverage`'s answer, read before this synchronous
    call, and it covers only when it judged exactly that pinned id ready
    under the very artifact digest this proof computes. Anything else —
    absent, stale, revoked, unreadable, or judged against other bytes — is
    UNCOVERED with the reason.
    """
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
                "but a fresh deployment is required to start another run."
            ),
        )
    configured = seal.configured_signal
    failed = next((check for check in _seal_checks(binding, seal, contract) if not check.holds), None)
    if failed is not None:
        return _unproven(binding.strategy_key, verified_at_ms, explanation=failed.explanation)
    try:
        record_imported_program_sources()
        running_digest = running_artifact_digest(contract)
        # Hashed here, with the artifact digest, rather than after the receipt
        # lookup where it is used: both read files off disk and both can raise
        # on a source tree missing a declared path, and an admission check that
        # escapes as an internal error is not failing closed. The anchor shares
        # the guard for the same reason: its import can raise on a torn tree.
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
    # #2450: the digests above read the disk, not memory. A clerk deploys by
    # ``git pull`` then restart, so between the two the disk holds bytes this
    # process never imported, and a proof computed from them would name code
    # that is not running. Refuse until the restart makes them one again.
    # #2450 review: this second source read stays inside the fail-closed
    # guard too -- a checkout that removes or replaces a declared source
    # mid-pull makes it raise, and a Deploy in that window must get
    # this refusal, never an internal error escaping the boundary.
    try:
        drifted = imported_source_drift(contract)
    except (OSError, ValueError) as exc:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation=(
                "Restart needed: the code on disk became unreadable while comparing "
                f"it against the code this process is running ({type(exc).__name__})."
            ),
            next_step="Restart the service so the running code is the code on disk, then try again.",
        )
    if drifted is not None:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation=(
                "Restart needed: the code on disk differs from the code this process "
                f"is running ({drifted} sources changed after import)."
            ),
            next_step=(
                "Restart the service so the running code is the code on disk, then try again."
            ),
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
    # the admission control this module was built around, and issue #1735's scope
    # note keeps it blocking. Only this newly-covered half is toggle-governed.
    wiring_matches = receipt.wiring_digest == running_wiring
    if not wiring_matches and settings.SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED:
        return _unproven(
            binding.strategy_key,
            verified_at_ms,
            explanation="The running strategy wiring does not match its golden-qualification receipt.",
        )
    wiring: Literal["MATCHED", "DRIFTED"] = "MATCHED" if wiring_matches else "DRIFTED"
    # `golden_trace_root` pins one decision *stream*, produced by specific
    # math at a specific cadence over specific symbols. A resolved value
    # outside `validated_settings`/`validated_symbols` means the corpus does
    # not describe this configuration -- a fact about the *evidence*, not the
    # *bytes*, so it is stamped here rather than refusing the proof; the pure
    # admission policy decides whether an uncovered point may start (ADR 0054).
    # A Golden Search qualification extends that corpus to one exact tuple
    # (#2696), only while it stays ready for these running bytes.
    corpus_coverage, coverage_note = _corpus_coverage(
        configured, coverage, running_digest, _sealed_tuple_sha256(registration, configured)
    )
    explanation, next_step = proven_build_copy(
        wiring=wiring,
        corpus_coverage=corpus_coverage,
        matched="The running Signal Program build matches its golden qualification receipt.",
        drifted=(
            "The running Signal Program math matches its golden qualification receipt, but "
            "the strategy wiring has changed since that receipt was minted."
        ),
        coverage_note=coverage_note,
    )
    qualification_refs = (
        (f"golden-qualification:{configured.qualification_id}",)
        if corpus_coverage == "COVERED" and configured.qualification_id is not None
        else ()
    )
    return ProgramBuildAdmissionFact(
        state="PROVEN",
        program_key=binding.strategy_key,
        program_version=configured.program_version,
        golden_trace_root=configured.golden_trace_root,
        running_artifact_digest=running_digest,
        qualification_receipt_hash=receipt.receipt_hash,
        verified_at_ms=verified_at_ms,
        wiring=wiring,
        corpus_coverage=corpus_coverage,
        evidence_refs=(
            f"signal-program-seal:{seal.bot_configuration_hash}",
            f"program-build-receipt:{receipt.receipt_hash}",
            f"program-build-digest:{running_digest}",
            f"program-wiring-digest:{running_wiring}",
            f"program-corpus-coverage:{corpus_coverage}",
            *qualification_refs,
        ),
        explanation=explanation,
        next_step=next_step,
    )


def _corpus_coverage(
    configured: ConfiguredSignalProgramSeal,
    coverage: Coverage | None,
    running_digest: str,
    sealed_tuple_sha256: str | None,
) -> tuple[Literal["COVERED", "UNCOVERED"], str | None]:
    """The coverage stamp for a proven build, and the qualification sentence behind it if one decided it.

    A qualification covers only when the answer judged the pinned id ready
    for these running bytes AND for this seal's own canonical tuple: an
    answer resolved for any other parameter set or stock never vouches for it.
    """
    if configured.parameters_match_validated_settings:
        return "COVERED", None
    pinned = configured.qualification_id
    if pinned is None:
        return "UNCOVERED", None
    if (
        coverage is not None
        and coverage.artifact_digest == running_digest
        and sealed_tuple_sha256 is not None
        and coverage.params_sha256 == sealed_tuple_sha256
    ):
        if coverage.state == "COVERED" and coverage.qualification_id == pinned:
            return "COVERED", QUALIFICATION_COVERED
        if coverage.state == "UNCOVERED":
            return "UNCOVERED", coverage.explanation
    if coverage is not None and coverage.artifact_digest is None and coverage.state == "UNCOVERED":
        # Its evidence could not be read: say so, never "not on record".
        return "UNCOVERED", coverage.explanation
    return "UNCOVERED", QUALIFICATION_NOT_REVERIFIED


def _canonical_tuple(
    registration: StrategyRegistration, values: Mapping[str, Any], symbol: str
) -> dict[str, Any] | None:
    """A configuration as a qualification stores it: the schema's canonical dump, stock upper-cased.

    ``None`` when the values no longer validate; the seal and the proof
    refuse such a configuration on their own.
    """
    try:
        return registration.param_schema.model_validate({**values, "symbol": symbol.upper()}).model_dump(mode="json")
    except ValidationError:
        return None


def _sealed_tuple_sha256(registration: StrategyRegistration, configured: ConfiguredSignalProgramSeal) -> str | None:
    """The canonical tuple a seal attests to, as the coverage it pins must have judged it."""
    values = {name: parameter.value for name, parameter in configured.parameters.items()}
    canonical = _canonical_tuple(registration, values, configured.data.symbol)
    return None if canonical is None else params_sha256(canonical)


class RestartNeededError(RuntimeError):
    """The code on disk is not the code this process imported, so no digest can name the running build (#2450)."""


def running_build_digests(contract: SignalProgramContract) -> tuple[str, str]:
    """The (artifact, wiring) digests of the Signal Program build this process is running.

    Read off disk only after confirming disk still holds the bytes this
    process imported (#2450); a drift raises :class:`RestartNeededError`.
    An unreadable or invalid source raises ``OSError`` / ``ValueError``.
    """
    record_imported_program_sources()
    drifted = imported_source_drift(contract)
    if drifted is not None:
        raise RestartNeededError(
            f"The code on disk differs from the code this process is running ({drifted} sources changed "
            "after import). Restart the service so the running code is the code on disk."
        )
    return running_artifact_digest(contract), running_wiring_digest(contract)


async def resolve_admission_coverage(
    binding: BrokerBotBinding,
    *,
    lookup: QualificationLookup = load_qualification_evidence,
) -> Coverage | None:
    """Golden Search coverage for the configuration a Start seals, read before its synchronous seal and proof.

    An unsealed binding asks which ready qualification, if any, covers its
    exact canonical tuple (stock upper-cased, the form qualifications
    store). A sealed one re-verifies only the qualification it pins. The
    research store is read through the same pooled connection as Golden
    Validation admission; the resolver turns any read failure into an
    UNCOVERED "cannot verify" answer rather than raising. ``None`` means
    there is nothing to resolve: not a registered Signal Program, a seal
    that pins no qualification, or parameters the seal and proof will
    refuse on their own.
    """
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    contract = registration.signal_program_contract if registration is not None else None
    if registration is None or registration.signal_program_factory is None or contract is None:
        return None
    seal = binding.sealed_program
    pinned = seal.configured_signal.qualification_id if seal is not None else None
    if seal is not None and pinned is None:
        return None
    values = (
        {name: parameter.value for name, parameter in seal.configured_signal.parameters.items()}
        if seal is not None
        else dict(binding.strategy_params or {})
    )
    symbol = seal.configured_signal.data.symbol if seal is not None else binding.symbol
    params = _canonical_tuple(registration, values, symbol)
    if params is None:
        return None
    if registry_point_matches(contract, params):
        return Coverage(state="COVERED", qualification_id=None, explanation=REGISTRY_POINT_COVERED)
    try:
        artifact_digest, _wiring_digest = running_build_digests(contract)
    except (RestartNeededError, OSError, ValueError):
        # The proof refuses this build on its own; nothing can be judged ready for it.
        logger.warning(
            "Golden Search coverage could not name the running build",
            extra={"action": "golden_coverage_build_unnamed", "strategy_key": binding.strategy_key},
            exc_info=True,
        )
        return Coverage(state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_UNJUDGEABLE)
    return await resolve_coverage(
        program_key=binding.strategy_key,
        contract=contract,
        params=params,
        running_artifact_digest=artifact_digest,
        lookup=lookup,
        qualification_id=pinned,
    )


def _resolve_artifact(relative: str) -> Path:
    """The one validated on-disk location for a service-relative source file."""
    candidate = (_SERVICE_ROOT / relative).resolve()
    if _SERVICE_ROOT not in candidate.parents or not candidate.is_file():
        raise ValueError(f"invalid Signal Program artifact path: {relative}")
    return candidate


def _digest_entries(
    paths: tuple[str, ...], digest_for: Callable[[str], str]
) -> str:
    """Hash a closed, ordered set of service-relative source files."""
    entries = [{"path": relative, "sha256": digest_for(relative)} for relative in paths]
    return semantic_payload_hash(entries)


def _digest_paths(paths: tuple[str, ...]) -> str:
    """Hash a closed, ordered set of service-relative source files."""
    return _digest_entries(
        paths, lambda relative: hashlib.sha256(_resolve_artifact(relative).read_bytes()).hexdigest()
    )


def _imported_digest(paths: tuple[str, ...]) -> str:
    """The digest of ``paths`` as recorded at import time (startup)."""
    return _digest_entries(paths, _IMPORTED_SOURCE_DIGESTS.__getitem__)


def imported_source_drift(contract: SignalProgramContract) -> Literal["artifact", "wiring"] | None:
    """Which half of a proof's sources changed on disk after this process imported them.

    ``None`` when the on-disk bytes are still the bytes in memory, which is
    the only case in which a proof computed from disk can name the code that
    is running. An unrecorded path counts as drift: a proof must not claim
    bytes it never anchored.
    """
    for label, paths in (
        ("artifact", contract.artifact_paths),
        ("wiring", contract.wiring_artifact_paths),
    ):
        try:
            imported = _imported_digest(paths)
        except KeyError:
            return label
        if imported != _digest_paths(paths):
            return label
    return None


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
    git_provenance: dict[str, Any] | None = None,
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
    if git_provenance is not None:
        payload["git_provenance"] = git_provenance
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
    # The series, exit rule and warmup lookback a seal attests to depend on its
    # own parameters (#2696, #2841), so they are compared against the contract
    # resolved for them.
    sealed_params = _sealed_parameters(binding.strategy_key, configured)
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
        # must still be present and unchanged against the currently registered
        # contract. Not a live-feed parity gate -- see
        # SignalDataContract.provider's docstring.
        _SealCheck(
            "data.provider",
            configured.data.provider == contract.provider,
            "The sealed qualification lineage no longer matches the registered contract.",
        ),
        # The sealed-semantics completeness fix (sibling to #1729): every field
        # widened onto the seal must still match the registered
        # contract, at the same cadence as the identity checks above.
        _SealCheck(
            "protocol_version",
            configured.protocol_version == contract.protocol_version,
            "The registered session protocol has moved since this instance was sealed.",
        ),
        # The parameters come first: the rows after them compare against the
        # contract resolved for the seal's own parameters.
        _SealCheck(
            "parameters",
            sealed_params is not None,
            "The sealed parameters no longer validate against the registered parameter schema.",
        ),
        _SealCheck(
            "parameter_schema_version",
            sealed_params is not None
            and configured.parameter_schema_version == contract.resolved_parameter_schema_version(sealed_params),
            "The registered parameter schema has moved since this instance was sealed.",
        ),
        _SealCheck(
            "signals",
            sealed_params is not None and configured.signals == contract.resolved_signals(sealed_params),
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
            sealed_params is not None
            and configured.exit_eligibility == contract.resolved_exit_eligibility(sealed_params),
            "The registered exit-eligibility rule has moved since this instance was sealed.",
        ),
        _SealCheck(
            "numerical_provenance",
            sealed_params is not None
            and configured.numerical_provenance == contract.resolved_numerical_provenance(sealed_params),
            "The registered numerical provenance has moved since this instance was sealed.",
        ),
        _SealCheck(
            "data.base_timeframe_ms",
            configured.data.base_timeframe_ms == contract.base_timeframe_ms,
            "The sealed source-bar cadence no longer matches the registered contract.",
        ),
        _SealCheck(
            "clock.warmup_lookback_days",
            sealed_params is not None
            and configured.clock.warmup_lookback_days == contract.resolved_warmup_lookback_days(sealed_params),
            "The sealed warmup requirement no longer matches the registered contract.",
        ),
        # `parameters_match_validated_settings` is deliberately not a row here.
        # It says whether the corpus *covers* this configuration, not whether
        # the sealed program still *is* the registered one, and since ADR 0054
        # it is reported as `corpus_coverage` on the proof rather than
        # refusing it -- see `prove_running_program_build`.
    )


def _sealed_parameters(program_key: str, configured: ConfiguredSignalProgramSeal) -> StrategyParamsBase | None:
    """The parameter model a seal's own values build, or ``None`` when they no longer validate."""
    try:
        return _STRATEGY_REGISTRY[program_key].param_schema.model_validate(
            {name: parameter.value for name, parameter in configured.parameters.items()}
        )
    except ValidationError:
        return None


def _unproven(
    program_key: str,
    verified_at_ms: int,
    *,
    explanation: str,
    next_step: str = (
        "Run golden qualification for these bytes, or deploy a newly sealed compatible instance."
    ),
) -> ProgramBuildAdmissionFact:
    return ProgramBuildAdmissionFact(
        state="UNPROVEN",
        program_key=program_key,
        verified_at_ms=verified_at_ms,
        explanation=explanation,
        next_step=next_step,
    )


__all__ = [
    "DEFAULT_QUALIFICATION_MANIFEST",
    "ProgramBuildQualificationManifest",
    "ProgramBuildQualificationReceipt",
    "RestartNeededError",
    "SignalProgramSealError",
    "build_start_program_seal",
    "imported_source_drift",
    "prove_running_program_build",
    "qualification_receipt_payload",
    "record_imported_program_sources",
    "resolve_admission_coverage",
    "running_artifact_digest",
    "running_build_digests",
    "running_wiring_digest",
    "unsealed_program_build",
]
