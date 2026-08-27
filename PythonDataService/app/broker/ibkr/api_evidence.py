"""In-process IBKR API evidence stream.

This is a cockpit diagnostics surface, not an engine input. Broker adapters
publish the exact IBKR request envelope plus raw response/callback object
snapshots here so the operator can inspect what TWS/Gateway sent us before we
curate it into engine-facing models.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.broker.ibkr.models import (
    IbkrApiCallbackName,
    IbkrApiRequestEvidence,
    IbkrApiRequestName,
    IbkrApiResponseEvidence,
    IbkrObjectSnapshot,
    IbkrSerializerWarning,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_MAX_EVENTS = 10_000
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

    def clear(self) -> None:
        self._seq = 0
        self._events.clear()

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
    warnings: list[IbkrSerializerWarning] = []
    for index, obj in enumerate(objects):
        snapshot = snapshot_ibkr_object(obj)
        if snapshot is None:
            out[f"object_{index}"] = {}
            continue
        error = snapshot.serializer_error
        if isinstance(error, str):
            warnings.append(
                IbkrSerializerWarning(
                    object_type=snapshot.object_type,
                    serializer_error=error,
                )
            )
        out[f"object_{index}"] = snapshot.model_dump(mode="json")
    return IbkrApiResponseEvidence(
        callback=callback,
        fields=out,
        serializer_warnings=warnings,
    )


def snapshot_ibkr_object(obj: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(obj)


def _object_snapshot(obj: object | None) -> IbkrObjectSnapshot | None:
    if obj is None:
        return None
    object_type = _object_type(obj)
    try:
        return IbkrObjectSnapshot(object_type=object_type, fields=_snapshot_fields(obj))
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        logger.warning(
            "IBKR evidence serializer emitted placeholder for unsupported object: %s",
            exc,
            extra={"object_type": object_type, "serializer_error": str(exc)},
        )
        return IbkrObjectSnapshot(
            object_type=object_type,
            fields={"serializer_error": str(exc)},
            serializer_error=str(exc),
        )


def _snapshot_fields(obj: object) -> dict[str, JsonValue]:
    return {
        key: _json_value(value)
        for key, value in _typed_fields(obj).items()
    }


def _typed_fields(obj: object) -> Mapping[str, object]:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {field.name: getattr(obj, field.name) for field in fields(obj)}
    if isinstance(obj, SimpleNamespace):
        return vars(obj)
    if isinstance(obj, tuple) and hasattr(obj, "_asdict"):
        return obj._asdict()
    if hasattr(obj, "__dict__"):
        return {
            key: value
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    raise TypeError(
        f"Cannot snapshot unsupported IBKR evidence object {type(obj).__qualname__}"
    )


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {str(key): _json_value(item) for key, item in value._asdict().items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    if isinstance(value, SimpleNamespace):
        return {key: _json_value(item) for key, item in vars(value).items()}
    if hasattr(value, "__dict__"):
        return {
            key: _json_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise TypeError(
        f"Cannot convert unsupported IBKR evidence value {type(value).__qualname__}"
    )


def _object_type(obj: object) -> str:
    cls = obj.__class__
    return f"{cls.__module__}.{cls.__qualname__}"
