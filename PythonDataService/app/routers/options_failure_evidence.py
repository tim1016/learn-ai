"""Bounded read-only diagnostics over admitted, exposed Options evidence (#3399)."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.research import options_failure_evidence
from app.research.golden_search.proof import ProofMismatchError
from app.routers.options_golden_search import StudyId, refusal
from app.routers.research_store_unavailable import StoreUnreachableIsA503
from app.schemas.options_failure_evidence import OptionsFailureEvidence, OptionsFailureInspection
from app.utils.session_anchors import MAX_TIMESTAMP_MS

router = APIRouter(route_class=StoreUnreachableIsA503)
CandidateHash = Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")]


async def get_service():
    return options_failure_evidence


def unavailable(exc: Exception) -> HTTPException:
    code = exc.code if isinstance(exc, ProofMismatchError) else "RETAINED_EVIDENCE_UNREADABLE"
    return HTTPException(503, {"code": code, "message": "The exact retained evaluation is unavailable, unreadable or fails integrity checks. No replay, capture or mutable lake fallback was attempted."})


@router.get("/studies/{study_id}/candidate-evidence/{candidate_hash}/failures", response_model=OptionsFailureEvidence)
async def failures(study_id: StudyId, candidate_hash: CandidateHash,
    window: Literal["development", "final"] = "development", offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 50, api=Depends(get_service)) -> OptionsFailureEvidence:
    try:
        return await api.read(study_id, candidate_hash, window, offset=offset, limit=limit)
    except (KeyError, IndexError, TypeError) as exc:
        raise unavailable(exc) from exc
    except LookupError as exc:
        raise refusal(exc) from exc
    except (ValueError, OSError, ProofMismatchError) as exc:
        raise unavailable(exc) from exc


@router.get("/studies/{study_id}/candidate-evidence/{candidate_hash}/failures/inspect", response_model=OptionsFailureInspection)
async def inspect_failure(study_id: StudyId, candidate_hash: CandidateHash,
    session_open_ms: Annotated[int, Query(ge=0, le=MAX_TIMESTAMP_MS)],
    at_ms: Annotated[int, Query(ge=0, le=MAX_TIMESTAMP_MS)], leg_id: Annotated[str, Query(min_length=1, max_length=64)],
    window: Literal["development", "final"] = "development", api=Depends(get_service)) -> OptionsFailureInspection:
    try:
        return await api.inspect(study_id, candidate_hash, window, session_open_ms, at_ms, leg_id)
    except (KeyError, IndexError, TypeError) as exc:
        raise unavailable(exc) from exc
    except LookupError as exc:
        raise refusal(exc) from exc
    except (ValueError, OSError, ProofMismatchError) as exc:
        raise unavailable(exc) from exc
