"""Realized vs IV: the window a run reads when its request names none (#3525).

The study itself is a job (``POST /api/jobs-internal/realized-vs-iv``). This route only answers, from the canonical
calendar, what that job would read now, so a page can show the window and count what the lake lacks over exactly that
span before the person runs anything. It reads no bar and calls no vendor.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Query

from app.research.lake_study import SESSION_POLICY_FOR_REQUEST
from app.schemas.realized_vs_iv import RealizedVsIvWindow
from app.services.realized_vs_iv_study_service import default_window
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc

router = APIRouter(prefix="/api/edge/realized-vs-iv", tags=["edge-realized-vs-iv"])

_REQUEST_SESSION = {policy: spelling for spelling, policy in SESSION_POLICY_FOR_REQUEST.items()}


@router.get("/window", response_model=RealizedVsIvWindow)
async def get_default_window(
    bar_size: Literal["15m", "1d"] = Query(..., description="The study's bar size."),
    session: Literal["rth", "extended"] | None = Query(
        None, description="The session the run reads; omitted, it is the session the bar size reads by default."
    ),
) -> RealizedVsIvWindow:
    """The window a run of ``bar_size`` reads when it names none, and the session it reads it in."""
    window = await asyncio.to_thread(
        default_window, bar_size, SESSION_POLICY_FOR_REQUEST[session] if session is not None else None, now_ms_utc()
    )
    return RealizedVsIvWindow(
        bar_size=window.bar_size,  # type: ignore[arg-type]
        session=_REQUEST_SESSION[window.session_policy],  # type: ignore[arg-type]
        window_start_ms=et_midnight_ms(window.start),
        window_end_ms=et_midnight_ms(window.end),
        sessions=window.sessions,
    )
