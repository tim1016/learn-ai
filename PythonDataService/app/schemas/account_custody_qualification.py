"""Versioned, broker-free evidence for the account-custody qualification run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QualificationDrillVerdict = Literal["CLEAN", "RECONCILING", "SUSPENDED", "UNAVAILABLE"]
QualificationCertificateStatus = Literal[
    "DETERMINISTIC_PASSED_AWAITING_PAPER",
    "FAILED",
]
PaperQualificationStatus = Literal["NOT_RUN"]
INT64_MAX = 9_223_372_036_854_775_807
BACKEND_CUSTODY_QUALIFICATION_DRILL_IDS = (*range(1, 13), *range(16, 19))


def account_custody_qualification_payload_sha256(payload: Mapping[str, object]) -> str:
    """Hash the complete semantic report payload with deterministic JSON bytes."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AccountCustodyQualificationDrill(BaseModel):
    """One deterministic fault drill and its complete operator-facing receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drill_id: int = Field(ge=1, le=18)
    name: str = Field(min_length=1, max_length=160)
    initial_state: str = Field(min_length=1, max_length=640)
    injected_fault: str = Field(min_length=1, max_length=640)
    expected_invariant: str = Field(min_length=1, max_length=1_024)
    observed_receipts: tuple[str, ...] = Field(min_length=1, max_length=64)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    final_account_verdict: QualificationDrillVerdict
    passed: bool
    failure_detail: str | None = Field(default=None, max_length=1_024)

    @model_validator(mode="after")
    def validate_failure_detail(self) -> AccountCustodyQualificationDrill:
        if self.passed and self.failure_detail is not None:
            raise ValueError("a passing drill must not contain a failure detail")
        if not self.passed and self.failure_detail is None:
            raise ValueError("a failed drill requires a failure detail")
        return self


class AccountCustodyQualificationMetric(BaseModel):
    """One explicit latency distribution from the deterministic run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=0)
    p50_ms: float | None = Field(default=None, ge=0)
    p95_ms: float | None = Field(default=None, ge=0)
    p99_ms: float | None = Field(default=None, ge=0)
    source_receipt_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    sample_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_quantile_order(self) -> AccountCustodyQualificationMetric:
        percentiles = (self.p50_ms, self.p95_ms, self.p99_ms)
        if self.sample_count == 0:
            if any(value is not None for value in percentiles):
                raise ValueError("an unavailable metric must not contain percentiles")
            return self
        if any(value is None for value in percentiles):
            raise ValueError("an observed metric requires every percentile")
        assert self.p50_ms is not None and self.p95_ms is not None and self.p99_ms is not None
        if not self.p50_ms <= self.p95_ms <= self.p99_ms:
            raise ValueError("latency percentiles must be nondecreasing")
        return self


class AccountCustodyQualificationMetrics(BaseModel):
    """Capacity and phase timing evidence required by custody S15."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase_latencies: tuple[AccountCustodyQualificationMetric, ...] = Field(min_length=1)
    queue_high_water: int | None = Field(default=None, ge=0)
    queue_refusal_count: int | None = Field(default=None, ge=0)
    epoch_recovery_ms: int | None = Field(default=None, ge=0)
    max_uncertain_intent_age_ms: int | None = Field(default=None, ge=0)
    projection_gap_count: int | None = Field(default=None, ge=0)


class AccountCustodyQualificationCertificate(BaseModel):
    """Backend-only pass/fail decision; paper promotion is deliberately absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: QualificationCertificateStatus
    deterministic_drill_count: int = Field(ge=0)
    passed_drill_count: int = Field(ge=0)
    failed_drill_ids: tuple[int, ...] = ()
    paper_environment_status: PaperQualificationStatus = "NOT_RUN"

    @model_validator(mode="after")
    def validate_certificate_state(self) -> AccountCustodyQualificationCertificate:
        if self.passed_drill_count > self.deterministic_drill_count:
            raise ValueError("passed drill count cannot exceed deterministic drill count")
        if self.status == "FAILED":
            if not self.failed_drill_ids:
                raise ValueError("a failed certificate requires failed drill ids")
        elif self.failed_drill_ids:
            raise ValueError("a passing certificate must not contain failed drill ids")
        return self


class AccountCustodyQualificationReport(BaseModel):
    """Content-addressed output of a deterministic eight-bot fault campaign."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    generated_at_ms: int = Field(ge=0, le=INT64_MAX)
    account_id: str = Field(min_length=1, max_length=64)
    execution_mode: Literal["DETERMINISTIC"] = "DETERMINISTIC"
    broker_dependency: Literal["NONE"] = "NONE"
    fleet_size: Literal[8] = 8
    entry_capacity: Literal[8] = 8
    risk_reducing_capacity: Literal[0] = 0
    drills: tuple[AccountCustodyQualificationDrill, ...] = Field(min_length=1, max_length=15)
    metrics: AccountCustodyQualificationMetrics
    certificate: AccountCustodyQualificationCertificate
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_drill_coverage(self) -> AccountCustodyQualificationReport:
        drill_ids = tuple(drill.drill_id for drill in self.drills)
        if tuple(sorted(drill_ids)) != BACKEND_CUSTODY_QUALIFICATION_DRILL_IDS:
            raise ValueError("qualification report must contain each required backend drill exactly once")
        if self.certificate.deterministic_drill_count != len(self.drills):
            raise ValueError("certificate drill count must match report drills")
        if self.certificate.passed_drill_count != sum(drill.passed for drill in self.drills):
            raise ValueError("certificate passed count must match report drills")
        failed_ids = tuple(drill.drill_id for drill in self.drills if not drill.passed)
        if self.certificate.failed_drill_ids != failed_ids:
            raise ValueError("certificate failed drill ids must match report drills")
        payload = self.model_dump(mode="json", exclude={"report_sha256"})
        if self.report_sha256 != account_custody_qualification_payload_sha256(payload):
            raise ValueError("qualification report SHA-256 does not match its semantic payload")
        return self
