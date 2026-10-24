"""GET /api/scoped-health: the one projection behind the Operations chips and the pages' notices.

Transport only. The reading, mapping and filtering live in
``app.services.scoped_health_service``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.data_lake.types import MAX_SYMBOL_LENGTH, SYMBOL_RE
from app.schemas.scoped_health import AffectedOperation, ScopedHealthResponse
from app.services.scoped_health_service import ScopedHealthService, default_readers

router = APIRouter(prefix="/api/scoped-health", tags=["scoped-health"])


async def get_scoped_health_service(request: Request) -> ScopedHealthService:
    """The process's one projection, built on first use so it can see the running app's archive service.

    ``async`` so the check-and-build runs on the event loop, never on two pool threads at once.
    """
    app = request.app
    service = getattr(app.state, "scoped_health_service", None)
    if service is None:
        service = ScopedHealthService(default_readers(lambda: getattr(app.state, "option_archive_service", None)))
        app.state.scoped_health_service = service
    return service


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
