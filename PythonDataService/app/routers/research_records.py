"""HTTP behaviour the fenced research records share (Grid Search, Walk-Forward Study).

Not a router: helpers the two routers compose. Refusals become a 400 with a
``{code, message}`` detail; job liveness is asked of Redis only when it can
change what a row reads back as; deletion of a live record first cancels and
waits for the worker's acknowledgement; and a filter on a live-derived status
(``queued`` / ``running`` / ``interrupted``) is applied to the *presented*
status, after the stored live rows are read and presented.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from fastapi import HTTPException, status

from app.research.grid_search.service import GridSearchRefusal
from app.research.persistence import lifecycle
from app.research.persistence.lifecycle import FencedRecord

CANCEL_ACK_TIMEOUT_SECONDS = 30.0
STORED_LIVE_STATUSES: tuple[str, ...] = ("queued", "running")
LIVE_DERIVED_STATUSES: frozenset[str] = frozenset({*STORED_LIVE_STATUSES, "interrupted"})
# Live rows are few (a handful of concurrent records); scan them all before presenting.
LIVE_SCAN_LIMIT = 1000


class Presented(Protocol):
    @property
    def status(self) -> str: ...


def refused(exc: GridSearchRefusal) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": str(exc)})


def liveness(row: FencedRecord) -> bool | None:
    """Ask Redis only when the answer can change what the row reads back as."""
    return lifecycle.job_is_live(row.job_id) if row.status in STORED_LIVE_STATUSES else False


def liveness_or_503(row: FencedRecord, *, noun: str) -> bool:
    """For actions that must not proceed on an unknown answer (delete)."""
    live = liveness(row)
    if live is None:
        raise _job_store_unreachable(noun)
    return live


async def refuse_unauthorized_dispatch() -> None:
    """Refuse a research dispatch before any durable mutation happens (#3042).

    A stamp that refuses after the job is bound strands the binding on a job
    no worker will serve; this check runs first, so the common refusals —
    the lock held elsewhere, a dead authority connection — never bind at
    all. It cannot prove authority survives until the stamp (the stamp on
    the lock-owning session answers that); it narrows the window.
    """
    from app.research.persistence import ownership

    if ownership.dispatch_refused():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="research dispatch refused: another coordinator holds the singleton authority",
        )
    authority = ownership.process_authority()
    if authority is not None and not authority.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="research dispatch refused: this coordinator's authority connection was lost",
        )


async def stamp_research_dispatch(*, job_id: str, kind: str, record_id: str) -> None:
    """Record one research dispatch under the process's runtime authority (#3042).

    503 when a live coordinator elsewhere — or this process's dead authority
    connection — outranks the dispatch; a no-op under the legacy lifecycle
    (no authority claimed, no ownership rows, jobs swept as API-owned).
    """
    from fastapi import HTTPException

    from app.research.persistence import ownership

    try:
        await ownership.stamp_dispatch(job_id=job_id, kind=kind, record_id=record_id)
    except ownership.CoordinatorHeldElsewhere as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


def require_live_redelivery(job_id: str, *, noun: str) -> None:
    """A redelivered dispatch is acknowledged only while the first worker still holds the job.

    A job that is no longer live cannot be redelivered under its id (409): replaying a closed
    job through the same transport record is not a resume (its event stream, result key,
    cancel flag and run accounting all still describe the first execution). An unknown answer
    is a 503, as for delete.
    """
    live = lifecycle.job_is_live(job_id)
    if live is None:
        raise _job_store_unreachable(noun)
    if not live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"the {noun}'s job is no longer running; it cannot be redelivered under this job id",
        )


def _job_store_unreachable(noun: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"whether the {noun} is still running cannot be established (job store unreachable); try again shortly",
    )


def stored_status_query(status_filter: str | None, limit: int) -> tuple[Sequence[str] | None, int]:
    """Which stored statuses to read, and how many, for a requested (possibly live-derived) status filter."""
    if status_filter is None:
        return None, limit
    if status_filter in LIVE_DERIVED_STATUSES:
        return STORED_LIVE_STATUSES, LIVE_SCAN_LIMIT
    return (status_filter,), limit


def cut_to_presented[T: Presented](summaries: list[T], status_filter: str | None, limit: int) -> list[T]:
    if status_filter in LIVE_DERIVED_STATUSES:
        return [summary for summary in summaries if summary.status == status_filter][:limit]
    return summaries


async def cancel_and_await_ack(job_id: str, current_status: Callable[[], Awaitable[str | None]], *, noun: str) -> None:
    """Request cancellation and wait until the record leaves its live status (or is gone); 409 if it does not."""
    lifecycle.request_cancel(job_id)
    deadline = asyncio.get_running_loop().time() + CANCEL_ACK_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        current = await current_status()
        if current is None or current not in STORED_LIVE_STATUSES:
            return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"the running {noun} has not acknowledged cancellation yet; try again shortly",
    )
