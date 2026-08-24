"""Fresh validation-proof receipts for the Start and Resume admission boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

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
            next_step="Record the paper-mode evidence override before deploying.",
        )
    return _unverified(
        binding.strategy_key,
        observed_at_ms,
        evidence_status=evidence_status,
        event=event,
        evidence_refs=refs,
        explanation="The active validation proof is not accepted for this deployment.",
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
