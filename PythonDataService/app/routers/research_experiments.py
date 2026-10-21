"""The Research Lab's experiment history over REST (``/api/research/experiments``, ``/api/research/signal-experiments``).

Replaces the GraphQL ``getResearchExperiments`` / ``getSignalExperiments`` / ``getSignalExperimentReport`` queries of
the retired .NET backend. Read-only: nothing writes these tables any more, and the rows are legacy history.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.routers.research_store_unavailable import StoreUnreachableIsA503
from app.schemas.research_experiments import (
    MAX_EXPERIMENT_ID,
    TICKER_MAX_LENGTH,
    ResearchExperimentRefusalBody,
    ResearchExperimentResponse,
    SignalExperimentReport,
    SignalExperimentResponse,
)
from app.services.research_experiment_service import (
    PostgresResearchExperimentStore,
    ResearchExperimentStore,
    StoredReportUnreadable,
)

logger = logging.getLogger(__name__)


class _ExperimentsStoreUnreachableIsA503(StoreUnreachableIsA503):
    unavailable_detail = {
        "code": "RESEARCH_EXPERIMENTS_STORE_UNAVAILABLE",
        "message": "The stored experiments could not be reached. Try again once the database is back.",
    }


router = APIRouter(route_class=_ExperimentsStoreUnreachableIsA503)


def get_experiment_store() -> ResearchExperimentStore:
    return PostgresResearchExperimentStore()


ExperimentStore = Annotated[ResearchExperimentStore, Depends(get_experiment_store)]
TickerQuery = Annotated[
    str, Query(min_length=1, max_length=TICKER_MAX_LENGTH, description="The ticker's symbol; case does not matter.")
]
ExperimentId = Annotated[int, Path(ge=1, le=MAX_EXPERIMENT_ID)]


@router.get("/experiments", response_model=list[ResearchExperimentResponse])
async def list_research_experiments(ticker: TickerQuery, store: ExperimentStore) -> list[ResearchExperimentResponse]:
    """A ticker's stored feature-research experiments, newest first."""
    return await store.research_experiments(ticker)


@router.get("/signal-experiments", response_model=list[SignalExperimentResponse])
async def list_signal_experiments(ticker: TickerQuery, store: ExperimentStore) -> list[SignalExperimentResponse]:
    """A ticker's stored signal-engine experiments, newest first, each as its headline without the full report."""
    return await store.signal_experiments(ticker)


@router.get(
    "/signal-experiments/{experiment_id}/report",
    response_model=SignalExperimentReport,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ResearchExperimentRefusalBody},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ResearchExperimentRefusalBody},
    },
)
async def get_signal_experiment_report(experiment_id: ExperimentId, store: ExperimentStore) -> SignalExperimentReport:
    """One signal experiment's full report. A stored report that cannot be read is a 500 that says so, never a partial one."""
    try:
        report = await store.signal_report(experiment_id)
    except StoredReportUnreadable as exc:
        logger.error(
            "a stored signal report cannot be read",
            extra={"action": "signal_report_unreadable", "experiment_id": experiment_id, "reason": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SIGNAL_REPORT_UNREADABLE",
                "message": f"The stored report of signal experiment {experiment_id} cannot be read: {exc}",
            },
        ) from exc
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SIGNAL_EXPERIMENT_NOT_FOUND",
                "message": f"Signal experiment {experiment_id} not found",
            },
        )
    return report
