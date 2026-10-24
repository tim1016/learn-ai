"""GET /api/scoped-health: the one projection behind the Operations chips and the pages' notices.

Transport only. The reading, mapping and filtering live in
``app.services.scoped_health_service``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.data_lake.types import MAX_SYMBOL_LENGTH, SYMBOL_RE
from app.schemas.scoped_health import AffectedOperation, ScopedHealthResponse
from app.services.scoped_health_service import ScopedHealthService, get_scoped_health_service

router = APIRouter(prefix="/api/scoped-health", tags=["scoped-health"])


@router.get("", response_model=ScopedHealthResponse)
async def scoped_health(
    operation: Annotated[
        list[AffectedOperation] | None,
        Query(description="Only findings that get in the way of these operations. Omitted: every finding."),
    ] = None,
    instrument: Annotated[
        str | None,
        Query(
            max_length=MAX_SYMBOL_LENGTH,
            pattern=SYMBOL_RE.pattern,
            description="Only findings about this instrument, plus those that cannot name one (broad).",
        ),
    ] = None,
    service: ScopedHealthService = Depends(get_scoped_health_service),
) -> ScopedHealthResponse:
    """Health findings with their scope, observed time, severity, affected operations and Operations link.

    A source that cannot be read answers ``unknown``, never healthy.
    """
    return await service.project(operations=frozenset(operation or ()), instrument=instrument)
