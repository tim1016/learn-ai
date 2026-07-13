"""Auto-dispatch of the LEAN validating companion for Python engine runs.

Every persisted Python engine run gets a parity disposition:

- Eligible runs (registered ``lean_twin``, raw bars, minute resolution,
  explicit window) spawn an async LEAN run through the public jobs
  surface, sharing a ``parity_group_id``. A ``pending`` ParityVerdict
  row is created immediately; the .NET persist step freezes it to
  ``agree``/``diverged`` when the LEAN row lands.
- Ineligible runs get an honest ``unavailable`` verdict row carrying the
  reason — never a fake pass, never silence.

Failure surfacing: the LEAN job worker calls :func:`mark_parity_failed`
when the companion run fails or its persistence returns no row id,
transitioning ``pending → run_failed | persist_failed``. The .NET
endpoint makes that transition conditional, so a verdict that already
froze is never overwritten (first terminal state wins).

Every HTTP call here is best-effort with a short timeout: parity
bookkeeping must never fail or slow the Python run that triggered it.
A lost ``pending`` row degrades to "no parity info" on the report.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx

from app.config import settings

if TYPE_CHECKING:
    from app.engine.strategy.registry import StrategyRegistration
    from app.routers.engine import EngineBacktestRequest

logger = logging.getLogger(__name__)

PARITY_VERDICT_SCHEMA_VERSION = 1
_HTTP_TIMEOUT_S = 5.0

# Reasons for the honest ``unavailable`` disposition.
REASON_NO_TWIN = "no_lean_counterpart"
REASON_ADJUSTMENT = "adjustment_unsupported"
REASON_RESOLUTION = "resolution_unsupported"
REASON_WINDOW = "window_unsupported"


def new_parity_group_id() -> str:
    """Mint a group id (also embedded in the companion's run_id slug)."""
    return f"pg-{uuid.uuid4().hex[:20]}"


def companion_ineligibility_reason(
    registration: StrategyRegistration,
    request: EngineBacktestRequest,
) -> str | None:
    """Why this run cannot have a LEAN companion, or None when it can.

    The LEAN runtime consumes raw bars only (``_runtime_polygon_adjustment``
    pins ``"raw"``), so adjusted-policy runs are honestly unavailable until
    a pre-adjusted staging pipeline exists.
    """
    if registration.lean_twin is None:
        return REASON_NO_TWIN
    if request.data_policy is None or request.data_policy.adjusted:
        return REASON_ADJUSTMENT
    if request.resolution != "minute":
        return REASON_RESOLUTION
    if not request.from_date or not request.to_date:
        return REASON_WINDOW
    return None


def dispatch_parity_companion(
    *,
    registration: StrategyRegistration,
    request: EngineBacktestRequest,
    parity_group_id: str,
    left_execution_id: int,
) -> None:
    """Record the parity disposition and, when eligible, launch the companion.

    Called after the Python run persisted successfully. Never raises.
    """
    reason = companion_ineligibility_reason(registration, request)
    if reason is not None:
        _create_verdict_row(
            parity_group_id=parity_group_id,
            left_execution_id=left_execution_id,
            status="unavailable",
            verdict={
                "schema_version": PARITY_VERDICT_SCHEMA_VERSION,
                "parity_group_id": parity_group_id,
                "status": "unavailable",
                "reason": reason,
            },
        )
        return

    _create_verdict_row(
        parity_group_id=parity_group_id,
        left_execution_id=left_execution_id,
        status="pending",
        verdict={
            "schema_version": PARITY_VERDICT_SCHEMA_VERSION,
            "parity_group_id": parity_group_id,
            "status": "pending",
            "reason": None,
        },
    )
    _launch_companion_job(registration=registration, request=request, parity_group_id=parity_group_id)


def mark_parity_failed(parity_group_id: str, *, status: str, detail: str) -> None:
    """Transition the group's verdict ``pending → run_failed|persist_failed``.

    No-ops (server-side) when the verdict already reached a terminal
    state — first terminal state wins. Never raises.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            response = client.post(
                f"{settings.BACKEND_URL}/api/parity-verdicts/{parity_group_id}/mark-failed",
                json={"status": status, "detail": detail},
            )
        if response.status_code >= 300:
            logger.warning(
                "[PARITY] mark-failed rejected for %s: %s %s",
                parity_group_id,
                response.status_code,
                response.text[:200],
            )
    except httpx.HTTPError:
        logger.exception("[PARITY] mark-failed request failed for %s", parity_group_id)


def _create_verdict_row(
    *,
    parity_group_id: str,
    left_execution_id: int,
    status: str,
    verdict: dict[str, Any],
) -> None:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            response = client.post(
                f"{settings.BACKEND_URL}/api/parity-verdicts",
                json={
                    "parityGroupId": parity_group_id,
                    "leftExecutionId": left_execution_id,
                    "status": status,
                    "verdictJson": json.dumps(verdict),
                },
            )
        if response.status_code >= 300:
            logger.warning(
                "[PARITY] verdict-row create rejected for %s: %s %s",
                parity_group_id,
                response.status_code,
                response.text[:200],
            )
    except httpx.HTTPError:
        logger.exception("[PARITY] verdict-row create failed for %s", parity_group_id)


def _launch_companion_job(
    *,
    registration: StrategyRegistration,
    request: EngineBacktestRequest,
    parity_group_id: str,
) -> None:
    """POST the companion run through the public jobs surface.

    Going through ``/api/jobs/lean_engine_run`` (not the internal route)
    lets .NET mint the job id and record the Redis state, so the run
    dock and history auto-refresh see the companion like any other run.
    """
    from app.lean_sidecar.trading_calendar import (
        NoSessionError,
        is_trading_day,
        next_trading_day,
        session_open_ms_utc,
    )

    assert request.data_policy is not None  # checked by eligibility
    try:
        start_day = date.fromisoformat(request.from_date or "")
        end_day = date.fromisoformat(request.to_date or "")
        if not is_trading_day(start_day):
            start_day = next_trading_day(start_day)
        # The sidecar window is half-open [start, end) anchored at session
        # opens (P2.5 contract) — the exclusive end is the next session
        # open after the last requested trading day.
        start_ms = session_open_ms_utc(start_day)
        end_ms = session_open_ms_utc(next_trading_day(end_day))
    except (ValueError, NoSessionError) as exc:
        logger.warning("[PARITY] companion window rejected for %s: %s", parity_group_id, exc)
        mark_parity_failed(parity_group_id, status="run_failed", detail=f"companion window invalid: {exc}")
        return

    body = {
        "request": {
            "run_id": f"companion-{parity_group_id}",
            "start_ms_utc": start_ms,
            "end_ms_utc": end_ms,
            "starting_cash": request.initial_cash if request.initial_cash is not None else 100_000.0,
            "template": registration.lean_twin,
            "data_policy": request.data_policy.model_dump(),
            "parity_group_id": parity_group_id,
        }
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            response = client.post(f"{settings.BACKEND_URL}/api/jobs/lean_engine_run", json=body)
        if response.status_code >= 300:
            logger.warning(
                "[PARITY] companion dispatch rejected for %s: %s %s",
                parity_group_id,
                response.status_code,
                response.text[:200],
            )
            mark_parity_failed(
                parity_group_id,
                status="run_failed",
                detail=f"companion dispatch rejected: HTTP {response.status_code}",
            )
    except httpx.HTTPError as exc:
        logger.exception("[PARITY] companion dispatch failed for %s", parity_group_id)
        mark_parity_failed(parity_group_id, status="run_failed", detail=f"companion dispatch failed: {exc}")
