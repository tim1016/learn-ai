"""Immutable, server-authored evidence certificate for a validation cohort."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cohort_batch_launch import CohortEvidenceMemberResponse


class CohortCertificateSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_at_ms: int = Field(ge=0)
    observed_at_ms: int | None = Field(default=None, ge=0)
    account_truth: Literal["healthy", "failed", "unknown"]
    fleet: Literal["healthy", "failed", "unknown"]
    members: list[CohortEvidenceMemberResponse]
    broker_net_positions: dict[str, int] | None = None
    broker_residual: dict[str, int] | None = None


class CohortCertificateRoundTrip(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_order_namespace: str
    order_refs: list[str]
    order_ids: list[int]
    perm_ids: list[int]
    exec_ids: list[str]
    closed: bool


class CohortValidationCertificate(BaseModel):
    """Stable JSON artifact; generation time is intentionally not part of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    account_id: str
    cohort_id: str
    member_strategy_instance_ids: list[str]
    member_run_ids: dict[str, str]
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)
    healthy_overlap_ms: int = Field(ge=0)
    evidence_verdict: Literal["healthy", "failed", "unknown"]
    evidence_reason: str | None = None
    samples: list[CohortCertificateSample]
    round_trips: list[CohortCertificateRoundTrip]
    incidents: list[str]
    final_broker_net_positions: dict[str, int] | None = None
    final_broker_residual: dict[str, int] | None = None
    final_journal_exposure: dict[str, dict[str, float]]
    verdict: Literal["passed", "failed", "incomplete"]
    reasons: list[str]
