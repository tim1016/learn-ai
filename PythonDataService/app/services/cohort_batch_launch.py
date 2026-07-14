"""Durable authorization service for deliberate cohort launches."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    read_account_events,
    record_cohort_batch_launch_outcomes,
    record_cohort_batch_launch_receipt,
)
from app.schemas.cohort_batch_launch import (
    CohortBatchLaunchCreateRequest,
    CohortBatchLaunchOutcomesRequest,
)


class CohortBatchLaunchService:
    """Writes operator-authorized cohort launch receipts to account events."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    async def create_receipt(
        self,
        *,
        account_id: str,
        request: CohortBatchLaunchCreateRequest,
        recorded_at_ms: int,
    ) -> CohortBatchLaunchReceipt:
        receipt = CohortBatchLaunchReceipt(
            account_id=account_id,
            cohort_id=request.cohort_id,
            member_strategy_instance_ids=request.member_strategy_instance_ids,
            window_start_ms=request.window_start_ms,
            window_end_ms=request.window_end_ms,
            authorized_by=request.authorized_by,
            recorded_at_ms=recorded_at_ms,
        )
        await asyncio.to_thread(
            record_cohort_batch_launch_receipt,
            self._artifacts_root,
            receipt,
        )
        return receipt

    async def record_outcomes(
        self,
        *,
        account_id: str,
        cohort_id: str,
        request: CohortBatchLaunchOutcomesRequest,
        recorded_at_ms: int,
    ) -> CohortBatchLaunchOutcomesReceipt:
        """Record results only for members previously authorized in this account."""

        events = await asyncio.to_thread(read_account_events, self._artifacts_root, account_id)
        authorized_members = next(
            (
                set(event["member_strategy_instance_ids"])
                for event in reversed(events)
                if event.get("event_type") == "cohort_batch_launch_authorized"
                and event.get("cohort_id") == cohort_id
                and isinstance(event.get("member_strategy_instance_ids"), list)
            ),
            None,
        )
        if authorized_members is None:
            raise ValueError(f"cohort receipt not found: {cohort_id}")
        outcome_members = {outcome.strategy_instance_id for outcome in request.outcomes}
        if outcome_members != authorized_members:
            raise ValueError("cohort outcomes must cover exactly the authorization receipt members")

        receipt = CohortBatchLaunchOutcomesReceipt(
            account_id=account_id,
            cohort_id=cohort_id,
            outcomes=tuple(
                CohortBatchLaunchMemberOutcome.model_validate(outcome.model_dump())
                for outcome in request.outcomes
            ),
            recorded_at_ms=recorded_at_ms,
        )
        await asyncio.to_thread(record_cohort_batch_launch_outcomes, self._artifacts_root, receipt)
        return receipt
