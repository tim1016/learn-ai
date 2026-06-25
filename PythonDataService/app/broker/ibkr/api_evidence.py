"""In-process IBKR API evidence stream.

This is a cockpit diagnostics surface, not an engine input. Broker adapters
publish the exact IBKR request envelope plus raw response/callback object
snapshots here so the operator can inspect what TWS/Gateway sent us before we
curate it into engine-facing models.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.broker.ibkr.models import (
    IbkrApiCallbackName,
    IbkrApiRequestEvidence,
    IbkrApiRequestName,
    IbkrApiResponseEvidence,
)
from app.broker.ibkr.order_evidence import snapshot_ibkr_object
from app.utils.timestamps import now_ms_utc

_MAX_EVENTS = 1_000
_SUBSCRIBER_QUEUE_SIZE = 256


class IbkrApiEvidenceEvent(BaseModel):
    """One observed IBKR API request/response pair."""

    model_config = ConfigDict(frozen=True)

    seq: int = Field(ge=1)
    ts_ms: int
    source: str
    account_id: str | None = None
    symbol: str | None = None
    strategy_instance_id: str | None = None
    request: IbkrApiRequestEvidence
    response: IbkrApiResponseEvidence | None = None
    error: str | None = None


@dataclass(frozen=True)
class IbkrApiEvidenceSubscription:
    queue: asyncio.Queue[IbkrApiEvidenceEvent | None]


class IbkrApiEvidenceRecorder:
    def __init__(self) -> None:
        self._seq = 0
        self._events: deque[IbkrApiEvidenceEvent] = deque(maxlen=_MAX_EVENTS)
        self._subscribers: set[asyncio.Queue[IbkrApiEvidenceEvent | None]] = set()

    def record(
        self,
        *,
        source: str,
        request: IbkrApiRequestEvidence,
        response: IbkrApiResponseEvidence | None = None,
        error: str | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
        strategy_instance_id: str | None = None,
    ) -> IbkrApiEvidenceEvent:
        self._seq += 1
        event = IbkrApiEvidenceEvent(
            seq=self._seq,
            ts_ms=now_ms_utc(),
            source=source,
            account_id=account_id,
            symbol=symbol,
            strategy_instance_id=strategy_instance_id,
            request=request,
            response=response,
            error=error,
        )
        self._events.append(event)
        self._broadcast(event)
        return event

    def backfill(self, *, after_seq: int = 0, limit: int = 250) -> list[IbkrApiEvidenceEvent]:
        return [event for event in self._events if event.seq > after_seq][:limit]

    def subscribe(self) -> IbkrApiEvidenceSubscription:
        queue: asyncio.Queue[IbkrApiEvidenceEvent | None] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        self._subscribers.add(queue)
        return IbkrApiEvidenceSubscription(queue=queue)

    def unsubscribe(self, subscription: IbkrApiEvidenceSubscription) -> None:
        self._subscribers.discard(subscription.queue)
        with suppress(asyncio.QueueFull):
            subscription.queue.put_nowait(None)

    def _broadcast(self, event: IbkrApiEvidenceEvent) -> None:
        dead: list[asyncio.Queue[IbkrApiEvidenceEvent | None]] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)


_RECORDER = IbkrApiEvidenceRecorder()


def get_ibkr_api_evidence_recorder() -> IbkrApiEvidenceRecorder:
    return _RECORDER


def evidence_request(call: IbkrApiRequestName, **params: JsonValue) -> IbkrApiRequestEvidence:
    return IbkrApiRequestEvidence(call=call, params=dict(params))


def evidence_response(
    callback: IbkrApiCallbackName,
    *,
    fields: dict[str, JsonValue] | None = None,
    objects: Iterable[object] = (),
) -> IbkrApiResponseEvidence:
    out: dict[str, JsonValue] = dict(fields or {})
    for index, obj in enumerate(objects):
        snapshot = snapshot_ibkr_object(obj)
        out[f"object_{index}"] = snapshot.model_dump(mode="json") if snapshot else {}
    return IbkrApiResponseEvidence(callback=callback, fields=out)
