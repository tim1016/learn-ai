"""The public jobs surface: ``/api/jobs`` is what the browser starts, watches, cancels and collects jobs through.

Transport only; the work is :mod:`app.jobs.facade`. A start mints the job id,
records it in Redis and dispatches the body to the type's internal route
(``/api/jobs-internal/*``, which is where each type's schema is owned); the
rest read and flag the Redis records the workers write
(:mod:`app.jobs.progress`). There is no authentication here: the browser
reaches it the same way it reaches the rest of the data plane's reads.
"""

from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.jobs import facade
from app.schemas.jobs_facade import (
    JobAcceptedResponse,
    JobCancelResponse,
    JobErrorResponse,
    JobRejectedProblem,
    UnsupportedJobTypeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_STREAM_ID = re.compile(r"^\d+(-\d+)?$")
_START_BODY_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
    }
}


def _not_found(exc: facade.JobRecordNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": str(exc)})


def _problem(status_code: int, detail: str | None) -> JSONResponse:
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    return JSONResponse(
        status_code=status_code,
        content={"title": title, "status": status_code, "detail": detail or ""},
        media_type="application/problem+json",
    )


def _attachment_disposition(filename: str) -> str:
    """``attachment`` with the recorded name; a non-ASCII name also travels as an RFC 5987 ``filename*``."""
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", filename)
    if fallback == filename:
        return f'attachment; filename="{fallback}"'
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.post(
    "/{job_type}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    responses={
        400: {"model": UnsupportedJobTypeResponse, "description": "Unknown job type, or a body that is not JSON."},
        "4XX": {"model": JobRejectedProblem, "description": "The type's worker route refused the start."},
        502: {"model": JobRejectedProblem, "description": "The worker route could not be reached or crashed."},
    },
    openapi_extra=_START_BODY_SCHEMA,
)
async def start_job(job_type: str, request: Request) -> Response:
    """Start a job of ``job_type``: the body goes to its worker route as sent, plus the minted ``job_id``."""
    if job_type not in facade.JOB_TYPE_ROUTES:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": f"unsupported job type '{job_type}'", "supported": list(facade.JOB_TYPE_ROUTES)},
        )
    try:
        parsed = json.loads(await request.body())
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"error": "the request body must be a JSON document"}
        )
    # Only an object carries parameters; any other JSON value starts the job with none.
    body: dict[str, Any] = parsed if isinstance(parsed, dict) else {}

    outcome = await facade.start_job(request.app, job_type, body)
    if not outcome.accepted:
        return _problem(outcome.status_code, outcome.detail)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"id": outcome.job_id, "status": "queued"},
        headers={"Location": f"/api/jobs/{outcome.job_id}/events"},
    )


@router.get(
    "/{job_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}, "description": "SSE job events."},
        400: {"model": JobErrorResponse},
    },
)
async def stream_job_events(
    job_id: str,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    """Server-sent events for a job; ``Last-Event-ID`` (a Redis stream entry id) resumes after that entry."""
    if last_event_id and not _STREAM_ID.fullmatch(last_event_id):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Last-Event-ID must be a stream entry id"}
        )
    return StreamingResponse(
        facade.stream_job_events(job_id, last_event_id),
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # no proxy buffering
        },
    )


@router.get(
    "/{job_id}/result",
    responses={200: {"content": {"application/json": {"schema": {}}}}, 404: {"model": JobErrorResponse}},
)
async def get_job_result(job_id: str) -> Response:
    """The JSON a completed job stored, verbatim."""
    try:
        raw = await facade.read_result(job_id)
    except facade.JobRecordNotFoundError as exc:
        return _not_found(exc)
    return Response(content=raw, media_type="application/json")


@router.get(
    "/{job_id}/download",
    responses={
        200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
        404: {"model": JobErrorResponse},
    },
)
async def download_job_blob(job_id: str) -> Response:
    """The binary a completed job stored (a ZIP, a PDF), under the name and type it recorded."""
    try:
        blob = await facade.read_download(job_id)
    except facade.JobRecordNotFoundError as exc:
        return _not_found(exc)
    return Response(
        content=blob.body,
        media_type=blob.content_type,
        headers={"Content-Disposition": _attachment_disposition(blob.filename)},
    )


@router.delete("/{job_id}", response_model=JobCancelResponse, responses={404: {"model": JobErrorResponse}})
async def cancel_job(job_id: str) -> Response:
    """Ask a job's worker to stop. Cooperative: the worker finishes its own record."""
    try:
        await facade.request_cancel(job_id)
    except facade.JobRecordNotFoundError as exc:
        return _not_found(exc)
    return JSONResponse(content={"id": job_id, "cancel_requested": True})


@router.get("", response_model=list[dict[str, str]])
async def list_jobs(active: bool = Query(default=True)) -> list[dict[str, str]]:
    """The state record of every queued or running job. There is no index of finished jobs, so ``active=false`` is empty."""
    if not active:
        return []
    return await facade.list_active_jobs()
