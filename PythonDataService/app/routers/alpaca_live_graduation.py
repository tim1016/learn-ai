"""HTTP transport for the supervised Alpaca Shadow-to-Live graduation."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, status

from app.schemas.alpaca_live_graduation import (
    LiveGraduationApplyOutcome,
    LiveGraduationApplyRequest,
    LiveGraduationPlanView,
    LiveGraduationStatus,
)
from app.services.alpaca_live_graduation import (
    AlpacaLiveGraduationService,
    LiveGraduationRefused,
    get_alpaca_live_graduation_service,
    restart_after_graduation,
)

router = APIRouter(
    prefix="/api/brokers/alpaca/accounts/{account_id}/live-graduation",
    tags=["alpaca-live-graduation"],
)

AccountId = Annotated[str, Path(min_length=1, max_length=120)]
ServiceDep = Annotated[
    AlpacaLiveGraduationService,
    Depends(get_alpaca_live_graduation_service),
]


def _raise_refusal(error: LiveGraduationRefused) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "reason": error.reason,
            "message": str(error),
            "next_action": error.next_action,
        },
    ) from error


@router.get("", response_model=LiveGraduationStatus)
async def read_live_graduation_status(
    account_id: AccountId,
    service: ServiceDep,
) -> LiveGraduationStatus:
    try:
        return service.status(account_id)
    except LiveGraduationRefused as error:
        _raise_refusal(error)


@router.post("/plan", response_model=LiveGraduationPlanView)
async def prepare_live_graduation(
    account_id: AccountId,
    service: ServiceDep,
) -> LiveGraduationPlanView:
    try:
        return await service.prepare(account_id)
    except LiveGraduationRefused as error:
        _raise_refusal(error)


@router.post(
    "/apply",
    response_model=LiveGraduationApplyOutcome,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_live_graduation(
    account_id: AccountId,
    body: LiveGraduationApplyRequest,
    background_tasks: BackgroundTasks,
    service: ServiceDep,
) -> LiveGraduationApplyOutcome:
    try:
        outcome = await service.apply(
            account_id,
            plan_id=body.plan_id,
            confirmation_token=body.confirmation_token,
        )
    except LiveGraduationRefused as error:
        _raise_refusal(error)
    background_tasks.add_task(restart_after_graduation)
    return outcome

