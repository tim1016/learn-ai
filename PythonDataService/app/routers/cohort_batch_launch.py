"""Account-event endpoint for deliberate live-bot cohort launches."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_identity import normalize_account_id
from app.schemas.cohort_batch_launch import CohortBatchLaunchStatusResponse
from app.schemas.cohort_validation_certificate import CohortValidationCertificate
from app.services.account_truth_refresh import account_truth_artifacts_root
from app.services.cohort_batch_launch import CohortBatchLaunchService
from app.services.cohort_validation_certificate import CohortValidationCertificateService

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def get_cohort_batch_launch_service() -> CohortBatchLaunchService:
    return CohortBatchLaunchService(artifacts_root=account_truth_artifacts_root())


def get_cohort_validation_certificate_service() -> CohortValidationCertificateService:
    return CohortValidationCertificateService(artifacts_root=account_truth_artifacts_root())


CohortBatchLaunchDependency = Annotated[
    CohortBatchLaunchService,
    Depends(get_cohort_batch_launch_service),
]
CohortCertificateDependency = Annotated[
    CohortValidationCertificateService,
    Depends(get_cohort_validation_certificate_service),
]


@router.get(
    "/{account_id}/cohort-batch-launches/latest",
    response_model=CohortBatchLaunchStatusResponse | None,
)
async def get_latest_cohort_batch_launch_status_endpoint(
    account_id: str,
    service: CohortBatchLaunchDependency,
) -> CohortBatchLaunchStatusResponse | None:
    """Return the most recent account-rooted cohort receipt and outcomes."""

    try:
        return await service.get_status(
            account_id=normalize_account_id(account_id),
            cohort_id=None,
        )
    except (AccountArtifactError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get(
    "/{account_id}/cohort-batch-launches/{cohort_id}",
    response_model=CohortBatchLaunchStatusResponse,
)
async def get_cohort_batch_launch_status_endpoint(
    account_id: str,
    cohort_id: str,
    service: CohortBatchLaunchDependency,
) -> CohortBatchLaunchStatusResponse:
    """Return one durable cohort receipt and its exact persisted outcomes."""

    try:
        status_view = await service.get_status(
            account_id=normalize_account_id(account_id),
            cohort_id=cohort_id,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (AccountArtifactError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if status_view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cohort receipt not found: {cohort_id}")
    return status_view


@router.post(
    "/{account_id}/cohort-batch-launches/{cohort_id}/certificate",
    response_model=CohortValidationCertificate,
    status_code=status.HTTP_201_CREATED,
)
async def create_cohort_validation_certificate_endpoint(
    account_id: str,
    cohort_id: str,
    service: CohortCertificateDependency,
) -> CohortValidationCertificate:
    """Generate once from durable evidence; never overwrite a certificate."""

    try:
        certificate = await service.generate(
            account_id=normalize_account_id(account_id),
            cohort_id=cohort_id,
        )
        await asyncio.to_thread(service.write_once, certificate)
        return certificate
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (AccountArtifactError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get(
    "/{account_id}/cohort-batch-launches/{cohort_id}/certificate",
    response_model=CohortValidationCertificate,
)
async def get_cohort_validation_certificate_endpoint(
    account_id: str,
    cohort_id: str,
    service: CohortCertificateDependency,
) -> CohortValidationCertificate:
    """Read the immutable server-authored certificate without recomputation."""

    certificate = await asyncio.to_thread(
        service.read,
        account_id=normalize_account_id(account_id),
        cohort_id=cohort_id,
    )
    if certificate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cohort certificate not found: {cohort_id}")
    return certificate
