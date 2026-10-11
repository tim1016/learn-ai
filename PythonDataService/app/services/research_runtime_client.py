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


def _require_runtime_url() -> str:
    url = settings.RESEARCH_RUNTIME_URL.strip().rstrip("/")
    if not url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RESEARCH_EXECUTION_MODE=research requires RESEARCH_RUNTIME_URL; no research dispatch can run",
        )
    from app.broker.fleet.internal_http import FleetTransportRefused

    try:
        enforce_private_http_target(url)
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

    base = _require_runtime_url()
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    try:
        async with build_internal_client(timeout_s=FORWARD_TIMEOUT_S) as client:
            response = await client.post(
                f"{base}/api/jobs-internal/golden-search",
                json=payload,
                headers={CONTROL_SECRET_HEADER: secret},
            )
    except httpx.HTTPError as exc:
        return await _reconcile_after_transport_failure(payload, exc)
    if response.status_code == status.HTTP_202_ACCEPTED:
        return response.json()
    detail = _detail_of(response)
    raise HTTPException(status_code=response.status_code, detail=detail)


def _detail_of(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text


async def _reconcile_after_transport_failure(payload: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """A timeout is not a refusal: ask the durable binding what actually happened.

    The runtime binds the job to the study before it starts any work, so the
    study row's ``job_id`` is the truth of acceptance. Bound to this job id
    means the original acceptance stands and this call joins it — the same
    answer the runtime's redelivery branch would give; anything else means
    nothing started, and the operator gets the transport error, not silence.
    """
    from app.research.golden_search import service

    study_id = str(payload.get("study_id", ""))
    job_id = str(payload.get("job_id", ""))
    binding = await service.study_job_binding(study_id)
    if binding is not None and binding == job_id:
        logger.warning(
            "research runtime transport failed after acceptance; joining the original dispatch (%s)",
            exc,
            exc_info=True,
        )
        return {"job_id": job_id, "study_id": study_id, "status": "queued"}
    logger.error("research runtime dispatch failed without a durable binding: %s", exc, exc_info=True)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"the research runtime did not accept the dispatch ({type(exc).__name__}); no study attempt started",
    )
