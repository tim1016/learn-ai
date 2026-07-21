"""HTTP contracts for deliberate live-bot cohort launches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from app.engine.live.account_artifacts import (
        CohortBatchLaunchOutcomesReceipt,
        CohortBatchLaunchReceipt,
    )


def validate_cohort_batch_launch_window_and_members(
    window_start_ms: int,
    window_end_ms: int,
    member_strategy_instance_ids: tuple[str, ...],
) -> None:
    """Validate the invariants shared by cohort request and receipt models."""
    if window_end_ms < window_start_ms:
        raise ValueError("window_end_ms must not precede window_start_ms")
    if any(not member.strip() for member in member_strategy_instance_ids):
        raise ValueError("member_strategy_instance_ids must not contain blank values")
    if len(set(member_strategy_instance_ids)) != len(member_strategy_instance_ids):
        raise ValueError("member_strategy_instance_ids must be unique")


COHORT_EVIDENCE_CADENCE_MS = 5_000
"""Server evidence-sampling cadence, shared by every staggered cohort profile."""


@dataclass(frozen=True)
class CohortStaggerProfile:
    """Canonical launch parameters for one server-owned staggered cohort profile."""

    member_count: int
    stagger_ms: int
    overlap_ms: int


# Single source of truth for every staggered-cohort profile's timing. The receipt
# validator (account_artifacts), the server launcher (cohort_launch), and this
# request contract all derive member count / schedule spacing / overlap width from
# this table, so the three can never drift apart.
COHORT_STAGGER_PROFILES: dict[str, CohortStaggerProfile] = {
    "paper_three_bot_stagger_v2": CohortStaggerProfile(
        member_count=3, stagger_ms=15 * 60 * 1_000, overlap_ms=60 * 60 * 1_000
    ),
    "paper_five_bot_stagger_v2": CohortStaggerProfile(
        member_count=5, stagger_ms=5 * 60 * 1_000, overlap_ms=45 * 60 * 1_000
    ),
}

CohortStaggerProfileName = Literal["paper_three_bot_stagger_v2", "paper_five_bot_stagger_v2"]
CohortBatchLaunchMemberOutcomeReason = Literal[
    "COHORT_ACCOUNT_FROZEN",
    "COHORT_CRASH_RECOVERY_BLOCKED",
    "COHORT_DAEMON_NOT_STARTABLE",
    "COHORT_DAEMON_UNAVAILABLE",
    "COHORT_MEMBER_DELETED",
    "COHORT_MEMBER_POISONED",
    "COHORT_MEMBER_RETIRED",
    "COHORT_PRIOR_MEMBER_BLOCKED",
    # Historical receipt events remain readable after slot dispatch retires
    # these outcomes. New V2 scheduler code must not emit them.
    "COHORT_POSTURE_MISMATCH",
    "COHORT_SLOT_PREFLIGHT_NOT_READY",
    "COHORT_START_ACCEPTED",
    "COHORT_START_FAILED",
    "COHORT_START_NOT_ACCEPTED",
    "COHORT_START_REJECTED",
    "COHORT_START_SETTINGS_UNREADABLE",
]


class CohortBatchLaunchCommandRequest(BaseModel):
    """Client selection for one server-authored cohort launch command.

    The member IDs are a compare-and-swap token for the displayed roll-call
    set, not authority to select stale offers, invent a window, or author an
    outcome.  The data plane refreshes and pins every other field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    member_strategy_instance_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    launch_profile: CohortStaggerProfileName | None = None

    @model_validator(mode="after")
    def validate_members(self) -> CohortBatchLaunchCommandRequest:
        if any(not member.strip() for member in self.member_strategy_instance_ids):
            raise ValueError("member_strategy_instance_ids must not contain blank values")
        if len(set(self.member_strategy_instance_ids)) != len(self.member_strategy_instance_ids):
            raise ValueError("member_strategy_instance_ids must be unique")
        profile = COHORT_STAGGER_PROFILES.get(self.launch_profile) if self.launch_profile else None
        if profile is not None and len(self.member_strategy_instance_ids) != profile.member_count:
            raise ValueError(
                f"{self.launch_profile} requires exactly {profile.member_count} selected bots"
            )
        return self


