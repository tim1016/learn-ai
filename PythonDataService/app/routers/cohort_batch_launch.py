"""Account-event endpoint for deliberate live-bot cohort launches."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_identity import normalize_account_id
from app.schemas.cohort_batch_launch import (
    CohortBatchLaunchCreateRequest,
    CohortBatchLaunchCreateResponse,
    CohortBatchLaunchOutcomesRequest,
    CohortBatchLaunchOutcomesResponse,
)
from app.services.account_truth_refresh import account_truth_artifacts_root
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.utils.timestamps import now_ms_utc

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def get_cohort_batch_launch_service() -> CohortBatchLaunchService:
    return CohortBatchLaunchService(artifacts_root=account_truth_artifacts_root())


CohortBatchLaunchDependency = Annotated[
    CohortBatchLaunchService,
    Depends(get_cohort_batch_launch_service),
]


@router.post(
    "/{account_id}/cohort-batch-launches",
    response_model=CohortBatchLaunchCreateResponse,
)
async def create_cohort_batch_launch_receipt_endpoint(
    account_id: str,
    request: CohortBatchLaunchCreateRequest,
    service: CohortBatchLaunchDependency,
) -> CohortBatchLaunchCreateResponse:
    """Record operator authorization before a deliberate multi-bot start."""

    try:
        receipt = await service.create_receipt(
            account_id=normalize_account_id(account_id),
            request=request,
            recorded_at_ms=now_ms_utc(),
        )
    except (AccountArtifactError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return CohortBatchLaunchCreateResponse.from_receipt(receipt)


@router.post(
    "/{account_id}/cohort-batch-launches/{cohort_id}/outcomes",
    response_model=CohortBatchLaunchOutcomesResponse,
)
async def record_cohort_batch_launch_outcomes_endpoint(
    account_id: str,
    cohort_id: str,
    request: CohortBatchLaunchOutcomesRequest,
    service: CohortBatchLaunchDependency,
) -> CohortBatchLaunchOutcomesResponse:
    """Persist every blocked or accepted member outcome under its authorization."""

    try:
        receipt = await service.record_outcomes(
            account_id=normalize_account_id(account_id),
            cohort_id=cohort_id,
            request=request,
            recorded_at_ms=now_ms_utc(),
        )
    except (AccountArtifactError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return CohortBatchLaunchOutcomesResponse.from_receipt(receipt)
