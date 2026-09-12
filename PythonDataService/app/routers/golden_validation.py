"""Golden Validation designation, evidence, and human-review endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.research.golden_validation import service
from app.research.persistence.db import with_connection
from app.schemas.golden_validation import (
    DesignateGoldenRunRequest,
    GoldenValidationApplicabilityRequest,
    GoldenValidationApplicabilityResponse,
    GoldenValidationResponse,
    ReviewGoldenRunRequest,
)
from app.services.strategy_validation_manifest import local_strategy_validation_actor

router = APIRouter()


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "GOLDEN_VALIDATION_NOT_FOUND", "message": message},
    )


def _workflow_error(exc: service.GoldenValidationError) -> HTTPException:
    if isinstance(exc, service.GoldenRunNotFoundError):
        return _not_found(str(exc))
    if isinstance(exc, service.StaleEvidenceError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STALE_GOLDEN_VALIDATION_EVIDENCE",
                "message": str(exc),
                "current_evidence_revision": exc.current_revision,
            },
        )
    if isinstance(exc, service.CommandConflictError):
        code = "GOLDEN_VALIDATION_COMMAND_CONFLICT"
    else:
        code = "GOLDEN_VALIDATION_INELIGIBLE"
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": code, "message": str(exc)})


@router.post("", response_model=GoldenValidationResponse, status_code=status.HTTP_201_CREATED)
async def designate_golden_run(body: DesignateGoldenRunRequest) -> GoldenValidationResponse:
    try:
        dossier = await with_connection(
            service.designate,
            source_run_id=body.source_run_id,
            command_id=body.command_id,
            label=body.label,
            rationale=body.rationale,
            actor=local_strategy_validation_actor(),
        )
    except service.GoldenValidationError as exc:
        raise _workflow_error(exc) from exc
    return GoldenValidationResponse.from_dossier(dossier)


@router.get("", response_model=list[GoldenValidationResponse])
async def list_golden_runs(
    strategy_name: str | None = Query(default=None, min_length=1, max_length=120),
    symbol: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[GoldenValidationResponse]:
    dossiers = await with_connection(
        service.list_dossiers,
        strategy_name=strategy_name,
        symbol=symbol.upper() if symbol else None,
        limit=limit,
    )
    return [GoldenValidationResponse.from_dossier(dossier) for dossier in dossiers]


@router.get("/{golden_run_id}", response_model=GoldenValidationResponse)
async def get_golden_run(golden_run_id: int) -> GoldenValidationResponse:
    dossier = await with_connection(service.get_dossier, golden_run_id)
    if dossier is None:
        raise _not_found(f"Validation Golden Run {golden_run_id} was not found.")
    return GoldenValidationResponse.from_dossier(dossier)


@router.post("/{golden_run_id}/applicability", response_model=GoldenValidationApplicabilityResponse)
async def assess_golden_run(
    golden_run_id: int,
    body: GoldenValidationApplicabilityRequest,
) -> GoldenValidationApplicabilityResponse:
    dossier = await with_connection(service.get_dossier, golden_run_id)
    if dossier is None:
        raise _not_found(f"Validation Golden Run {golden_run_id} was not found.")
    receipt = service.assess(dossier, body.as_configuration())
    return GoldenValidationApplicabilityResponse(
        golden_validation_id=receipt.golden_validation_id,
        applicable=receipt.applicable,
        state=receipt.state,
        classification=receipt.classification,
        mismatched_fields=list(receipt.mismatched_fields),
        explanation=receipt.explanation,
    )


@router.post("/{golden_run_id}/reviews", response_model=GoldenValidationResponse)
async def review_golden_run(golden_run_id: int, body: ReviewGoldenRunRequest) -> GoldenValidationResponse:
    try:
        dossier = await with_connection(
            service.review,
            golden_run_id=golden_run_id,
            command_id=body.command_id,
            expected_evidence_revision=body.expected_evidence_revision,
            decision=body.decision,
            reason=body.reason,
            quantconnect_backtest_id=body.quantconnect_backtest_id,
            authorized_program_version=body.authorized_program_version,
            actor=local_strategy_validation_actor(),
        )
    except service.GoldenValidationError as exc:
        raise _workflow_error(exc) from exc
    return GoldenValidationResponse.from_dossier(dossier)