class CohortBatchLaunchMemberOutcomeRequest(BaseModel):
    """One client-observed start result, retained with its safe follow-up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "blocked", "skipped"]
    reason: CohortBatchLaunchMemberOutcomeReason
    next_safe_action: str = Field(min_length=1, max_length=512)


class CohortEvidenceSummaryResponse(BaseModel):
    """Server-authored cohort-window evidence with its calculation provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_count: int = Field(ge=0)
    cadence_ms: int = Field(gt=0)
    healthy_overlap_ms: int = Field(ge=0)
    verdict: Literal["healthy", "failed", "unknown"]
    reason: str | None = None
    source: Literal["account_event.cohort_evidence_sample"]
    members: list[CohortEvidenceMemberResponse] = Field(default_factory=list)


class CohortEvidenceMemberResponse(BaseModel):
    """Latest server observation for one receipt-pinned cohort member."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    verdict: Literal["healthy", "failed", "unknown"]
    reason: str | None = None
    orders_used: int | None = Field(default=None, ge=0)
    orders_cap: int | None = Field(default=None, gt=0)


def unknown_cohort_evidence_summary() -> CohortEvidenceSummaryResponse:
    """Return the fail-closed state before the server sampler has proof."""

    return CohortEvidenceSummaryResponse(
        sample_count=0,
        cadence_ms=5_000,
        healthy_overlap_ms=0,
        verdict="unknown",
        reason="COHORT_EVIDENCE_MISSING",
        source="account_event.cohort_evidence_sample",
        members=[],
    )


class CohortBatchLaunchStatusResponse(BaseModel):
    """Durable cohort authorization plus the latest exact member outcomes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    launch_profile: CohortStaggerProfileName | None = None
    account_id: str = Field(min_length=1, max_length=64)
    cohort_id: str = Field(min_length=1, max_length=128)
    member_strategy_instance_ids: list[str] = Field(min_length=1, max_length=128)
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)
    authorized_by: str = Field(min_length=1, max_length=128)
    authorized_recorded_at_ms: int = Field(ge=0)
    outcomes_state: Literal["pending", "recorded", "unreadable"]
    outcomes: list[CohortBatchLaunchMemberOutcomeRequest] = Field(default_factory=list)
    outcomes_recorded_at_ms: int | None = Field(default=None, ge=0)
    outcomes_error: str | None = None
    evidence: CohortEvidenceSummaryResponse = Field(default_factory=unknown_cohort_evidence_summary)
    member_scheduled_start_at_ms: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_receipts(
        cls,
        receipt: CohortBatchLaunchReceipt,
        outcomes_receipt: CohortBatchLaunchOutcomesReceipt | None,
        *,
        outcomes_error: str | None = None,
        evidence: CohortEvidenceSummaryResponse | None = None,
    ) -> CohortBatchLaunchStatusResponse:
        return cls(
            account_id=receipt.account_id,
            schema_version=receipt.schema_version,
            launch_profile=receipt.launch_profile,
            cohort_id=receipt.cohort_id,
            member_strategy_instance_ids=list(receipt.member_strategy_instance_ids),
            window_start_ms=receipt.window_start_ms,
            window_end_ms=receipt.window_end_ms,
            authorized_by=receipt.authorized_by,
            authorized_recorded_at_ms=receipt.recorded_at_ms,
            outcomes_state=(
                "unreadable"
                if outcomes_error is not None
                else "recorded"
                if outcomes_receipt is not None
                else "pending"
            ),
            outcomes=(
                [
                    CohortBatchLaunchMemberOutcomeRequest.model_validate(outcome.model_dump())
                    for outcome in outcomes_receipt.outcomes
                ]
                if outcomes_receipt is not None
                else []
            ),
            outcomes_recorded_at_ms=(
                outcomes_receipt.recorded_at_ms if outcomes_receipt is not None else None
            ),
            outcomes_error=outcomes_error,
            evidence=evidence if evidence is not None else unknown_cohort_evidence_summary(),
            member_scheduled_start_at_ms={
                slot.strategy_instance_id: slot.scheduled_start_at_ms
                for slot in receipt.member_schedule
            },
        )
