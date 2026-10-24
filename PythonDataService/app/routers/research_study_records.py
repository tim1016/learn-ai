"""A stored study run over REST: ``GET /api/research/study-records/{study}/{record_id}``.

Transport only. Opening a record reads what was written when the run ended and runs nothing: there is no write here, and
no path from a read to a recompute. The lists of these records are the Research History's (``/api/research/history``);
this is the detail a history entry of a study type opens.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Path as PathParam

from app.research.study_records.models import StudyRecordConfig, StudyRecordResult, StudyType
from app.research.study_records.specs import STUDY_SPECS
from app.research.study_records.store import StudyRecordCorruptError, StudyRecordNotFoundError, StudyRecordStore
from app.schemas.research_history import ResearchHistoryRef, ResearchHistoryWindow
from app.schemas.research_study_records import StoredStudyRecord, StudyRecordRefusalBody

logger = logging.getLogger(__name__)

router = APIRouter()

RECORD_ID_MAX_LENGTH = 64


def get_study_records_root() -> Path | None:
    """Where the records are read from; ``None`` is the real artifact root. Tests override this."""
    return None


def _stored(config: StudyRecordConfig, result: StudyRecordResult) -> StoredStudyRecord:
    spec = STUDY_SPECS[config.study]
    window = spec.window(config.inputs)
    return StoredStudyRecord(
        study=config.study,
        record_id=config.record_id,
        schema_version=config.schema_version,
        status=config.status,
        status_detail=config.status_detail,
        question=spec.question(config.inputs),
        symbols=list(spec.symbols(config.inputs)),
        window=None if window is None else ResearchHistoryWindow(start_ms=window[0], end_ms=window[1]),
        created_at_ms=config.created_at_ms,
        completed_at_ms=config.completed_at_ms,
        inputs=config.inputs,
        data_source=config.data_source,
        data_identity=config.data_identity,
        job_id=config.job_id,
        rerun_of=None if config.rerun_of is None else ResearchHistoryRef(type=config.study, native_id=config.rerun_of),
        result=result.result,
    )


@router.get(
    "/{study}/{record_id}",
    response_model=StoredStudyRecord,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": StudyRecordRefusalBody},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": StudyRecordRefusalBody},
    },
)
async def get_study_record(
    study: StudyType,
    record_id: Annotated[str, PathParam(min_length=1, max_length=RECORD_ID_MAX_LENGTH)],
    root: Annotated[Path | None, Depends(get_study_records_root)],
) -> StoredStudyRecord:
    """One recorded run. A record that cannot be read is a 500 that says so, never a partial one."""
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "STUDY_RECORD_NOT_FOUND", "message": f"No {study} run is recorded as {record_id}."},
    )
    try:
        config, result = await asyncio.to_thread(StudyRecordStore(study, root=root).load, record_id)
    except StudyRecordNotFoundError as exc:
        raise not_found from exc
    except StudyRecordCorruptError as exc:
        logger.error(
            "a stored study run cannot be read",
            extra={"action": "study_record_unreadable", "study": study, "record_id": record_id, "reason": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "STUDY_RECORD_UNREADABLE",
                "message": f"The recorded {study} run {record_id} cannot be read: it may have been written by a newer build.",
            },
        ) from exc
    except ValueError as exc:  # an id that no record can have
        raise not_found from exc
    try:
        return _stored(config, result)
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "a stored study run's inputs cannot be read",
            extra={"action": "study_record_inputs_unreadable", "study": study, "record_id": record_id, "reason": repr(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "STUDY_RECORD_UNREADABLE",
                "message": f"The recorded {study} run {record_id} cannot be read: its inputs are not ones this build understands.",
            },
        ) from exc
