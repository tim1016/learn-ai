"""Golden Search qualified versions: list, inspect, offer to Deploy, revoke and re-proof (#2696)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.research.golden_search import qualification_service
from app.research.golden_search.qualification_service import JudgedQualification, QualificationRefusal
from app.research.persistence.db import with_connection
from app.schemas.golden_qualifications import (
    GoldenDefaultView,
    GoldenQualificationDeployOffer,
    GoldenQualificationDetail,
    GoldenQualificationSummary,
    ReproveQualificationRequest,
    RevokeQualificationRequest,
)
from app.services.strategy_validation_manifest import local_strategy_validation_actor

router = APIRouter()


def _refusal(exc: QualificationRefusal) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


async def _judged(qualification_id: str) -> JudgedQualification:
    judged = await with_connection(qualification_service.get_judged, qualification_id)
    if judged is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "QUALIFICATION_NOT_FOUND",
                "message": f"Golden Search qualification {qualification_id!r} was not found.",
            },
        )
    return judged


@router.get("", response_model=list[GoldenQualificationSummary])
async def list_golden_qualifications(
    program_key: str | None = Query(default=None, min_length=1, max_length=120),
    symbol: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[GoldenQualificationSummary]:
    judged = await with_connection(
        qualification_service.list_judged,
        program_key=program_key,
        symbol=symbol.upper() if symbol else None,
        limit=limit,
    )
    return [GoldenQualificationSummary.from_judged(item) for item in judged]


# Declared before ``/{qualification_id}`` so the literal segment wins.
@router.get("/defaults", response_model=list[GoldenDefaultView])
async def list_golden_defaults(
    program_key: str | None = Query(default=None, min_length=1, max_length=120),
    symbol: str | None = Query(default=None, min_length=1, max_length=32),
) -> list[GoldenDefaultView]:
    defaults = await with_connection(
        qualification_service.judged_defaults,
        program_key=program_key,
        symbol=symbol.upper() if symbol else None,
    )
    return [GoldenDefaultView.from_judged(item) for item in defaults]


@router.get("/{qualification_id}", response_model=GoldenQualificationDetail)
async def get_golden_qualification(qualification_id: str) -> GoldenQualificationDetail:
    return GoldenQualificationDetail.from_judged(await _judged(qualification_id))


@router.get("/{qualification_id}/deploy-offer", response_model=GoldenQualificationDeployOffer)
async def get_golden_qualification_deploy_offer(qualification_id: str) -> GoldenQualificationDeployOffer:
    """The exact tuple for Deploy. Answered whatever the status; Deploy applies only a ready one."""
    return GoldenQualificationDeployOffer.from_judged(await _judged(qualification_id))


@router.post("/{qualification_id}/revoke", response_model=GoldenQualificationDetail)
async def revoke_golden_qualification(
    qualification_id: str, body: RevokeQualificationRequest
) -> GoldenQualificationDetail:
    try:
        judged = await qualification_service.revoke(
            qualification_id=qualification_id,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            actor=local_strategy_validation_actor(),
        )
    except QualificationRefusal as exc:
        raise _refusal(exc) from exc
    return GoldenQualificationDetail.from_judged(judged)


@router.post("/{qualification_id}/reprove", response_model=GoldenQualificationDetail)
async def reprove_golden_qualification(
    qualification_id: str, body: ReproveQualificationRequest
) -> GoldenQualificationDetail:
    try:
        judged = await qualification_service.reprove_qualification(
            qualification_id=qualification_id,
            idempotency_key=body.idempotency_key,
            actor=local_strategy_validation_actor(),
        )
    except QualificationRefusal as exc:
        raise _refusal(exc) from exc
    return GoldenQualificationDetail.from_judged(judged)
