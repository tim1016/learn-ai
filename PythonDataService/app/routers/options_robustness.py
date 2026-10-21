"""Bounded read-only robustness for admitted candidates and exposed evidence (#3401)."""
from __future__ import annotations

from types import ModuleType
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.research import options_robustness
from app.research.golden_search.proof import ProofMismatchError
from app.routers.options_golden_search import StudyId
from app.routers.research_store_unavailable import StoreUnreachableIsA503
from app.schemas.options_robustness import OptionsCandidateRobustness, OptionsNeighborPage

router = APIRouter(route_class=StoreUnreachableIsA503)
CandidateHash = Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")]


def get_service() -> ModuleType:
    return options_robustness


def unavailable() -> HTTPException:
    return HTTPException(503, {"code": "RETAINED_ROBUSTNESS_UNAVAILABLE", "message": "The retained robustness evidence fails its identity or shape checks. No new evaluation or mutable input was used."})


@router.get("/studies/{study_id}/candidate-evidence/{candidate_hash}/robustness", response_model=OptionsCandidateRobustness)
async def read(study_id: StudyId, candidate_hash: CandidateHash, window: Literal["development", "final"] = "development",
    api: ModuleType = Depends(get_service)) -> OptionsCandidateRobustness:
    try:
        return await api.read(study_id, candidate_hash, window)
    except (KeyError, IndexError, TypeError, ValueError, OSError, ProofMismatchError) as exc:
        raise unavailable() from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.get("/studies/{study_id}/candidate-evidence/{candidate_hash}/neighbors", response_model=OptionsNeighborPage)
async def neighbors(study_id: StudyId, candidate_hash: CandidateHash, window: Literal["development", "final"] = "development",
    offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=50)] = 50,
    api: ModuleType = Depends(get_service)) -> OptionsNeighborPage:
    try:
        return await api.neighbors(study_id, candidate_hash, window, offset=offset, limit=limit)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise unavailable() from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
