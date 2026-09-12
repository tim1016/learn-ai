"""Fresh validation-proof receipts for the Start and Resume admission boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Protocol

import asyncpg
from pydantic import ValidationError

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_validation import service as golden_validation_service
from app.research.persistence.db import with_connection
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.schemas.strategy_validation import (
    StrategyValidationEntry,
    StrategyValidationFlagEvent,
)
from app.services.strategy_validation_manifest import (
    StrategyValidationManifestError,
    load_strategy_validation_entries,
    strategy_audit_copy_is_current,
    strategy_registry_seeds,
    strategy_settings_file_is_current,
    strategy_validator_code_is_current,
)
from app.services.strategy_validation_policy import strategy_validation_policy


class ValidationAdmissionBinding(Protocol):
    """The immutable binding facts that select one validation proof."""

    strategy_key: str
    evidence_override: object | None


EntryLoader = Callable[[], list[StrategyValidationEntry]]
ValidationFactResolver = Callable[
    [ValidationAdmissionBinding, int],
    StrategyValidationAdmissionFact | Awaitable[StrategyValidationAdmissionFact],
]
GoldenDossierLoader = Callable[
    [str, str, dict[str, object]],
    Awaitable[golden_validation_service.GoldenDeploymentCandidates],
]
GoldenDossierByIdLoader = Callable[
    [int],
    Awaitable[golden_validation_service.GoldenValidationDossier | None],
]


async def resolve_strategy_validation_fact(
    resolver: ValidationFactResolver,
    binding: ValidationAdmissionBinding,
    observed_at_ms: int,
) -> StrategyValidationAdmissionFact:
    """Resolve either the legacy synchronous gate or the DB-backed gate."""
    result = resolver(binding, observed_at_ms)
    if isawaitable(result):
        return await result
    return result


def current_strategy_validation_fact(
    binding: ValidationAdmissionBinding,
    observed_at_ms: int,
    *,
    entries_loader: EntryLoader | None = None,
) -> StrategyValidationAdmissionFact:
    """Re-read and re-hash the proof that this run proposes to rely on.

    This adapter deliberately executes at Start/Resume, not only when the
    deploy panel was rendered.  A stale panel is therefore unable to turn a
    later-edited settings file, audit copy, validator, or validation event into
    a new duty run.
    """
    load_entries = entries_loader or _load_current_entries
    try:
        entries = load_entries()
    except (StrategyValidationManifestError, KeyError, TypeError, ValueError):
        return StrategyValidationAdmissionFact(
            state="UNREADABLE",
            strategy_key=binding.strategy_key,
            evidence_status="unknown",
            verified_at_ms=observed_at_ms,
            explanation="The strategy validation evidence could not be read or its event receipt is invalid.",
            next_step="Restore the validation manifest and evidence artifacts, then revalidate.",
        )

    entry = next((item for item in entries if item.strategy_key == binding.strategy_key), None)
    if entry is None:
        return _unverified(
            binding.strategy_key,
            observed_at_ms,
            evidence_status="unknown",
            explanation="No validation record exists for the requested strategy runtime.",
        )
    event = entry.current_flag_event
    if entry.validation_state != "validated" or event is None or event.flag != "validated":
        return _unverified(
            binding.strategy_key,
            observed_at_ms,
            evidence_status="blocked",
            explanation="The requested strategy has no active validated proof.",
        )

    evidence_status = _evidence_status(event)
    refs = _evidence_refs(event)
    # The durable evidence-only override accepts the *absence* of reference
    # artifacts (a candidate with no registered proof has nothing to be
    # current against) but never the *drift* of recorded ones — a recorded
    # artifact whose bytes changed still refuses (operator decision
    # 2026-08-24, restoring the override contract #1746 had made
    # unsatisfiable for proof-less candidates).
    override_accepts_absence = (
        evidence_status == "evidence_only" and binding.evidence_override is not None
    )
    matches = (
        _event_matches_recorded_artifacts(entry, event)
        if override_accepts_absence
        else _event_matches_current_artifacts(entry, event)
    )
    if not matches:
        return _unverified(
            binding.strategy_key,
            observed_at_ms,
            evidence_status=evidence_status,
            event=event,
            evidence_refs=refs,
            explanation="The active validation event no longer matches its current artifacts or manifest proof.",
        )
    if evidence_status == "accepted" and entry.deployable:
        return _verified(binding.strategy_key, observed_at_ms, evidence_status, event, refs)
    if evidence_status == "evidence_only" and binding.evidence_override is not None:
        return _verified(binding.strategy_key, observed_at_ms, evidence_status, event, refs)
    if evidence_status == "evidence_only":
        return _unverified(
            binding.strategy_key,
            observed_at_ms,
            evidence_status=evidence_status,
            event=event,
            evidence_refs=refs,
            explanation="This strategy has evidence-only validation and requires the durable human override.",
            next_step="Record the evidence override before deploying.",
        )
    return _unverified(
        binding.strategy_key,
        observed_at_ms,
        evidence_status=evidence_status,
        event=event,
        evidence_refs=refs,
        explanation="The active validation proof is not accepted for this deployment.",
    )


async def current_deployment_strategy_validation_fact(
    binding: ValidationAdmissionBinding,
    observed_at_ms: int,
    *,
    golden_loader: GoldenDossierLoader | None = None,
    golden_by_id_loader: GoldenDossierByIdLoader | None = None,
) -> StrategyValidationAdmissionFact:
    """Resolve the exact Golden gate for Start/Resume, with v1 compatibility.

    The first Golden case designated for a strategy moves that strategy onto
    configuration-scoped admission.  Until then, its existing v1 validation
    event remains authoritative.  A Resume whose immutable program seal names
    a Golden review is pinned to that exact record and can never fall back to
    a strategy-wide event.
    """
    legacy = current_strategy_validation_fact(binding, observed_at_ms)
    selected_reference = _sealed_golden_validation_reference(binding)
    scope = _deployment_scope(binding)
    if scope is None:
        return _golden_unreadable(
            binding.strategy_key,
            observed_at_ms,
            "The proposed strategy program or resolved parameters could not be reconstructed for Golden Validation.",
        )
    try:
        if selected_reference is None:
            load = golden_loader or _load_golden_dossiers
            candidates = await load(
                binding.strategy_key,
                str(scope["symbol"]),
                dict(scope["parameters"]),
            )
            dossiers = list(candidates.dossiers)
            strategy_has_golden_runs = candidates.strategy_has_golden_runs
        else:
            selected_id, _selected_review_id = selected_reference
            load_one = golden_by_id_loader or _load_golden_dossier
            selected = await load_one(selected_id)
            dossiers = [] if selected is None else [selected]
            strategy_has_golden_runs = True
    except (
        asyncpg.PostgresError,
        golden_validation_service.GoldenValidationError,
        KeyError,
        OSError,
        TimeoutError,
        TypeError,
        ValueError,
    ) as exc:
        return _golden_unreadable(
            binding.strategy_key,
            observed_at_ms,
            f"Golden Validation evidence could not be read ({type(exc).__name__}).",
        )
    if selected_reference is not None:
        selected_id, selected_review_id = selected_reference
        if not dossiers:
            return _golden_unverified(
                binding.strategy_key,
                observed_at_ms,
                "The immutable program seal names a Golden Validation record that is no longer readable.",
            )
        latest_review = dossiers[0].latest_review
        if latest_review is None or latest_review.id != selected_review_id:
            return _golden_unverified(
                binding.strategy_key,
                observed_at_ms,
                "The Golden Validation review selected by the immutable program seal is no longer current.",
            )
    elif not dossiers:
        if not strategy_has_golden_runs:
            return legacy
        return _golden_unverified(
            binding.strategy_key,
            observed_at_ms,
            "No Golden Validation record matches this exact program version, symbol, and resolved parameter set.",
        )

    scoped = [
        (dossier, golden_validation_service.assess_deployment_scope(dossier, scope))
        for dossier in dossiers
    ]
    exact = [(dossier, receipt) for dossier, receipt in scoped if not receipt.mismatched_fields]
    applicable = next(
        ((dossier, receipt) for dossier, receipt in exact if receipt.applicable),
        None,
    )
    if applicable is not None:
        return _golden_verified(*applicable, strategy_key=binding.strategy_key, observed_at_ms=observed_at_ms)
    if exact:
        return _golden_unverified(
            binding.strategy_key,
            observed_at_ms,
            exact[0][1].explanation,
        )
    return _golden_unverified(
        binding.strategy_key,
        observed_at_ms,
        "No Golden Validation record matches this exact program version, symbol, and resolved parameter set.",
    )


async def _load_golden_dossiers(
    strategy_key: str,
    symbol: str,
    parameters: dict[str, object],
) -> golden_validation_service.GoldenDeploymentCandidates:
    return await with_connection(
        golden_validation_service.find_deployment_scope_dossiers,
        strategy_name=strategy_key,
        symbol=symbol,
        parameters=parameters,
    )


async def _load_golden_dossier(
    golden_validation_id: int,
) -> golden_validation_service.GoldenValidationDossier | None:
    return await with_connection(
        golden_validation_service.get_dossier,
        golden_validation_id,
    )


def _deployment_scope(binding: ValidationAdmissionBinding) -> dict[str, object] | None:
    seal = getattr(binding, "sealed_program", None)
    if seal is not None:
        configured = seal.configured_signal
        return {
            "strategy_name": configured.program_key,
            "program_version": configured.program_version,
            "symbol": configured.data.symbol.upper(),
            "parameters": {
                name: parameter.value
                for name, parameter in configured.parameters.items()
            },
        }
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    contract = registration.signal_program_contract if registration is not None else None
    symbol = getattr(binding, "symbol", None)
    if registration is None or contract is None or not isinstance(symbol, str):
        return None
    try:
        resolved = registration.param_schema.model_validate(
            {**(getattr(binding, "strategy_params", None) or {}), "symbol": symbol.upper()}
        )
    except ValidationError:
        return None
    return {
        "strategy_name": binding.strategy_key,
        "program_version": contract.program_version,
        "symbol": symbol.upper(),
        "parameters": resolved.model_dump(mode="json"),
    }


def _sealed_golden_validation_reference(
    binding: ValidationAdmissionBinding,
) -> tuple[int, int] | None:
    seal = getattr(binding, "sealed_program", None)
    event_id = getattr(seal, "validation_event_id", "") if seal is not None else ""
    parts = event_id.split(":")
    if len(parts) != 4 or parts[0] != "golden-validation" or parts[2] != "review":
        return None
    try:
        return int(parts[1]), int(parts[3])
    except ValueError:
        return None


def _golden_verified(
    dossier: golden_validation_service.GoldenValidationDossier,
    receipt: golden_validation_service.ApplicabilityReceipt,
    *,
    strategy_key: str,
    observed_at_ms: int,
) -> StrategyValidationAdmissionFact:
    review = dossier.latest_review
    assert review is not None and receipt.classification is not None
    golden_id = dossier.golden_run.id
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key=strategy_key,
        evidence_status="accepted",
        event_id=f"golden-validation:{golden_id}:review:{review.id}",
        evidence_snapshot_sha256=dossier.evidence.revision,
        verified_at_ms=observed_at_ms,
        evidence_refs=(
            f"golden-validation:{golden_id}",
            f"golden-validation:case:{dossier.golden_run.case_sha256}",
            f"golden-validation:review:{review.id}",
            f"golden-validation:evidence:{dossier.evidence.revision}",
            f"golden-validation:classification:{receipt.classification}",
        ),
        explanation=(
            f"Golden Validation {golden_id} ({receipt.classification}) matches the exact "
            "program version, symbol, and resolved parameters for this admission."
        ),
    )


def _golden_unverified(
    strategy_key: str,
    observed_at_ms: int,
    explanation: str,
) -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="UNVERIFIED",
        strategy_key=strategy_key,
        evidence_status="blocked",
        verified_at_ms=observed_at_ms,
        explanation=explanation,
        next_step="Review or designate a Golden Validation for this exact configuration before deployment.",
    )


def _golden_unreadable(
    strategy_key: str,
    observed_at_ms: int,
    explanation: str,
) -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="UNREADABLE",
        strategy_key=strategy_key,
        evidence_status="unknown",
        verified_at_ms=observed_at_ms,
        explanation=explanation,
        next_step="Restore the Golden Validation evidence store, then retry admission.",
    )


def _load_current_entries() -> list[StrategyValidationEntry]:
    return load_strategy_validation_entries(strategy_registry_seeds())


def _event_matches_current_artifacts(
    entry: StrategyValidationEntry,
    event: StrategyValidationFlagEvent,
) -> bool:
    """Require both immutable proof identity and freshly hashed artifact bytes."""
    policy = strategy_validation_policy(entry.strategy_category)
    return policy.snapshot_matches_entry(entry, event.evidence_snapshot) and policy.artifacts_are_current(
        entry,
        settings_check=strategy_settings_file_is_current,
        validator_check=strategy_validator_code_is_current,
        audit_check=strategy_audit_copy_is_current,
    )


def _event_matches_recorded_artifacts(
    entry: StrategyValidationEntry,
    event: StrategyValidationFlagEvent,
) -> bool:
    """Like ``_event_matches_current_artifacts`` but an unrecorded artifact is vacuously current."""
    policy = strategy_validation_policy(entry.strategy_category)
    return policy.snapshot_matches_entry(entry, event.evidence_snapshot) and policy.artifacts_are_current(
        entry,
        settings_check=_recorded_or(strategy_settings_file_is_current, "settings_file_ref"),
        validator_check=_recorded_or(strategy_validator_code_is_current, "validator_code_ref"),
        audit_check=_recorded_or(strategy_audit_copy_is_current, "audit_copy_ref"),
    )


def _recorded_or(
    check: Callable[[StrategyValidationEntry], bool],
    ref_field: str,
) -> Callable[[StrategyValidationEntry], bool]:
    def _check(entry: StrategyValidationEntry) -> bool:
        if getattr(entry, ref_field) is None:
            return True
        return check(entry)

    return _check


def _evidence_status(event: StrategyValidationFlagEvent) -> str:
    if event.behavioral_equivalence.verdict == "accepted_for_deploy":
        return "accepted"
    if event.behavioral_equivalence.verdict == "evidence_only":
        return "evidence_only"
    return "blocked"


def _evidence_refs(event: StrategyValidationFlagEvent) -> tuple[str, ...]:
    snapshot = event.evidence_snapshot
    refs = [
        f"strategy-validation:event:{event.event_id}",
        f"strategy-validation:snapshot:{event.evidence_snapshot_sha256}",
    ]
    for kind, ref, sha256 in (
        ("validator", snapshot.validator_code_ref, snapshot.validator_code_sha256),
        ("settings", snapshot.settings_file_ref, snapshot.settings_file_sha256),
        ("audit", snapshot.audit_copy_ref, snapshot.audit_copy_sha256),
    ):
        if ref is not None and sha256 is not None:
            refs.append(f"strategy-validation:{kind}:{ref}:{sha256}")
    return tuple(refs)


def _verified(
    strategy_key: str,
    observed_at_ms: int,
    evidence_status: str,
    event: StrategyValidationFlagEvent,
    evidence_refs: tuple[str, ...],
) -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key=strategy_key,
        evidence_status=evidence_status,
        event_id=event.event_id,
        evidence_snapshot_sha256=event.evidence_snapshot_sha256,
        verified_at_ms=observed_at_ms,
        evidence_refs=evidence_refs,
        explanation="The active validation proof and all referenced artifacts were re-hashed for this admission.",
    )


def _unverified(
    strategy_key: str,
    observed_at_ms: int,
    *,
    evidence_status: str,
    explanation: str,
    event: StrategyValidationFlagEvent | None = None,
    evidence_refs: tuple[str, ...] = (),
    next_step: str | None = "Restore current validation evidence and revalidate before deployment.",
) -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="UNVERIFIED",
        strategy_key=strategy_key,
        evidence_status=evidence_status,
        event_id=None if event is None else event.event_id,
        evidence_snapshot_sha256=None if event is None else event.evidence_snapshot_sha256,
        verified_at_ms=observed_at_ms,
        evidence_refs=evidence_refs,
        explanation=explanation,
        next_step=next_step,
    )
