"""FastAPI router for the data-divergence research dashboard.

Endpoints
---------

``GET  /research/data-divergence``
    302 redirect to the prebuilt HTML dashboard.

``GET  /research/data-divergence/dashboard.html``
    Serves the cached HTML file directly. Static; zero latency.

``POST /research/data-divergence/rebuild``
    Rebuilds the dashboard from the latest ``cache/divergence/{tf}/`` data.
    Returns JSON with file paths and sizes. Intended for dev loops and
    scheduled cron jobs, not interactive UI.

``GET  /research/data-divergence/matrix/{tf}``
    JSON payload of the divergence matrix CSV for ``tf``. Useful for
    Angular-side rendering that wants to build its own charts.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.research.divergence.dashboard import build_dashboard
from app.research.divergence.preflight import (
    IndicatorRequest,
    PreflightRequest,
    run_preflight,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/data-divergence", tags=["research"])

CACHE_ROOT = Path("cache/divergence")
DEFAULT_TF: Literal["5m", "15m", "1h"] = "15m"


@router.get("", include_in_schema=False)
def _root_redirect() -> RedirectResponse:
    """Redirect bare endpoint to the dashboard HTML for the default timeframe."""
    return RedirectResponse(url=f"/research/data-divergence/dashboard.html?tf={DEFAULT_TF}")


@router.get("/dashboard.html")
def serve_dashboard(tf: str = DEFAULT_TF) -> FileResponse:
    """Serve the prebuilt dashboard HTML for a given timeframe."""
    if tf not in ("5m", "15m", "1h"):
        raise HTTPException(status_code=400, detail=f"Unknown timeframe {tf!r}")
    path = CACHE_ROOT / tf / f"dashboard_{tf}.html"
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Dashboard for {tf} not built yet. Run "
                "`python -m app.research.divergence.cli all ...` then "
                "POST /research/data-divergence/rebuild."
            ),
        )
    return FileResponse(path, media_type="text/html")


@router.post("/rebuild")
def rebuild_dashboard(tf: str = DEFAULT_TF) -> JSONResponse:
    """Rebuild the dashboard from current cache contents."""
    if tf not in ("5m", "15m", "1h"):
        raise HTTPException(status_code=400, detail=f"Unknown timeframe {tf!r}")
    try:
        out_path = build_dashboard(timeframe=tf)
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cache not populated for {tf}: {e}",
        )
    size_mb = out_path.stat().st_size / 1e6
    return JSONResponse({"path": str(out_path), "size_mb": round(size_mb, 2), "timeframe": tf})


@router.get("/matrix/{tf}")
def get_matrix(tf: str) -> JSONResponse:
    """Return the divergence matrix for ``tf`` as JSON."""
    if tf not in ("5m", "15m", "1h"):
        raise HTTPException(status_code=400, detail=f"Unknown timeframe {tf!r}")
    path = CACHE_ROOT / tf / f"matrix_{tf}.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Matrix for {tf} not computed")
    df = pd.read_csv(path)
    return JSONResponse({"timeframe": tf, "rows": df.to_dict(orient="records")})


# ----------------------------------------------------------------------
# Pre-flight checks (Surface 3)
# ----------------------------------------------------------------------


class _PreflightIndicator(BaseModel):
    name: str = Field(..., examples=["ema", "rsi", "macd"])
    length: int = Field(..., ge=1, examples=[5, 14, 200])
    extras: dict = Field(default_factory=dict)


class _PreflightRequestBody(BaseModel):
    strategy_name: str
    symbol: str
    start_date: date
    end_date: date
    timeframe: Literal["5m", "15m", "1h"]
    indicators: list[_PreflightIndicator]
    session_filter: Literal["rth_only", "full_session", "unspecified"] = "unspecified"
    warmup_days: int = Field(default=0, ge=0)
    dividend_adjustment: bool = False


@router.post("/preflight")
def preflight(body: _PreflightRequestBody) -> JSONResponse:
    """Run pre-flight checks for a proposed backtest run.

    Returns structured JSON with one entry per check (status: ok / warning /
    blocking) plus an overall verdict. Caller decides whether to block or
    warn based on the overall status. See ``app.research.divergence.preflight``
    for the per-check logic.
    """
    req = PreflightRequest(
        strategy_name=body.strategy_name,
        symbol=body.symbol,
        start_date=body.start_date,
        end_date=body.end_date,
        timeframe=body.timeframe,
        indicators=[IndicatorRequest(name=i.name, length=i.length, extras=i.extras) for i in body.indicators],
        session_filter=body.session_filter,
        warmup_days=body.warmup_days,
        dividend_adjustment=body.dividend_adjustment,
    )
    result = run_preflight(req)
    return JSONResponse(
        {
            "overall": result.overall,
            "summary": result.summary,
            "checks": [asdict(c) for c in result.checks],
        }
    )
