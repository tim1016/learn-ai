"""Bounded runtime resources and compatibility-read evidence for one lane.

This module is intentionally fleet-generic: it does not know a provider,
account, strategy, or any broker credential.  A clerk-agent process opts into
it through :class:`app.config.FleetSettings`; the coordinator and legacy
combined process never install this middleware.  Two independent pools keep a
long-lived SSE client from consuming ordinary request capacity.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import now_ms_utc

_COMPATIBILITY_EVIDENCE_SCHEMA_VERSION = 1
_COMPATIBILITY_EVIDENCE_RELATIVE_PATH = Path("compatibility") / "route_hits.json"
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_COMPATIBILITY_ROUTE_FAMILIES = frozenset(
    {
        "broker_bots",
        "broker_configuration",
        "broker_v2_panel",
        "brokers_lane_extras",
        "run_replay",
    }
)
_COMPATIBILITY_RESPONSE_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})


class FleetLaneCapacityExhausted(Exception):
    """A lane's bounded request or stream budget could not admit a caller."""

    reason = "fleet_lane_capacity_exhausted"

    def __init__(self, pool: str) -> None:
        """Name the exhausted opaque resource class without request details."""
        self.pool = pool
        super().__init__(f"The fleet lane {pool} budget is exhausted.")


class _StreamCapacityRefused(Exception):
    """Stop an already-starting SSE response after its safe 503 replacement."""


@dataclass(frozen=True, slots=True)
class LaneRuntimeConfig:
    """Validated capacity sizing supplied by a clerk-agent deployment."""

    max_inflight_requests: int
    max_inflight_streams: int
    request_queue_limit: int
    request_queue_timeout_ms: int

    @classmethod
    def from_settings(cls, settings: Any) -> LaneRuntimeConfig:
        """Construct the explicit settings contract and reject partial sizing."""
        values = cls(
            max_inflight_requests=int(settings.MAX_INFLIGHT_REQUESTS),
            max_inflight_streams=int(settings.MAX_INFLIGHT_STREAMS),
            request_queue_limit=int(settings.REQUEST_QUEUE_LIMIT),
            request_queue_timeout_ms=int(settings.REQUEST_QUEUE_TIMEOUT_MS),
        )
        if values.max_inflight_requests < 1 or values.max_inflight_streams < 1:
            raise ValueError(
                "FLEET_MAX_INFLIGHT_REQUESTS and FLEET_MAX_INFLIGHT_STREAMS "
                "must both be positive for FLEET_ROLE=clerk_agent."
            )
        if values.request_queue_limit < 0 or values.request_queue_timeout_ms < 0:
            raise ValueError(
                "FLEET_REQUEST_QUEUE_LIMIT and FLEET_REQUEST_QUEUE_TIMEOUT_MS "
                "must not be negative."
            )
        return values


class _CapacityLease:
    """One idempotent acquired budget slot."""

    def __init__(self, pool: _CapacityPool) -> None:
        self._pool = pool
        self._released = False

    async def release(self) -> None:
        """Return the slot exactly once, including cancellation paths."""
        if not self._released:
            self._released = True
            await self._pool.release()


class _CapacityPool:
    """A small FIFO admission pool with a bounded queue and deadline."""

    def __init__(self, *, name: str, limit: int, queue_limit: int, timeout_ms: int) -> None:
        self._name = name
        self._limit = limit
        self._queue_limit = queue_limit
        self._timeout_s = timeout_ms / 1_000
        self._inflight = 0
        self._queued = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> _CapacityLease:
        """Acquire one slot or produce a typed bounded-admission refusal."""
        async with self._condition:
            if self._inflight < self._limit:
                self._inflight += 1
                return _CapacityLease(self)
            if self._queued >= self._queue_limit or self._timeout_s == 0:
                raise FleetLaneCapacityExhausted(self._name)
            self._queued += 1
            try:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: self._inflight < self._limit),
                        timeout=self._timeout_s,
                    )
                except TimeoutError as exc:
                    raise FleetLaneCapacityExhausted(self._name) from exc
                self._inflight += 1
                return _CapacityLease(self)
            finally:
                self._queued -= 1

    async def release(self) -> None:
        """Return one lease and wake a bounded waiter."""
        async with self._condition:
            self._inflight -= 1
            self._condition.notify(1)


