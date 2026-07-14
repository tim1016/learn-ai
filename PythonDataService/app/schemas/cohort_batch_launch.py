"""HTTP contracts for deliberate live-bot cohort launches."""

from __future__ import annotations

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


class CohortBatchLaunchCreateRequest(BaseModel):
    """Operator authorization details for a deliberate cohort launch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cohort_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    member_strategy_instance_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)
    authorized_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_window_and_members(self) -> CohortBatchLaunchCreateRequest:
        validate_cohort_batch_launch_window_and_members(
            self.window_start_ms,
            self.window_end_ms,
            self.member_strategy_instance_ids,
        )
        return self


class CohortBatchLaunchCreateResponse(BaseModel):
    """Durable receipt for an authorized cohort launch window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    cohort_id: str = Field(min_length=1, max_length=128)
    member_strategy_instance_ids: list[str] = Field(min_length=1, max_length=128)
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)
    authorized_by: str = Field(min_length=1, max_length=128)
    recorded_at_ms: int = Field(ge=0)

    @classmethod
    def from_receipt(cls, receipt: CohortBatchLaunchReceipt) -> CohortBatchLaunchCreateResponse:
        return cls(
            schema_version=receipt.schema_version,
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            member_strategy_instance_ids=list(receipt.member_strategy_instance_ids),
            window_start_ms=receipt.window_start_ms,
            window_end_ms=receipt.window_end_ms,
            authorized_by=receipt.authorized_by,
            recorded_at_ms=receipt.recorded_at_ms,
        )


class CohortBatchLaunchMemberOutcomeRequest(BaseModel):
    """One client-observed start result, retained with its safe follow-up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "blocked", "skipped"]
    reason: str = Field(min_length=1, max_length=512)
    next_safe_action: str = Field(min_length=1, max_length=512)


class CohortBatchLaunchOutcomesRequest(BaseModel):
    """Exact outcomes for every attempted member in one cohort receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: tuple[CohortBatchLaunchMemberOutcomeRequest, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_unique_members(self) -> CohortBatchLaunchOutcomesRequest:
        member_ids = tuple(outcome.strategy_instance_id for outcome in self.outcomes)
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("cohort outcome strategy_instance_id values must be unique")
        return self


class CohortBatchLaunchOutcomesResponse(BaseModel):
    """Immutable receipt confirming cohort outcomes reached account events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str = Field(min_length=1, max_length=64)
    cohort_id: str = Field(min_length=1, max_length=128)
    outcomes: list[CohortBatchLaunchMemberOutcomeRequest] = Field(min_length=1, max_length=128)
    recorded_at_ms: int = Field(ge=0)

    @classmethod
    def from_receipt(cls, receipt: CohortBatchLaunchOutcomesReceipt) -> CohortBatchLaunchOutcomesResponse:
        return cls(
            schema_version=receipt.schema_version,
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            outcomes=[
                CohortBatchLaunchMemberOutcomeRequest.model_validate(outcome.model_dump())
                for outcome in receipt.outcomes
            ],
            recorded_at_ms=receipt.recorded_at_ms,
        )


class CohortBatchLaunchStatusResponse(BaseModel):
    """Durable cohort authorization plus the latest exact member outcomes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
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

    @classmethod
    def from_receipts(
        cls,
        receipt: CohortBatchLaunchReceipt,
        outcomes_receipt: CohortBatchLaunchOutcomesReceipt | None,
        *,
        outcomes_error: str | None = None,
    ) -> CohortBatchLaunchStatusResponse:
        return cls(
            account_id=receipt.account_id,
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
        )
