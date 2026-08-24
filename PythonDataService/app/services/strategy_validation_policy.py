"""Category-owned evidence rules for strategy validation proofs.

The category determines one evidence shape at acceptance and every later
admission boundary.  Consumers must not independently decide which fields or
artifact hashes apply; doing so lets a proof appear current in one surface and
stale in another.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.strategy_validation import (
    StrategyCategory,
    StrategyEvidenceSnapshot,
    StrategyValidationEntry,
)

ArtifactCurrentCheck = Callable[[StrategyValidationEntry], bool]

_EXTERNAL_REFERENCE_FIELDS = (
    "validator_code_ref",
    "validator_code_sha256",
    "qc_cloud_backtest_id",
    "audit_copy_ref",
    "audit_copy_sha256",
)
_COMMON_REQUIRED_FIELDS = (
    "settings_file_ref",
    "settings_file_sha256",
    "reconciliation_ref",
    "validation_case_symbol",
    "reconciliation_status",
    "diagnostics",
)


@dataclass(frozen=True)
class StrategyValidationPolicy:
    """One category's complete accepted-proof contract."""

    category: StrategyCategory
    requires_external_reference: bool
    omitted_snapshot_fields: tuple[str, ...]
    required_snapshot_fields: tuple[str, ...]

    def normalize_snapshot(
        self,
        snapshot: StrategyEvidenceSnapshot,
    ) -> StrategyEvidenceSnapshot:
        return snapshot.model_copy(update={field_name: None for field_name in self.omitted_snapshot_fields})

    def snapshot_for_entry(
        self,
        entry: StrategyValidationEntry,
    ) -> StrategyEvidenceSnapshot:
        return self.normalize_snapshot(
            StrategyEvidenceSnapshot(
                validator_code_ref=entry.validator_code_ref,
                validator_code_sha256=entry.validator_code_sha256,
                settings_file_ref=entry.settings_file_ref,
                settings_file_sha256=entry.settings_file_sha256,
                qc_cloud_backtest_id=entry.qc_cloud_backtest_id,
                audit_copy_ref=entry.audit_copy_ref,
                audit_copy_sha256=entry.audit_copy_sha256,
                reconciliation_ref=entry.reconciliation_ref,
                validation_case_symbol=entry.validation_case_symbol,
                reconciliation_status=entry.reconciliation_status,
                diagnostics=entry.diagnostics,
            )
        )

    def snapshot_matches_entry(
        self,
        entry: StrategyValidationEntry,
        snapshot: StrategyEvidenceSnapshot,
    ) -> bool:
        return snapshot == self.snapshot_for_entry(entry)

    def missing_required_fields(
        self,
        snapshot: StrategyEvidenceSnapshot,
    ) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in self.required_snapshot_fields
            if not _has_evidence_value(getattr(snapshot, field_name))
        )

    def artifacts_are_current(
        self,
        entry: StrategyValidationEntry,
        *,
        settings_check: ArtifactCurrentCheck,
        validator_check: ArtifactCurrentCheck,
        audit_check: ArtifactCurrentCheck,
    ) -> bool:
        return (
            self.first_stale_artifact(
                entry,
                settings_check=settings_check,
                validator_check=validator_check,
                audit_check=audit_check,
            )
            is None
        )

    def first_stale_artifact(
        self,
        entry: StrategyValidationEntry,
        *,
        settings_check: ArtifactCurrentCheck,
        validator_check: ArtifactCurrentCheck,
        audit_check: ArtifactCurrentCheck,
    ) -> tuple[str, str | None] | None:
        if not settings_check(entry):
            return ("settings/deploy binding file", entry.settings_file_ref)
        if not self.requires_external_reference:
            return None
        if not validator_check(entry):
            return ("validator code", entry.validator_code_ref)
        if not audit_check(entry):
            return ("audit copy", entry.audit_copy_ref)
        return None


_PRODUCTION_CANDIDATE_POLICY = StrategyValidationPolicy(
    category="production_candidate",
    requires_external_reference=True,
    omitted_snapshot_fields=(),
    required_snapshot_fields=(*_EXTERNAL_REFERENCE_FIELDS, *_COMMON_REQUIRED_FIELDS),
)
_OPERATIONAL_HARNESS_POLICY = StrategyValidationPolicy(
    category="operational_validation_harness",
    requires_external_reference=False,
    omitted_snapshot_fields=_EXTERNAL_REFERENCE_FIELDS,
    required_snapshot_fields=_COMMON_REQUIRED_FIELDS,
)
_POLICIES: dict[StrategyCategory, StrategyValidationPolicy] = {
    _PRODUCTION_CANDIDATE_POLICY.category: _PRODUCTION_CANDIDATE_POLICY,
    _OPERATIONAL_HARNESS_POLICY.category: _OPERATIONAL_HARNESS_POLICY,
}


def strategy_validation_policy(
    category: StrategyCategory,
) -> StrategyValidationPolicy:
    return _POLICIES[category]


def _has_evidence_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
