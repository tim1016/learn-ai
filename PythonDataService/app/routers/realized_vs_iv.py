"""Realized vs IV: the window a run reads when its request names none (#3525).

The study itself is a job (``POST /api/jobs-internal/realized-vs-iv``). This route only answers, from the canonical
calendar, what that job would read now, so a page can show the window and count what the lake lacks over exactly that
span before the person runs anything. It reads no bar and calls no vendor.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.research.lake_study import SESSION_POLICY_FOR_REQUEST
from app.research.study_inputs import SessionPolicy, StudyInputRefusal
from app.schemas.realized_vs_iv import RealizedVsIvWindow
from app.services.realized_vs_iv_study_service import BarSizeLabel, default_window
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc

router = APIRouter(prefix="/api/edge/realized-vs-iv", tags=["edge-realized-vs-iv"])


def _request_session(policy: SessionPolicy) -> Literal["rth", "extended"]:
    """How every ticker request spells the session a policy names (the inverse of ``SESSION_POLICY_FOR_REQUEST``)."""
    return "rth" if policy == "regular" else "extended"


@router.get("/window", response_model=RealizedVsIvWindow)
async def get_default_window(
    bar_size: BarSizeLabel = Query(..., description="The study's bar size."),
    session: Literal["rth", "extended"] | None = Query(
        None, description="The session the run reads; omitted, it is the session the bar size reads by default."
    ),
) -> RealizedVsIvWindow:
    """The window a run of ``bar_size`` reads when it names none, and the session it reads it in.

    A bar size and session the study does not offer (extended-hours daily bars) is a 422 whose detail carries the
    refusal's ``code`` and ``reason``, the answer a run of the same inputs would give.
    """
    try:
        window = await asyncio.to_thread(
            default_window, bar_size, SESSION_POLICY_FOR_REQUEST[session] if session is not None else None, now_ms_utc()
        )
    except StudyInputRefusal as refusal:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": refusal.code, "reason": refusal.reason}
        ) from refusal
    return RealizedVsIvWindow(
        bar_size=window.bar_size,
        session=_request_session(window.session_policy),
        window_start_ms=et_midnight_ms(window.start),
        window_end_ms=et_midnight_ms(window.end),
        sessions=window.sessions,
    )
