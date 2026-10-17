"""Owner qualification of exact tested Options setups; no broker actions."""

from __future__ import annotations

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.options_bots.golden import GoldenOptionsSetups, GoldenSetupError
from app.research.golden_search.proof import ProofMismatchError
from app.schemas.golden_option_setups import (
    ApproveGoldenOptionSetupRequest,
    GoldenOptionSetupRefusal,
    GoldenOptionSetupView,
    ReproveGoldenOptionSetupRequest,
    RevokeGoldenOptionSetupRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()
_REFUSALS = {code: {"model": GoldenOptionSetupRefusal} for code in (404, 409, 503)}
QualificationId = Annotated[str, Path(pattern=r"^ogs_[a-f0-9]{32}$")]


def service() -> GoldenOptionsSetups:
    return GoldenOptionsSetups()


Service = Annotated[GoldenOptionsSetups, Depends(service)]


def _refusal(exc: Exception) -> HTTPException:
    if isinstance(exc, GoldenSetupError):
        return HTTPException(exc.http_status, {"code": exc.code, "message": exc.message})
    if isinstance(exc, ProofMismatchError):
        return HTTPException(409, {"code": "PROOF_MISMATCH", "message": "The archived proof inputs are missing or damaged."})
    logger.warning("Golden Options evidence unavailable", extra={"action": "golden_options_unavailable"}, exc_info=exc)
    return HTTPException(503, {"code": "EVIDENCE_UNAVAILABLE", "message": "The retained Options evidence could not be read. Try again when its store is available."})


_ERRORS = (GoldenSetupError, ProofMismatchError, OSError, asyncpg.PostgresError, CatalogUnavailableError, LakeAdmissionError)


@router.get("", response_model=list[GoldenOptionSetupView], responses=_REFUSALS)
async def list_golden_setups(module: Service, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[GoldenOptionSetupView]:
    try:
        return await module.list(limit=limit)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.post("/approve", response_model=GoldenOptionSetupView, responses=_REFUSALS)
async def approve_golden_setup(body: ApproveGoldenOptionSetupRequest, module: Service) -> GoldenOptionSetupView:
    try:
        return await module.approve(body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.get("/{qualification_id}", response_model=GoldenOptionSetupView, responses=_REFUSALS)
async def get_golden_setup(qualification_id: QualificationId, module: Service) -> GoldenOptionSetupView:
    try:
        return await module.get(qualification_id)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.post("/{qualification_id}/revoke", response_model=GoldenOptionSetupView, responses=_REFUSALS)
async def revoke_golden_setup(
    qualification_id: QualificationId, body: RevokeGoldenOptionSetupRequest, module: Service,
) -> GoldenOptionSetupView:
    try:
        return await module.revoke(qualification_id, body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc


@router.post("/{qualification_id}/reprove", response_model=GoldenOptionSetupView, responses=_REFUSALS)
async def reprove_golden_setup(
    qualification_id: QualificationId, body: ReproveGoldenOptionSetupRequest, module: Service,
) -> GoldenOptionSetupView:
    try:
        return await module.reprove(qualification_id, body)
    except _ERRORS as exc:
        raise _refusal(exc) from exc
