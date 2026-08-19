"""Read-only host capability and diagnostic compatibility routes.

The IBKR bot lifecycle routes that formerly lived here retired under ADR 0038.
Canonical bot control is the Alpaca Broker V2 router. This compatibility module
keeps only the host/Clerk diagnostic evidence used by the IBKR market-data and
account recovery surfaces.
"""

from __future__ import annotations

import logging
from typing import Literal, NoReturn

from fastapi import APIRouter, HTTPException, status

from app.engine.live import host_daemon_client
from app.schemas.live_runs import HostRunnerHealth, MutationOutcomeUnknownResponse
from app.services.host_capability import (
    HostCapabilityPayloadError,
    HostCapabilityProbeError,
    get_host_capability_service,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["host-capability"])


@router.get("/daemon-health", response_model=HostRunnerHealth)
async def get_daemon_health() -> HostRunnerHealth:
    """Forward authenticated host/Clerk capability health."""

    try:
        return await get_host_capability_service().health()
    except HostCapabilityProbeError as exc:
        if exc.result.kind == "AUTH_FAILED":
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail="host capability daemon rejected the data plane's token",
            ) from exc
        if exc.result.kind == "UNREACHABLE":
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=exc.result.detail or "host capability daemon unreachable",
            ) from exc
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/daemon-health/renew-lease", response_model=HostRunnerHealth)
async def renew_daemon_lease() -> HostRunnerHealth:
    """Ask the host capability daemon to refresh its liveness lease."""

    try:
        return await get_host_capability_service().renew_lease()
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("renew_daemon_lease", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    except HostCapabilityPayloadError as exc:
        logger.warning("invalid renew-lease payload from host daemon: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _raise_outcome_unknown(
    endpoint: Literal["renew_daemon_lease"],
    exc: host_daemon_client.HostDaemonOutcomeUnknownError,
) -> NoReturn:
    body = MutationOutcomeUnknownResponse(
        error_category=exc.error_category,
        detail=exc.detail,
        endpoint=endpoint,
        occurred_at_ms=now_ms_utc(),
        runbook_hint=(
            "A host capability lease renewal was sent but its response was lost. "
            "Refresh diagnostics before deciding whether to retry."
        ),
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=body.model_dump(mode="json"),
    ) from exc
