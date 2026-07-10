"""Shared HTTP-facing SSE loop for durable event channels."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import HTTPException, status

from app.services.durable_event_channel import (
    DurableEventChannel,
    EventCursor,
    EventEnd,
    EventGap,
    EventRecord,
    EventReset,
    event_message_sse,
)

RowT = TypeVar("RowT")


@dataclass(frozen=True)
class DurableEventStreamCursor:
    requested_cursor: EventCursor
    use_legacy_backfill: bool
    legacy_after_seq: int


def parse_event_cursor(value: str | None) -> EventCursor | None:
    try:
        return EventCursor.parse(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def resolve_stream_cursor(  # noqa: UP047 - Python 3.11 runtime.
    *,
    channel: DurableEventChannel[RowT],
    query_cursor: str | None,
    last_event_id: str | None,
    legacy_since_seq: int,
    legacy_since_seq_provided: bool,
) -> DurableEventStreamCursor:
    parsed = parse_event_cursor(last_event_id or query_cursor)
    requested_cursor = parsed or EventCursor(channel.stream_id, legacy_since_seq)
    return DurableEventStreamCursor(
        requested_cursor=requested_cursor,
        use_legacy_backfill=query_cursor is None
        and (last_event_id is None or legacy_since_seq_provided),
        legacy_after_seq=requested_cursor.seq
        if last_event_id is not None
        else legacy_since_seq,
    )


async def stream_durable_event_channel(  # noqa: UP047 - Python 3.11 runtime.
    *,
    channel: DurableEventChannel[RowT],
    cursor: DurableEventStreamCursor,
    encode_row: Callable[[RowT], str],
    handle_error: Callable[[BaseException], str],
    projection_error_types: tuple[type[BaseException], ...] = (),
    handle_projection_error: Callable[[BaseException], str] | None = None,
    on_close: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> AsyncIterator[str]:
    records: list[EventRecord[RowT]] = []
    if cursor.use_legacy_backfill:
        records, subscription = channel.subscribe_with_backfill(cursor.legacy_after_seq)
    else:
        subscription = channel.subscribe(cursor.requested_cursor)
    try:
        for record in records:
            yield event_message_sse(record, encode_row=encode_row)
            subscription.acknowledge(record.cursor)
        while True:
            message = await subscription.queue.get()
            yield event_message_sse(message, encode_row=encode_row)
            if isinstance(message, EventRecord):
                subscription.acknowledge(message.cursor)
                continue
            if isinstance(message, EventReset) and subscription.active:
                continue
            if isinstance(message, (EventGap, EventEnd, EventReset)):
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if (
            projection_error_types
            and isinstance(exc, projection_error_types)
            and handle_projection_error is not None
        ):
            yield handle_projection_error(exc)
        else:
            yield handle_error(exc)
    finally:
        channel.unsubscribe(subscription)
        if on_close is not None:
            await on_close()
