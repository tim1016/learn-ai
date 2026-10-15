"""The API's forwarding client for research dispatches to the isolated runtime (#3043).

When ``RESEARCH_EXECUTION_MODE=research``, the API keeps its HTTP contract
with the job transport (.NET mints ids and still posts ``/api/jobs-internal``)
but hands each research dispatch to the isolated research runtime over the
internal boundary — the same private-target-enforced, redirect-refusing
client the fleet uses, authenticated with the data-plane control secret both
containers already share.

The one subtlety is the timeout: an HTTP timeout is not proof the runtime
did not accept the job (PRD #3034 — "command delivery is idempotent under
the existing job/study identity and attempt fence"). On a transport failure
the API asks the durable study row what actually happened: a job already
bound to this job id joined the original acceptance (the runtime's lease is
the liveness authority from here), anything else refuses with the reason.
The API never binds, stamps or starts a thread in this mode — ownership
moves with execution (#3042).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.broker.fleet.internal_http import build_internal_client, enforce_private_http_target
from app.config import settings
from app.security.data_plane_control import CONTROL_SECRET_HEADER

logger = logging.getLogger(__name__)

# A dispatch is accepted once the runtime binds it; the engine run itself is
# unbounded (the job store carries it), so only the hop is timed.
FORWARD_TIMEOUT_S = 10.0


def research_forwarding_enabled() -> bool:
    """Whether this process forwards research dispatches instead of executing them."""
    return settings.RESEARCH_EXECUTION_MODE == "research"


def is_research_runtime() -> bool:
    """Whether this process IS the research runtime (``RESEARCH_RUNTIME=1``, #3043).

    The runtime container shares the deployment's ``RESEARCH_EXECUTION_MODE``
    configuration; this marker is what keeps its dispatch route executing
    rather than forwarding to itself, and what keys the checkpoint-gated
    lease contract to the process that owns execution.
    """
    import os

    return os.environ.get("RESEARCH_RUNTIME", "").strip() == "1"


async def _require_runtime_url() -> str:
    url = settings.RESEARCH_RUNTIME_URL.strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RESEARCH_EXECUTION_MODE=research requires RESEARCH_RUNTIME_URL; no research dispatch can run",
        )
    from app.broker.fleet.internal_http import FleetTransportRefused

    try:
        await asyncio.to_thread(enforce_private_http_target, url)
    except FleetTransportRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"RESEARCH_RUNTIME_URL is not a usable internal destination: {exc}",
        ) from exc
    return url


async def forward_golden_search_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand one golden-search dispatch to the research runtime; reconcile on transport failure.

    ``payload`` is the jobs-internal request body (study id, stage token,
    job id). Returns the runtime's JSON answer, or raises the HTTP error the
    API should surface: the runtime's own refusals pass through with their
    status and reason; a transport failure with no durable binding is a 502.
    """
    try:
        response = await _post_runtime_dispatch("/api/jobs-internal/golden-search", payload)
    except httpx.HTTPError as exc:
        return await _reconcile_after_transport_failure(payload, exc)
    return _answer_of(response)


async def forward_grid_search_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand one grid-search dispatch to the research runtime (#3049); reconcile on transport failure.

    Same contract as the golden hop: the runtime's refusals pass through,
    and a transport failure is reconciled against the durable search row
    before the API answers — a timeout is not a refusal, but silence with
    no live binding is a 502, never a quiet second search.
    """
    try:
        response = await _post_runtime_dispatch("/api/jobs-internal/grid-search", payload)
    except httpx.HTTPError as exc:
        return await _reconcile_grid_after_transport_failure(payload, exc)
    return _answer_of(response)


async def _post_runtime_dispatch(path: str, payload: dict[str, Any]) -> httpx.Response:
    """One authenticated hop to the runtime's internal dispatch surface."""
    base = await _require_runtime_url()
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    async with build_internal_client(timeout_s=FORWARD_TIMEOUT_S) as client:
        return await client.post(f"{base}{path}", json=payload, headers={CONTROL_SECRET_HEADER: secret})


def _answer_of(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == status.HTTP_202_ACCEPTED:
        return response.json()
    raise HTTPException(status_code=response.status_code, detail=_detail_of(response))


async def _grid_job_binding(job_id: str) -> str | None:
    """The search id a job is bound to, if any — the durable evidence a grid reconcile reads (#3049)."""

    from app.research.grid_search import repository as repo
    from app.research.persistence.db import with_connection

    bound = await with_connection(repo.list_searches, job_id=job_id, limit=1)
    return bound[0].id if bound else None


async def _reconcile_grid_after_transport_failure(payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """A grid dispatch's timeout is not a refusal: ask the durable row what actually happened (#3049).

    The runtime binds the search to the job before any cell runs, so a
    binding plus a held lease is durable evidence a live worker took the
    dispatch — this call joins the original acceptance, naming the search
    the runtime created. Anything less refuses with the transport error.
    """
    from app.research.persistence import lifecycle

    job_id = str(payload.get("job_id", ""))
    bound = await _grid_job_binding(job_id)
    if bound is not None and lifecycle.job_is_live(job_id) is True:
        logger.warning(
            "research runtime transport failed after a live worker took the grid dispatch; joining the original dispatch (%s)",
            exc,
            exc_info=True,
        )
        return {"job_id": job_id, "search_id": bound, "status": "queued"}
    logger.error("research runtime grid dispatch failed without a live worker on the job: %s", exc, exc_info=True)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"the research runtime did not accept the grid dispatch ({type(exc).__name__}); no search attempt started",
    )


async def forward_walk_forward_study_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand one walk-forward-study dispatch to the research runtime (#3050); reconcile on transport failure.

    Same contract as the grid hop: the runtime's refusals pass through, and
    a transport failure is reconciled against the durable study row before
    the API answers.
    """
    try:
        response = await _post_runtime_dispatch("/api/jobs-internal/walk-forward-study", payload)
    except httpx.HTTPError as exc:
        return await _reconcile_walk_forward_after_transport_failure(payload, exc)
    return _answer_of(response)


async def _walk_forward_job_binding(job_id: str) -> str | None:
    """The study id a job is bound to, if any — the durable evidence a study reconcile reads (#3050)."""

    from app.research.persistence.db import with_connection
    from app.research.walk_forward_study import repository as repo

    bound = await with_connection(repo.list_studies, job_id=job_id, limit=1)
    return bound[0].id if bound else None


async def _reconcile_walk_forward_after_transport_failure(payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """A study dispatch's timeout is not a refusal: ask the durable row what actually happened (#3050)."""
    from app.research.persistence import lifecycle

    job_id = str(payload.get("job_id", ""))
    bound = await _walk_forward_job_binding(job_id)
    if bound is not None and lifecycle.job_is_live(job_id) is True:
        logger.warning(
            "research runtime transport failed after a live worker took the study dispatch; joining the original dispatch (%s)",
            exc,
            exc_info=True,
        )
        return {"job_id": job_id, "study_id": bound, "status": "queued"}
    logger.error("research runtime study dispatch failed without a live worker on the job: %s", exc, exc_info=True)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"the research runtime did not accept the study dispatch ({type(exc).__name__}); no study attempt started",
    )


async def forward_recency_chart_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand one recency-chart dispatch to the research runtime (#3051); reconcile on transport failure.

    Same contract as the other hops: the runtime's refusals pass through,
    and a transport failure is reconciled against the durable launch row
    before the API answers.
    """
    try:
        response = await _post_runtime_dispatch("/api/jobs-internal/recency-chart", payload)
    except httpx.HTTPError as exc:
        return await _reconcile_recency_after_transport_failure(payload, exc)
    return _answer_of(response)


async def _recency_job_binding(payload: dict[str, Any]) -> str | None:
    """The launch id a job is bound to, if any — the durable evidence a recency reconcile reads (#3051).

    A fresh launch's id is its job id; a resume names the launch while the
    worker's claim binds the job. Either way the durable row plus a live
    lease is the evidence a dispatch reached a worker.
    """
    from app.research.recency import service as recency_service

    job_id = str(payload.get("job_id", ""))
    launch_id = str(payload.get("resume_launch_id") or job_id)
    launch = await recency_service.load_launch(launch_id)
    if launch is None:
        return None
    return launch.launch_id


async def _reconcile_recency_after_transport_failure(payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """A recency dispatch's timeout is not a refusal: ask the durable row what actually happened (#3051)."""
    from app.research.persistence import lifecycle

    job_id = str(payload.get("job_id", ""))
    bound = await _recency_job_binding(payload)
    if bound is not None and lifecycle.job_is_live(job_id) is True:
        logger.warning(
            "research runtime transport failed after a live worker took the recency dispatch; joining the original dispatch (%s)",
            exc,
            exc_info=True,
        )
        return {"job_id": job_id, "launch_id": bound, "status": "queued"}
    logger.error("research runtime recency dispatch failed without a live worker on the job: %s", exc, exc_info=True)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"the research runtime did not accept the recency dispatch ({type(exc).__name__}); no launch attempt started",
    )


def _detail_of(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


async def _reconcile_after_transport_failure(payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """A timeout is not a refusal: ask the durable state what actually happened.

    The runtime binds the job to the study before it starts any work, so the
    binding alone proves acceptance *began*, not that a worker took the job —
    the bind is written before the ownership stamp and the lease, and a
    runtime that died in that window would leave the study parked on a job
    nothing will run. Only a binding **plus** a held lease is durable
    evidence dispatch reached a live worker; then this call joins the
    original acceptance, the same answer the runtime's redelivery branch
    would give. Anything less refuses with the transport error, not silence.
    """
    from app.research.golden_search import service
    from app.research.persistence import lifecycle

    study_id = str(payload.get("study_id", ""))
    job_id = str(payload.get("job_id", ""))
    binding = await service.study_job_binding(study_id)
    if binding is not None and binding == job_id and lifecycle.job_is_live(job_id) is True:
        logger.warning(
            "research runtime transport failed after a live worker took the job; joining the original dispatch (%s)",
            exc,
            exc_info=True,
        )
        return {"job_id": job_id, "study_id": study_id, "status": "queued"}
    logger.error("research runtime dispatch failed without a live worker on the job: %s", exc, exc_info=True)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"the research runtime did not accept the dispatch ({type(exc).__name__}); no study attempt started",
    )
