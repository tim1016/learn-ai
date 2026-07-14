"""HTTP contracts for deliberate live-bot cohort launches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from app.engine.live.account_artifacts import CohortBatchLaunchReceipt


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
