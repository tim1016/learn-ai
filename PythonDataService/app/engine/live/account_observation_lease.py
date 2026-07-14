"""Durable, fail-closed observation proof over Account Truth sweeps.

This artifact deliberately persists Account Truth's existing verdict rather
than deriving broker cleanliness a second time. Recovery/flatness remains the
separate concern of :mod:`app.services.account_reconciliation`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.account_artifacts import (
    account_artifacts_root,
    read_account_owner_generation,
)
from app.engine.live.account_identity import normalize_account_id
from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact
from app.schemas.live_runs import GateResult
from app.services.account_truth_snapshot import DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS

ACCOUNT_OBSERVATION_LEASE_FILENAME = "account_observation_lease.json"
ACCOUNT_OBSERVATION_LEASE_GATE_ID = "account.observation_lease"
ACCOUNT_OBSERVATION_LEASE_GATE_SOURCE = "account_observation_lease"


class AccountObservationLease(BaseModel):
    """A fresh Account Truth observation for one canonical broker account."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str = Field(min_length=1, max_length=64)
    status: Literal["VERIFIED", "REVOKED"]
    observed_at_ms: int = Field(ge=0)
    renewed_at_ms: int = Field(ge=0)
    valid_until_ms: int = Field(ge=0)
    account_owner_generation: int | None = Field(default=None, ge=0)
    truth_watermark: str = Field(min_length=1, max_length=128)
    revoked_reason_code: str | None = None
    revoked_detail: str | None = None


class AccountObservationLeaseAssessment(BaseModel):
    """Read-side assessment of a durable observation proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["VERIFIED", "REVOKED", "EXPIRED", "ABSENT"]
    lease: AccountObservationLease | None = None
    reason_code: str
    reason: str


class AccountObservationLeaseRepo:
    """Read and atomically write one observation lease per account."""

    def __init__(self, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    def path_for(self, account_id: str) -> Path:
        return (
            account_artifacts_root(self._artifacts_root, normalize_account_id(account_id))
            / ACCOUNT_OBSERVATION_LEASE_FILENAME
        )

    def read(self, account_id: str) -> AccountObservationLease | None:
        return read_pydantic_artifact(self.path_for(account_id), AccountObservationLease)

    def renew(
        self,
        *,
        account_id: str,
        observed_at_ms: int,
        now_ms: int,
        account_owner_generation: int | None,
    ) -> AccountObservationLease:
        """Persist a verified observation using the existing truth TTL."""

        canonical_account_id = normalize_account_id(account_id)
        lease = AccountObservationLease(
            account_id=canonical_account_id,
            status="VERIFIED",
            observed_at_ms=observed_at_ms,
            renewed_at_ms=now_ms,
            valid_until_ms=observed_at_ms + DEFAULT_ACCOUNT_TRUTH_READINESS_TTL_MS,
            account_owner_generation=account_owner_generation,
            truth_watermark=f"account_truth:{observed_at_ms}",
        )
        atomic_write_pydantic_artifact(self.path_for(canonical_account_id), lease)
        return lease

    def revoke(
        self,
        *,
        account_id: str,
        reason_code: str,
        detail: str,
        now_ms: int,
    ) -> AccountObservationLease:
        """Persist an immediate revocation; a later clean sweep may renew it."""

        canonical_account_id = normalize_account_id(account_id)
        existing = self.read(canonical_account_id)
        lease = AccountObservationLease(
            account_id=canonical_account_id,
            status="REVOKED",
            observed_at_ms=existing.observed_at_ms if existing is not None else now_ms,
            renewed_at_ms=now_ms,
            valid_until_ms=existing.valid_until_ms if existing is not None else now_ms,
            account_owner_generation=(
                existing.account_owner_generation if existing is not None else None
            ),
            truth_watermark=(
                existing.truth_watermark if existing is not None else f"account_truth:{now_ms}"
            ),
            revoked_reason_code=reason_code,
            revoked_detail=detail,
        )
        atomic_write_pydantic_artifact(self.path_for(canonical_account_id), lease)
        return lease


def assess_account_observation_lease(
    artifacts_root: Path,
    account_id: str,
    *,
    now_ms: int,
) -> AccountObservationLeaseAssessment:
    """Fail closed for absent, malformed, stale, or generation-mismatched proof."""

    canonical_account_id = normalize_account_id(account_id)
    lease = AccountObservationLeaseRepo(artifacts_root).read(canonical_account_id)
    if lease is None:
        return _assessment(
            state="ABSENT",
            reason_code="ACCOUNT_OBSERVATION_LEASE_ABSENT",
            reason="Account verification is not available yet.",
        )
    if lease.account_id != canonical_account_id:
        return _assessment(
            state="REVOKED",
            lease=lease,
            reason_code="ACCOUNT_OBSERVATION_LEASE_ACCOUNT_MISMATCH",
            reason="Account verification belongs to a different broker account.",
        )
    if lease.status == "REVOKED":
        return _assessment(
            state="REVOKED",
            lease=lease,
            reason_code=lease.revoked_reason_code or "ACCOUNT_OBSERVATION_LEASE_REVOKED",
            reason=lease.revoked_detail or "Account verification was revoked.",
        )
    if now_ms >= lease.valid_until_ms:
        return _assessment(
            state="EXPIRED",
            lease=lease,
            reason_code="ACCOUNT_OBSERVATION_LEASE_EXPIRED",
            reason="Account verification is overdue.",
        )

    owner = _read_owner_generation(artifacts_root, canonical_account_id)
    if (owner is not None and (
        owner.phase != "accepting"
        or owner.generation != lease.account_owner_generation
    )) or (owner is None and lease.account_owner_generation is not None):
        return _assessment(
            state="REVOKED",
            lease=lease,
            reason_code="ACCOUNT_OWNER_GENERATION_CHANGED",
            reason="Account ownership changed after this verification.",
        )
    return _assessment(
        state="VERIFIED",
        lease=lease,
        reason_code="ACCOUNT_OBSERVATION_LEASE_VERIFIED",
        reason="Account verified.",
    )


def account_observation_lease_gate_result(
    assessment: AccountObservationLeaseAssessment,
) -> GateResult:
    """Project lease assessment into the canonical account gate contract."""

    return GateResult(
        gate_id=ACCOUNT_OBSERVATION_LEASE_GATE_ID,
        status="pass" if assessment.state == "VERIFIED" else "block",
        source=ACCOUNT_OBSERVATION_LEASE_GATE_SOURCE,
        operator_reason=assessment.reason_code,
        operator_next_step=(None if assessment.state == "VERIFIED" else "RECONCILE_NOW"),
        evidence_at_ms=(assessment.lease.observed_at_ms if assessment.lease is not None else 0),
    )


def _read_owner_generation(artifacts_root: Path, account_id: str):
    try:
        return read_account_owner_generation(artifacts_root, account_id)
    except (OSError, ValueError):
        return None


def _assessment(
    *,
    state: Literal["VERIFIED", "REVOKED", "EXPIRED", "ABSENT"],
    reason_code: str,
    reason: str,
    lease: AccountObservationLease | None = None,
) -> AccountObservationLeaseAssessment:
    return AccountObservationLeaseAssessment(
        state=state,
        lease=lease,
        reason_code=reason_code,
        reason=reason,
    )


__all__ = [
    "ACCOUNT_OBSERVATION_LEASE_FILENAME",
    "AccountObservationLease",
    "AccountObservationLeaseAssessment",
    "AccountObservationLeaseRepo",
    "account_observation_lease_gate_result",
    "assess_account_observation_lease",
]
