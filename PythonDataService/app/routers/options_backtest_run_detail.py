"""Reading a saved Options Backtest run closely (#3030): its leg trades, one session for the timeline, an export of every
trade, and two runs side by side.

All reads; nothing here starts, changes or deletes a run. A run that has not completed has no trades or sessions: that
is a 409 and not an empty answer. The comparison route's path is ``/runs/compare``, which the run service's
``/runs/{run_id}`` also matches, so this router is mounted before that one (``main.py``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.options_backtest.run_detail import (
    LegNotInRun,
    RunNotComplete,
    RunNotFound,
    SessionNotInRun,
    read_trade_page,
)
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.schemas.options_backtest import OptionsBacktestRefusalBody
from app.schemas.options_backtest_run_detail import (
    TRADES_PAGE_DEFAULT,
    TRADES_PAGE_MAX,
    OptionsBacktestTradesResponse,
)
from app.schemas.options_backtest_runs import RUN_ID_MAX
from app.utils.session_anchors import MAX_TIMESTAMP_MS

router = APIRouter()

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": OptionsBacktestRefusalBody}}
_NOT_COMPLETE = {status.HTTP_409_CONFLICT: {"model": OptionsBacktestRefusalBody}}

RunId = Annotated[int, Path(ge=1, le=RUN_ID_MAX, description="The run's id, from the history.")]


def _refusal(exc: RunNotFound | RunNotComplete | SessionNotInRun | LegNotInRun) -> HTTPException:
    """The status and code each refusal of ``run_detail`` is answered with."""
    if isinstance(exc, RunNotFound):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "OPTIONS_BACKTEST_RUN_NOT_FOUND", "message": f"No run has the id {exc.run_id}."},
        )
    if isinstance(exc, RunNotComplete):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "OPTIONS_BACKTEST_RUN_NOT_COMPLETE",
                "message": f"Run {exc.run_id} is {exc.status}. Only a complete run has trades and sessions.",
            },
        )
    if isinstance(exc, SessionNotInRun):
        return HTTPException(
            status.HTTP_404_NOT_FOUND,
            {
                "code": "OPTIONS_BACKTEST_SESSION_NOT_IN_RUN",
                "message": f"Run {exc.run_id} did not test a session that opened at {exc.session_open_ms}.",
            },
        )
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        {"code": "OPTIONS_BACKTEST_LEG_NOT_IN_RUN", "message": f"Run {exc.run_id} has no leg {exc.leg_id!r}."},
    )


@router.get(
    "/runs/{run_id}/trades",
    response_model=OptionsBacktestTradesResponse,
    responses={**_NOT_FOUND, **_NOT_COMPLETE},
)
async def list_the_trades(
    run_id: RunId,
    session_open_ms: Annotated[
        int | None, Query(ge=0, le=MAX_TIMESTAMP_MS, description="Only this session's trades, by its open.")
    ] = None,
    leg_id: Annotated[str | None, Query(min_length=1, max_length=32, description="Only this leg's trades.")] = None,
    limit: Annotated[int, Query(ge=1, le=TRADES_PAGE_MAX)] = TRADES_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0, le=RUN_ID_MAX)] = 0,
) -> OptionsBacktestTradesResponse:
    """Every leg trade of a complete run, in order: session by session, and in each the order the run recorded them.

    Paged: ``total`` is every trade that matches, and ``next_offset`` is the ``offset`` that reads the rest (``null``
    on the last page). A session the run did not test, or a leg its setup does not have, is a 404 and not an empty page.
    A run that is not complete is a 409.
    """
    try:
        page = await with_connection(
            read_trade_page,
            run_id,
            is_live=lifecycle.job_is_live,
            session_open_ms=session_open_ms,
            leg_id=leg_id,
            limit=limit,
            offset=offset,
        )
    except (RunNotFound, RunNotComplete, SessionNotInRun, LegNotInRun) as exc:
        raise _refusal(exc) from exc
    return OptionsBacktestTradesResponse.from_page(page)
