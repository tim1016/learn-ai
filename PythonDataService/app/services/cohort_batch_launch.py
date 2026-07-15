"""Durable authorization service for deliberate cohort launches."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.account_artifacts import (
    CohortBatchLaunchOutcomesReceipt,
    CohortBatchLaunchReceipt,
    read_account_events,
)
from app.schemas.cohort_batch_launch import CohortBatchLaunchStatusResponse


class CohortBatchLaunchService:
    """Writes operator-authorized cohort launch receipts to account events."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._artifacts_root = artifacts_root

    async def get_status(
        self,
        *,
        account_id: str,
        cohort_id: str | None,
    ) -> CohortBatchLaunchStatusResponse | None:
        """Read the latest durable cohort state without inferring missing outcomes."""

        events = await asyncio.to_thread(read_account_events, self._artifacts_root, account_id)
        authorization = self._authorization_event(events, cohort_id)
        if authorization is None:
            if cohort_id is None:
                return None
            raise LookupError(f"cohort receipt not found: {cohort_id}")
        authorization_seq, receipt = authorization
        outcomes_receipt, outcomes_error = self._outcomes_after_authorization(
            events,
            authorization_seq=authorization_seq,
            receipt=receipt,
        )
        return CohortBatchLaunchStatusResponse.from_receipts(
            receipt,
            outcomes_receipt,
            outcomes_error=outcomes_error,
        )

    @staticmethod
    def _authorization_event(
        events: list[dict],
        cohort_id: str | None,
    ) -> tuple[int, CohortBatchLaunchReceipt] | None:
        for event in reversed(events):
            if event.get("event_type") != "cohort_batch_launch_authorized":
                continue
            if cohort_id is not None and event.get("cohort_id") != cohort_id:
                continue
            try:
                return int(event["seq"]), CohortBatchLaunchReceipt.model_validate(event)
            except (KeyError, TypeError, ValueError, ValidationError):
                identifier = cohort_id or event.get("cohort_id") or "latest"
                raise ValueError(f"cohort authorization is unreadable: {identifier}") from None
        return None

    @staticmethod
    def _outcomes_after_authorization(
        events: list[dict],
        *,
        authorization_seq: int,
        receipt: CohortBatchLaunchReceipt,
    ) -> tuple[CohortBatchLaunchOutcomesReceipt | None, str | None]:
        expected_members = set(receipt.member_strategy_instance_ids)
        for event in reversed(events):
            if event.get("event_type") != "cohort_batch_launch_outcomes_recorded":
                continue
            if event.get("cohort_id") != receipt.cohort_id:
                continue
            try:
                if int(event["seq"]) <= authorization_seq:
                    continue
                outcomes = CohortBatchLaunchOutcomesReceipt.model_validate(event)
                actual_members = {outcome.strategy_instance_id for outcome in outcomes.outcomes}
                if outcomes.account_id != receipt.account_id or actual_members != expected_members:
                    return None, "The persisted cohort outcomes do not match this authorization receipt."
                return outcomes, None
            except (KeyError, TypeError, ValueError, ValidationError):
                return None, "The persisted cohort outcomes are unreadable."
        return None, None
