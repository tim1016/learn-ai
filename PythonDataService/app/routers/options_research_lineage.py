"""Read-only frozen Options procedure and durable trial ledger (#3400)."""
from __future__ import annotations

from types import ModuleType
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.research import options_research_lineage as lineage
from app.routers.research_store_unavailable import StoreUnreachableIsA503
from app.schemas.options_research_lineage import OptionsLineageTrialPage, OptionsResearchLineage

router = APIRouter(route_class=StoreUnreachableIsA503)
StudyId = Annotated[str, Path(pattern=r"^os_[a-f0-9]{40}$")]


def get_lineage_service() -> ModuleType:
    return lineage


@router.get("/studies/{study_id}/lineage", response_model=OptionsResearchLineage)
async def read_lineage(study_id: StudyId, service: ModuleType = Depends(get_lineage_service)) -> OptionsResearchLineage:
    try:
        return await service.read(study_id)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(status_code=503, detail={"code": "RETAINED_PROCEDURE_UNAVAILABLE", "message": "The retained procedure fails its identity or shape checks."}) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/studies/{study_id}/lineage/trials", response_model=OptionsLineageTrialPage)
async def read_trials(study_id: StudyId, after: Annotated[str, Query(pattern=r"^(0|[1-9][0-9]{0,18})$")] = "0",
    limit: Annotated[int, Query(ge=1, le=50)] = 50, service: ModuleType = Depends(get_lineage_service)) -> OptionsLineageTrialPage:
    if int(after) > 2**63 - 1:
        raise HTTPException(status_code=422, detail="The trial cursor exceeds its recorded ID range.")
    try:
        return await service.trials(study_id, after=after, limit=limit)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise HTTPException(status_code=503, detail={"code": "RETAINED_TRIALS_UNAVAILABLE", "message": "The retained trial ledger could not be decoded."}) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