def compatibility_route_family(method: str, path: str, *, pinned: bool) -> str | None:
    """Return the fixed inventory family for an eligible legacy read.

    This deliberately matches only the Delivery-B retained-unscoped inventory.
    It retains no raw route parameter (account, strategy, ticket, or order ID)
    and excludes canonical clerk paths and all internal traffic.
    """
    if pinned or method.upper() not in _SAFE_METHODS or not path.startswith("/api/brokers/"):
        return None
    parts = tuple(segment for segment in path.split("/") if segment)
    # api, brokers, broker, ...; the broker value is intentionally discarded.
    if len(parts) < 4 or parts[:2] != ("api", "brokers") or parts[3] == "clerks":
        return None
    tail = parts[3:]
    if tail[:1] == ("configuration",):
        return "broker_configuration"
    if tail[:1] == ("assets",) or tail[:1] == ("activities",) or tail[:1] == ("clock",):
        return "brokers_lane_extras"
    if tail[:1] in {("order-groups",), ("portfolio-history",), ("portfolio-history-proof",)}:
        return "brokers_lane_extras"
    if tail[:1] == ("live-verdict",):
        return "brokers_lane_extras"
    if tail[:2] == ("fees", "session-reconciliation"):
        return "brokers_lane_extras"
    if tail[:3] == ("live-envelope", "loss-hold", "clear") or tail[:2] == (
        "live-envelope",
        "loss-hold",
    ):
        return "brokers_lane_extras"
    if tail[:2] == ("clerk", "status") or tail[:2] == ("clerk", "custody-diagnosis"):
        return "brokers_lane_extras"
    if tail[:1] == ("panel-profile",):
        return "broker_v2_panel"
    if tail[:1] != ("bots",):
        return None
    if len(tail) >= 5 and tail[-3:] == ("runs", tail[-2], "replay-receipt"):
        return "run_replay"
    if tail[:2] == ("bots", "catalog"):
        return "broker_v2_panel"
    if len(tail) >= 3 and tail[0] == "bots" and tail[2] in {
        "panel",
        "actions",
        "authority-facts",
        "evidence",
        "live-snapshot",
    }:
        return "broker_v2_panel"
    if len(tail) >= 4 and tail[:2] == ("bots", tail[1]) and tail[2] == "chart":
        return "broker_v2_panel"
    return "broker_bots"


