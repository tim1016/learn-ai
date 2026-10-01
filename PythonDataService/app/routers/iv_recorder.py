"""IV recorder router — POST endpoint that .NET cron calls per slot.

The .NET host's Quartz scheduler
(``AddIvRecorder`` in ``Backend/Jobs/IvRecorderRegistration.cs``)
registers one cron trigger per configured slot per ticker (default
09:35 / 12:30 / 15:55 / 16:00 ET — 15:55 runs alongside 16:00 for the
trial-month experiment in ADR 0071 decision 7) and calls this
endpoint at each slot time. The endpoint is idempotent on
``(ticker, snapshot_ts_ms)``: a duplicate fire writes a duplicate row,
which is acceptable for the forward-only history use case (the read
side de-dupes on ``snapshot_ts_ms``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.iv_recorder import (
    SLOT_CHOICES,
    get_iv_store,
    record_iv_snapshot,
)
from app.services.polygon_client import PolygonClientService

router = APIRouter(prefix="/api/iv-recorder", tags=["iv-recorder"])
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


# ── Models ──────────────────────────────────────────────────────────────────


class SnapshotRequest(BaseModel):
    ticker: str = Field(..., min_length=1)
    slot: str = Field(..., description=f"One of {SLOT_CHOICES}")
    target_calendar_days: int = Field(30, ge=1, le=180)


class RecordedSnapshotItem(BaseModel):
    ticker: str
    snapshot_ts_ms: int
    slot: str
    spot: float
    rate: float
    dividend_yield: float
    rate_source: str
    dividend_source: str
    iv30_vix_style: float | None
    iv30_parametric: float | None
    iv_provenance: dict
    error: str | None = None
    health_score: float | None = None


class SnapshotResponse(BaseModel):
    success: bool
    snapshot: RecordedSnapshotItem


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post("/snapshot", response_model=SnapshotResponse)
async def take_snapshot(req: SnapshotRequest) -> SnapshotResponse:
    """Capture a single slot for a single ticker.

    Always returns 200 — the recorder writes an error-tagged row when
    Polygon or the solver fails, so the caller (cron job) doesn't need
    retry logic for upstream failures. 4xx is reserved for input
    validation problems (unknown slot, missing ticker).
    """
    if req.slot not in SLOT_CHOICES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"slot must be one of {SLOT_CHOICES}, got {req.slot!r}",
        )
    row = record_iv_snapshot(
        ticker=req.ticker,
        slot=req.slot,
        store=get_iv_store(),
        polygon=polygon_client,
        target_calendar_days=req.target_calendar_days,
    )
    return SnapshotResponse(
        success=row.error is None,
        snapshot=RecordedSnapshotItem(
            ticker=row.ticker,
            snapshot_ts_ms=row.snapshot_ts_ms,
            slot=row.slot,
            spot=row.spot,
            rate=row.rate,
            dividend_yield=row.dividend_yield,
            rate_source=row.rate_source,
            dividend_source=row.dividend_source,
            iv30_vix_style=row.iv30_vix_style,
            iv30_parametric=row.iv30_parametric,
            iv_provenance=row.iv_provenance,
            error=row.error,
            health_score=row.health_score,
        ),
    )
