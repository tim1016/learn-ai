"""Reading a saved Options Backtest run closely (#3030): its leg trades, one session for the timeline, an export of every
trade, and two runs side by side.

All reads; nothing here starts, changes or deletes a run. A run that has not completed has no trades or sessions: that
is a 409 and not an empty answer. The comparison route's path is ``/runs/compare``, which the run service's
``/runs/{run_id}`` also matches, so this router is mounted before that one (``main.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run_detail import (
    LegNotInRun,
    RunNotComplete,
    RunNotFound,
    SessionNotInRun,
    read_every_trade,
    read_session_record,
    read_trade_page,
)
from app.options_backtest.session_levels import SessionRecordInconsistent, fills_of, levels_of, marks_of
from app.options_backtest.session_series import read_session_series
from app.options_backtest.trade_export import csv_file_name, trades_csv
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.schemas.options_backtest import OptionsBacktestRefusalBody
from app.schemas.options_backtest_run_detail import (
    TRADES_PAGE_DEFAULT,
    TRADES_PAGE_MAX,
    OptionsBacktestSessionResponse,
    OptionsBacktestTradesResponse,
)
from app.schemas.options_backtest_runs import RUN_ID_MAX
from app.utils.session_anchors import MAX_TIMESTAMP_MS

logger = logging.getLogger(__name__)

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


@router.get(
    "/runs/{run_id}/sessions/{session_open_ms}",
    response_model=OptionsBacktestSessionResponse,
    responses={
        **_NOT_FOUND,
        **_NOT_COMPLETE,
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": OptionsBacktestRefusalBody},
    },
)
async def get_the_session(
    run_id: RunId,
    session_open_ms: Annotated[
        int, Path(ge=0, le=MAX_TIMESTAMP_MS, description="The session's open, as the run's trades and days give it.")
    ],
) -> OptionsBacktestSessionResponse:
    """One session of a complete run, for the one-day timeline.

    The stored facts, trades, events, levels, fills and marks come from the run's own rows. The minute closes of the
    underlying and of each contract the session traded or took a starting price in are read from the lake and shown only
    if the file is the one the run read (its sha256 is the stored pin); a series that cannot be shown says why and the rest
    is still served. A session the run did not test is a 404; a run that is not complete is a 409.
    """
    try:
        record = await with_connection(read_session_record, run_id, session_open_ms, is_live=lifecycle.job_is_live)
    except (RunNotFound, RunNotComplete, SessionNotInRun) as exc:
        raise _refusal(exc) from exc
    session = record.session.result
    setup = record.header.setup
    try:
        levels = levels_of(setup, session.outcome)
    except SessionRecordInconsistent as exc:
        logger.error("a stored session does not agree with itself", extra={"run_id": run_id, "reason": str(exc)})
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            {
                "code": "OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT",
                "message": f"The stored trades and events of this session do not agree, so it cannot be drawn: {exc}.",
            },
        ) from exc
    # The lake's readers block on files and on the catalog.
    series = await asyncio.to_thread(
        read_session_series, session, underlying=setup.underlying, data=LakeRunData(setup.underlying)
    )
    return OptionsBacktestSessionResponse.from_parts(
        record,
        series=series,
        levels=levels,
        fills=fills_of(session.outcome.trades),
        marks=marks_of(session.outcome.trades),
    )


@router.get(
    "/runs/{run_id}/trades.csv",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "description": "Every leg trade of the run as a CSV download, named for the run. Never paged or truncated.",
            "content": {"text/csv": {"schema": {"type": "string"}}},
        },
        **_NOT_FOUND,
        **_NOT_COMPLETE,
    },
)
async def export_the_trades(run_id: RunId) -> Response:
    """Every leg trade of a complete run as a CSV: ``unix_ts`` (the entry fill's bar close, ms UTC) first, one readable
    New York time beside it, every other instant in a ``*_ms`` column, money and prices as exact decimal text.

    The whole run, however many trades it has: the page size of the trades route does not apply. A run that is not
    complete is a 409.
    """
    try:
        trades = await with_connection(read_every_trade, run_id, is_live=lifecycle.job_is_live)
    except (RunNotFound, RunNotComplete) as exc:
        raise _refusal(exc) from exc
    body = await asyncio.to_thread(trades_csv, trades)
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{csv_file_name(run_id)}"'},
    )
