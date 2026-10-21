"""Optional receipts for actual acquisition work, never for timer/poll activity."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("lake_work_progress", default=None)


@contextmanager
def observe_work(sink: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def completed_work(kind: str, identity: str) -> None:
    sink = _SINK.get()
    if sink is not None:
        sink({"kind": "work", "work_kind": kind, "identity": hashlib.sha256(identity.encode()).hexdigest()})


def completed_provider_response(label: str, body: bytes) -> None:
    """One vendor answer arrived; its body is hashed only while someone observes."""
    if _SINK.get() is not None:
        completed_work("provider_response", label + ":" + hashlib.sha256(body).hexdigest())


@contextmanager
def bounded_wait(timeout_s: float) -> Iterator[None]:
    sink = _SINK.get()
    if sink is not None:
        sink({"kind": "external_wait", "bound_ms": int(timeout_s * 1000)})
    try:
        yield
    finally:
        if sink is not None:
            sink({"kind": "external_wait_end"})
