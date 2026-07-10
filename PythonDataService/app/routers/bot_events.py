"""HTTP backfill surface for the authored bot-event stream."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse

from app.broker.ibkr.config import get_settings
from app.schemas.bot_events import BotEventPage, BotEventRow
from app.services.bot_event_stream_service import (
    BotEventStreamService,
    BotEventStreamUnavailableError,
    get_bot_event_stream_service,
)
from app.services.durable_event_channel import (
    DurableEventChannel,
    EventCursor,
    EventEnd,
    EventGap,
    EventRecord,
    EventReset,
    event_message_sse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bot-events"])

_BOT_EVENT_PROJECTION_ERROR = "bot-event stream history cannot be projected"
_BOT_EVENT_STREAM_ERROR = "bot-event stream unavailable"
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")


def _validate_run_id_literal(run_id: str) -> str:
    if not run_id or run_id != run_id.strip():
        raise ValueError("run_id must be non-empty with no surrounding whitespace")
    if run_id in (".", ".."):
        raise ValueError("run_id must not be a path segment ('.' or '..')")
    if "\x00" in run_id or "/" in run_id or "\\" in run_id:
        raise ValueError("run_id must not contain path separators or NUL bytes")
    if FsPath(run_id).is_absolute():
        raise ValueError("run_id must not be an absolute path")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(f"Invalid run_id format: {run_id!r}")
    return run_id


def _find_run_dir(root: FsPath, run_id: str) -> FsPath | None:
    safe = _validate_run_id_literal(run_id)
    try:
        root_resolved = root.resolve()
        for candidate in root_resolved.iterdir():
            if candidate.name == safe and candidate.is_dir():
                return candidate
    except OSError:
        return None
    return None


def _run_dir_for_http(run_id: str) -> FsPath:
    root = FsPath(get_settings().live_runs_root)
    try:
        run_dir = _find_run_dir(root, run_id)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}"
        ) from exc
    if run_dir is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")
    return run_dir


@router.get(
    "/{run_id}/bot-events",
    response_model=BotEventPage,
    summary="Paginated authored bot-event stream for a live run",
)
async def bot_event_backfill(
    run_id: Annotated[str, Path(min_length=1)],
    after_seq: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "Return authored rows with ``seq > after_seq``. To paginate, "
                "pass the previous response's ``next_seq`` verbatim."
            ),
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Max authored rows per page.",
        ),
    ] = 100,
    cursor: Annotated[str | None, Query()] = None,
    service: BotEventStreamService = Depends(get_bot_event_stream_service),
) -> BotEventPage:
    run_dir = _run_dir_for_http(run_id)
    try:
        channel = service.channel_for_run(run_dir)
        channel.refresh()
        parsed_cursor = _parse_event_cursor(cursor)
        if parsed_cursor is not None and parsed_cursor.stream_id != channel.stream_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "EVENT_STREAM_REPLACED",
                    "durable_stream_id": channel.stream_id,
                },
            )
        effective_after_seq = parsed_cursor.seq if parsed_cursor is not None else after_seq
        page = service.backfill_run(
            run_dir=run_dir,
            after_seq=effective_after_seq,
            limit=limit,
        )
    except BotEventStreamUnavailableError as exc:
        logger.warning(
            "Could not project bot-event stream",
            extra={"run_id": run_id, "run_dir": str(run_dir)},
            exc_info=True,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_BOT_EVENT_PROJECTION_ERROR,
        ) from exc
    high_water_seq = page.rows[-1].seq if page.rows else effective_after_seq
    return BotEventPage(
        rows=page.rows,
        next_seq=page.next_seq,
        durable_stream_id=channel.stream_id,
        high_water_cursor=EventCursor(channel.stream_id, high_water_seq).encode(),
        next_cursor=(
            EventCursor(channel.stream_id, page.next_seq).encode()
            if page.next_seq is not None
            else None
        ),
    )


def _error_sse(detail: str) -> str:
    return f"event: error\ndata: {json.dumps({'error': detail})}\n\n"


@router.get(
    "/{run_id}/bot-events/stream",
    summary="SSE stream of authored bot-event rows",
)
async def bot_event_stream(
    run_id: Annotated[str, Path(min_length=1)],
    since_seq: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "Replay authored rows with ``seq > since_seq``, then poll "
                "for live rows. Cold-start clients pass 0; reconnecting "
                "clients pass the highest seq they have."
            ),
        ),
    ] = 0,
    poll_ms: Annotated[
        int,
        Query(
            ge=250,
            le=60_000,
            deprecated=True,
            description=(
                "Legacy no-op retained for compatibility; the shared channel "
                "owns its WAL observation cadence."
            ),
        ),
    ] = 1000,
    cursor: Annotated[str | None, Query()] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    service: BotEventStreamService = Depends(get_bot_event_stream_service),
) -> StreamingResponse:
    run_dir = _run_dir_for_http(run_id)

    try:
        channel = service.channel_for_run(run_dir)
        channel.refresh()
    except BotEventStreamUnavailableError:
        async def projection_error_source() -> AsyncIterator[str]:
            yield _error_sse(_BOT_EVENT_PROJECTION_ERROR)

        return _streaming_response(projection_error_source())

    requested_cursor = _resolve_stream_cursor(
        channel=channel,
        query_cursor=cursor,
        last_event_id=last_event_id,
        legacy_since_seq=since_seq,
    )
    del poll_ms

    async def event_source() -> AsyncIterator[str]:
        subscription = channel.subscribe(requested_cursor)
        try:
            while True:
                message = await subscription.queue.get()
                yield event_message_sse(
                    message,
                    encode_row=lambda row: row.model_dump_json(),
                )
                if isinstance(message, EventRecord):
                    subscription.acknowledge(message.cursor)
                    continue
                if isinstance(message, EventReset) and subscription.active:
                    continue
                if isinstance(message, (EventGap, EventEnd, EventReset)):
                    return
        except asyncio.CancelledError:
            raise
        except BotEventStreamUnavailableError:
            logger.warning(
                "Could not stream bot-event rows",
                extra={"run_id": run_id, "run_dir": str(run_dir)},
                exc_info=True,
            )
            yield _error_sse(_BOT_EVENT_PROJECTION_ERROR)
        except Exception:
            logger.exception(
                "bot-event SSE stream error",
                extra={"run_id": run_id, "run_dir": str(run_dir)},
            )
            yield _error_sse(_BOT_EVENT_STREAM_ERROR)
        finally:
            channel.unsubscribe(subscription)

    return _streaming_response(event_source())


def _parse_event_cursor(value: str | None) -> EventCursor | None:
    try:
        return EventCursor.parse(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _resolve_stream_cursor(
    *,
    channel: DurableEventChannel[BotEventRow],
    query_cursor: str | None,
    last_event_id: str | None,
    legacy_since_seq: int,
) -> EventCursor:
    if query_cursor and last_event_id and query_cursor != last_event_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="cursor and Last-Event-ID must match when both are supplied",
        )
    parsed = _parse_event_cursor(query_cursor or last_event_id)
    if parsed is not None:
        return parsed
    return EventCursor(channel.stream_id, legacy_since_seq)


def _streaming_response(source: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        source,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
