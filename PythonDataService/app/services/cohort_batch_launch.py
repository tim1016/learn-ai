"""Durable authorization service for deliberate cohort launches."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.engine.live.account_artifacts import (
    CohortBatchLaunchReceipt,
    record_cohort_batch_launch_receipt,
)
from app.schemas.cohort_batch_launch import CohortBatchLaunchCreateRequest


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