class CompatibilityReadEvidence:
    """A lane-local, bounded-cardinality export seam for Delivery E."""

    def __init__(self, clerk_dir: Path, *, clock: Callable[[], int] = now_ms_utc) -> None:
        self._path = clerk_dir / _COMPATIBILITY_EVIDENCE_RELATIVE_PATH
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The stable lane-local file which Delivery E can inspect or export."""
        return self._path

    def record(self, *, method: str, route_family: str, response_class: str) -> None:
        """Atomically add one completed response without retaining request content."""
        timestamp_ms = self._clock()
        if timestamp_ms < 0 or timestamp_ms > MAX_TIMESTAMP_MS:
            raise ValueError("Compatibility evidence clock returned an invalid timestamp.")
        if (
            method.upper() not in _SAFE_METHODS
            or route_family not in _COMPATIBILITY_ROUTE_FAMILIES
            or response_class not in _COMPATIBILITY_RESPONSE_CLASSES
        ):
            raise ValueError("Compatibility evidence accepts only fixed retained-read families.")
        key = (method.upper(), route_family, response_class)
        with self._lock:
            payload = self._read_locked()
            entries = payload["route_hits"]
            for entry in entries:
                if (
                    entry["method"],
                    entry["route_family"],
                    entry["response_class"],
                ) == key:
                    entry["count"] += 1
                    entry["last_observed_at_ms"] = timestamp_ms
                    break
            else:
                entries.append(
                    {
                        "method": key[0],
                        "route_family": key[1],
                        "response_class": key[2],
                        "count": 1,
                        "first_observed_at_ms": timestamp_ms,
                        "last_observed_at_ms": timestamp_ms,
                    }
                )
            entries.sort(
                key=lambda entry: (
                    entry["route_family"],
                    entry["method"],
                    entry["response_class"],
                )
            )
            payload["updated_at_ms"] = timestamp_ms
            self._write_locked(payload)

    def snapshot(self) -> Mapping[str, object]:
        """Read the compact public-safe aggregate for operator acceptance."""
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {
                "schema_version": _COMPATIBILITY_EVIDENCE_SCHEMA_VERSION,
                "updated_at_ms": None,
                "route_hits": [],
            }
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _COMPATIBILITY_EVIDENCE_SCHEMA_VERSION
            or not isinstance(payload.get("route_hits"), list)
        ):
            raise ValueError("Compatibility route-hit evidence is unreadable.")
        return payload

    def _write_locked(self, payload: Mapping[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self._path.parent, prefix=f".{self._path.name}.", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            assert temporary_path is not None
            os.replace(temporary_path, self._path)
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise


class FleetLaneRuntimeMiddleware:
    """Apply lane resources and unscoped-read measurement to a clerk agent."""

    def __init__(
        self,
        app: Any,
        *,
        config: LaneRuntimeConfig,
        evidence: CompatibilityReadEvidence,
    ) -> None:
        self.app = app
        self._requests = _CapacityPool(
            name="request",
            limit=config.max_inflight_requests,
            queue_limit=config.request_queue_limit,
            timeout_ms=config.request_queue_timeout_ms,
        )
        self._streams = _CapacityPool(
            name="stream",
            limit=config.max_inflight_streams,
            queue_limit=config.request_queue_limit,
            timeout_ms=config.request_queue_timeout_ms,
        )
        self._evidence = evidence

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Bound one HTTP call and retain only an eligible route-family hit."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            bytes(name).lower(): bytes(value)
            for name, value in scope.get("headers", [])
        }
        method = str(scope["method"]).upper()
        family = compatibility_route_family(
            method,
            str(scope.get("path", "")),
            pinned=b"x-fleet-clerk-id" in headers,
        )
        request_lease: _CapacityLease | None = None
        stream_lease: _CapacityLease | None = None
        refusal_sent = False
        measurement_recorded = False

        def record_response(status: int) -> None:
            """Record one emitted response class only after its start line."""
            nonlocal measurement_recorded
            if family is None or measurement_recorded:
                return
            response_class = f"{status // 100}xx"
            if response_class in _COMPATIBILITY_RESPONSE_CLASSES:
                self._evidence.record(
                    method=method,
                    route_family=family,
                    response_class=response_class,
                )
            measurement_recorded = True

        async def refuse(pool: str) -> None:
            body = json.dumps(
                {
                    "reason": FleetLaneCapacityExhausted.reason,
                    "message": f"The fleet lane {pool} budget is exhausted.",
                    "next_step": "Retry after the lane has available capacity.",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", b"1"),
                    ],
                }
            )
            record_response(503)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        try:
            try:
                request_lease = await self._requests.acquire()
            except FleetLaneCapacityExhausted as exc:
                await refuse(exc.pool)
                return

            async def send_wrapped(message: dict[str, Any]) -> None:
                nonlocal request_lease, stream_lease, refusal_sent
                if refusal_sent:
                    return
                if message["type"] == "http.response.start":
                    response_headers = {
                        bytes(name).lower(): bytes(value).lower()
                        for name, value in message.get("headers", [])
                    }
                    content_type = response_headers.get(b"content-type", b"")
                    if b"text/event-stream" in content_type:
                        try:
                            stream_lease = await self._streams.acquire()
                        except FleetLaneCapacityExhausted as exc:
                            refusal_sent = True
                            await request_lease.release()
                            request_lease = None
                            await refuse(exc.pool)
                            raise _StreamCapacityRefused from exc
                        await request_lease.release()
                        request_lease = None
                    await send(message)
                    record_response(int(message["status"]))
                    return
                await send(message)
                if (
                    message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                ):
                    if stream_lease is not None:
                        await stream_lease.release()
                        stream_lease = None
                    if request_lease is not None:
                        await request_lease.release()
                        request_lease = None

            with contextlib.suppress(_StreamCapacityRefused):
                await self.app(scope, receive, send_wrapped)
        finally:
            if stream_lease is not None:
                await stream_lease.release()
            if request_lease is not None:
                await request_lease.release()


__all__ = [
    "CompatibilityReadEvidence",
    "FleetLaneCapacityExhausted",
    "FleetLaneRuntimeMiddleware",
    "LaneRuntimeConfig",
    "compatibility_route_family",
]
