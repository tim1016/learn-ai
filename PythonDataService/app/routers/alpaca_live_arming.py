"""Typed HTTP entry points for supervised instance arming; no force route."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Path
from starlette.concurrency import run_in_threadpool

from app.broker.alpaca.active_binding import BrokerUnbound
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.live_arming import LiveArmingRefused
from app.broker_configuration.errors import BrokerConfigurationError
from app.schemas.alpaca_live_arming import ArmingApplyRequest, ArmingPlanView, ArmingStatusView
from app.services.alpaca_live_arming import AlpacaLiveArmingService, get_alpaca_live_arming_service
from app.services.alpaca_live_graduation_gate import graduation_mutation_fence

router = APIRouter(prefix="/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/arming", tags=["alpaca-live-arming"])
AccountId = Annotated[str, Path(min_length=1, max_length=120)]
InstanceId = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
Service = Annotated[AlpacaLiveArmingService, Depends(get_alpaca_live_arming_service)]


def _refuse(error: Exception) -> NoReturn:
    raise HTTPException(
        status_code=409,
        detail={
            "reason": error.reason_code if isinstance(error, LiveArmingRefused) else "LIVE_ARMING_EVIDENCE_UNAVAILABLE",
            "message": str(error)
            if isinstance(error, LiveArmingRefused)
            else "Arming evidence could not be read. Refresh and prepare a new plan.",
        },
    ) from error


def _refresh_gate() -> None:
    runtime = get_active_clerk_runtime()
    if runtime is not None and runtime.envelope_sync is not None:
        runtime.envelope_sync.refresh_arming()


@router.get("", response_model=ArmingStatusView)
async def arming_status(account_id: AccountId, sid: InstanceId, service: Service) -> ArmingStatusView:
    try:
        return await run_in_threadpool(service.status, account_id, sid)
    except (BrokerUnbound, BrokerConfigurationError, LiveArmingRefused, OSError, ValueError) as error:
        _refuse(error)


@router.post("/plan", response_model=ArmingPlanView)
async def arming_plan(account_id: AccountId, sid: InstanceId, service: Service) -> ArmingPlanView:
    try:
        return await run_in_threadpool(service.prepare, account_id, sid)
    except (BrokerUnbound, BrokerConfigurationError, LiveArmingRefused, OSError, ValueError) as error:
        _refuse(error)


@router.post("/apply", response_model=ArmingStatusView)
async def arming_apply(
    account_id: AccountId, sid: InstanceId, body: ArmingApplyRequest, service: Service
) -> ArmingStatusView:
    try:
        async with graduation_mutation_fence():
            result = await run_in_threadpool(
                service.apply, account_id, sid, plan_id=body.plan_id, confirmation_token=body.confirmation_token
            )
            await run_in_threadpool(_refresh_gate)
            return result
    except (BrokerUnbound, BrokerConfigurationError, LiveArmingRefused, OSError, ValueError) as error:
        _refuse(error)


@router.post("/disarm", response_model=ArmingStatusView)
async def arming_disarm(account_id: AccountId, sid: InstanceId, service: Service) -> ArmingStatusView:
    try:
        async with graduation_mutation_fence():
            result = await run_in_threadpool(service.disarm, account_id, sid)
            await run_in_threadpool(_refresh_gate)
            return result
    except (BrokerUnbound, BrokerConfigurationError, LiveArmingRefused, OSError, ValueError) as error:
        _refuse(error)
