"""Dev-only HTTP surface to arm the fault-injection seam (PRD #1354).

Registered by ``main.py`` **only** when ``ALPACA_FAULT_INJECTION_ENABLED`` is
set (routes 404 otherwise), and — like every broker control route — gated by the
always-on data-plane control secret. Arming is additionally refused by the
registry unless the Alpaca posture is paper, so a validation tool can never
touch real funds.

Transport only: it validates the request, calls the registry, and shapes the
response or translates a typed refusal.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.broker.alpaca.fault_injection import (
    FaultInjectionRefused,
    get_fault_injection_registry,
)
from app.schemas.fault_injection import (
    ArmFaultRequest,
    FaultInjectionDisarmResult,
    FaultInjectionStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/brokers/alpaca/fault-injection",
    tags=["alpaca-fault-injection"],
)


@router.post("/arm", response_model=FaultInjectionStatus)
async def arm_fault(request: ArmFaultRequest) -> FaultInjectionStatus:
    """Arm one fault; refuses (403) unless the seam is permitted (paper only)."""
    registry = get_fault_injection_registry()
    try:
        registry.arm(
            request.kind,
            target=request.target,
            count=request.count,
            params=request.params,
        )
    except FaultInjectionRefused as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info(
        "[fault-injection] armed %s (target=%s, count=%d)",
        request.kind,
        request.target,
        request.count,
    )
    return FaultInjectionStatus(**registry.status())


@router.post("/disarm", response_model=FaultInjectionDisarmResult)
async def disarm_faults() -> FaultInjectionDisarmResult:
    """Clear every armed fault."""
    cleared = get_fault_injection_registry().disarm_all()
    return FaultInjectionDisarmResult(cleared=cleared)


@router.get("/status", response_model=FaultInjectionStatus)
async def fault_injection_status() -> FaultInjectionStatus:
    """Report whether the seam is permitted and what is currently armed."""
    return FaultInjectionStatus(**get_fault_injection_registry().status())
