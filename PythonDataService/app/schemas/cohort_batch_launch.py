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


class CohortBatchLaunchCommandRequest(BaseModel):
    """Client selection for one server-authored cohort launch command.

    The member IDs are a compare-and-swap token for the displayed roll-call
    set, not authority to select stale offers, invent a window, or author an
    outcome.  The data plane refreshes and pins every other field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    member_strategy_instance_ids: tuple[str, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_members(self) -> CohortBatchLaunchCommandRequest:
        if any(not member.strip() for member in self.member_strategy_instance_ids):
            raise ValueError("member_strategy_instance_ids must not contain blank values")
        if len(set(self.member_strategy_instance_ids)) != len(self.member_strategy_instance_ids):
            raise ValueError("member_strategy_instance_ids must be unique")
        return self


class CohortBatchLaunchMemberOutcomeRequest(BaseModel):
    """One client-observed start result, retained with its safe follow-up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "blocked", "skipped"]
    reason: str = Field(min_length=1, max_length=512)
    next_safe_action: str = Field(min_length=1, max_length=512)


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
