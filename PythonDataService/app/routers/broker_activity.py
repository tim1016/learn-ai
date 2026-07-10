"""HTTP surface for the broker-activity reconciliation stream (ADR 0014).

Two endpoints per strategy instance share the publisher-owned durable channel:

- ``GET /api/live-instances/{strategy_instance_id}/broker-activity/stream``
  — SSE channel. Replays the sequence-indexed in-memory ring, then follows
  live rows without request-owned WAL scans. Rows carry composite
  ``<durable_stream_id>:<seq>`` IDs. Ring misses and slow-client overflow
  produce explicit recovery control events.
- ``GET /api/live-instances/{strategy_instance_id}/broker-activity``
  — REST paginated query against the WAL. It accepts and returns the same
  composite cursor and is the deep-replay path after a gap marker.

The router is render-only: the publisher (``broker_activity_publisher``)
authors every row server-side per the truthfulness contract.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse

from app.broker.ibkr.client import get_client
from app.broker.ibkr.config import get_settings
from app.broker.ibkr.orders import (
    executions_for_reconnect_recovery,
    stream_order_events,
)
from app.engine.live.identity import safe_strategy_instance_path_segment
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.operator.incidents.store import IncidentStore
from app.schemas.broker_activity import (
    BrokerActivityPage,
    ReconciliationTimingPolicy,
)
from app.services.broker_activity_publisher import BrokerActivityPublisher
from app.services.broker_activity_publisher_registry import get_publisher_registry
from app.services.durable_event_channel import EventCursor
from app.services.durable_event_stream import (
    parse_event_cursor,
    resolve_stream_cursor,
    stream_durable_event_channel,
)


class PublisherBootstrapError(Exception):
    """Raised when ``bootstrap_publisher_for_instance`` cannot build a publisher.

    Carries a short ``code`` ("no_envelope", "envelope_corrupt",
    "broker_disconnected", "broker_disabled", "no_run_dir") so callers
    that surface to HTTP can map to status codes and callers that just
    want best-effort startup (the deploy-time hook in
    ``live_instances.start_run``) can log and continue.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/live-instances",
    tags=["broker-activity"],
)


def _validate_strategy_instance_id_for_path(strategy_instance_id: str) -> str:
    return safe_strategy_instance_path_segment(strategy_instance_id)


async def bootstrap_publisher_for_instance(
    strategy_instance_id: str,
) -> BrokerActivityPublisher:
    """Construct (or return the running) publisher for the instance.

    Slice 3: extracted from ``_ensure_publisher`` so the deploy-time
    start hook in ``live_instances.start_run`` can call it after a
    successful daemon start, NOT just on the cockpit's first
    broker-activity hit. Raises ``PublisherBootstrapError`` (with a
    short ``code``) when the publisher cannot be built so the caller
    can map to either an HTTP status (cockpit lazy path) or a structured
    log entry (deploy-time best-effort path).

    Preconditions checked in order: the instance's ``live_state.json``
    sidecar exists and is readable; the IBKR broker singleton is
    installed and connected; the instance's latest run dir resolves
    locally. Each missing precondition emits a distinct ``code``.

    The publisher carries both the live event source (the existing
    ``stream_order_events`` async iterator) AND the reconnect-recovery
    source (the new ``executions_for_reconnect_recovery`` adapter, which
    wraps ``IB.reqExecutionsAsync`` into ``IbkrOrderEvent``s the
    publisher's sweep authors with ``reconnect_recovery_active=True``).
    """
    try:
        safe_strategy_instance_id = _validate_strategy_instance_id_for_path(
            strategy_instance_id
        )
    except ValueError as exc:
        raise PublisherBootstrapError(
            "invalid_instance_id", "invalid strategy_instance_id"
        ) from exc

    registry = get_publisher_registry()
    existing = registry.get(safe_strategy_instance_id)
    if existing is not None and existing.is_running:
        return existing

    settings = get_settings()
    artifacts_root = FsPath(settings.live_runs_root).parent
    envelope_path = stable_live_state_path(artifacts_root, safe_strategy_instance_id)
    try:
        envelope = LiveStateSidecarRepo(
            envelope_path, trusted_root=artifacts_root / "live_state"
        ).read()
    except LiveStateSidecarCorruptError as exc:
        raise PublisherBootstrapError(
            "envelope_corrupt", f"live envelope is corrupt: {exc}"
        ) from exc
    if envelope is None:
        raise PublisherBootstrapError(
            "no_envelope",
            f"live envelope empty for {safe_strategy_instance_id!r}",
        )

    try:
        client = get_client()
    except Exception as exc:  # NotConnectedError when singleton missing
        raise PublisherBootstrapError(
            "broker_disabled",
            "IBKR broker disabled; broker-activity surface unavailable.",
        ) from exc
    if not client.is_connected():
        raise PublisherBootstrapError(
            "broker_disconnected",
            "IBKR broker disconnected; cannot start broker-activity publisher.",
        )

    from app.engine.live.run_lookup import latest_run_dir_for_instance

    run_dir = latest_run_dir_for_instance(artifacts_root, safe_strategy_instance_id)
    if run_dir is None:
        raise PublisherBootstrapError(
            "no_run_dir", f"no run directory for {safe_strategy_instance_id!r}"
        )

    timing_policy_dict = (
        envelope.model_dump().get("reconciliation_timing_policy")
        if hasattr(envelope, "reconciliation_timing_policy")
        else None
    )
    timing_policy = (
        ReconciliationTimingPolicy.model_validate(timing_policy_dict)
        if timing_policy_dict
        else ReconciliationTimingPolicy()
    )

    publisher = BrokerActivityPublisher(
        strategy_instance_id=safe_strategy_instance_id,
        bot_order_namespace=envelope.bot_order_namespace,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        timing_policy=timing_policy,
        event_source_factory=partial(stream_order_events, client),
        recovery_source_factory=partial(executions_for_reconnect_recovery, client),
        incident_store=IncidentStore(run_dir),
    )
    return await registry.register(
        publisher, strategy_instance_id=safe_strategy_instance_id
    )


# Map bootstrap error codes to HTTP status codes for the cockpit lazy
# path. ``broker_disconnected`` and ``broker_disabled`` are both 503;
# ``no_envelope`` / ``no_run_dir`` are 404; ``envelope_corrupt`` is 503.
_BOOTSTRAP_ERROR_STATUS: dict[str, int] = {
    "invalid_instance_id": status.HTTP_400_BAD_REQUEST,
    "no_envelope": status.HTTP_404_NOT_FOUND,
    "envelope_corrupt": status.HTTP_503_SERVICE_UNAVAILABLE,
    "broker_disconnected": status.HTTP_503_SERVICE_UNAVAILABLE,
    "broker_disabled": status.HTTP_503_SERVICE_UNAVAILABLE,
    "no_run_dir": status.HTTP_404_NOT_FOUND,
}


async def _ensure_publisher(
    strategy_instance_id: str,
) -> BrokerActivityPublisher:
    """Lazy-bootstrap path for cockpit-first scenarios.

    Slice 3: the hot path is now "registry already has a running
    publisher; return it" because ``live_instances.start_run`` registers
    the publisher at deploy time. This fallback only fires when the
    cockpit hits the broker-activity surface before any start_run hook
    has run for the instance (e.g. an operator opens the Activity tab
    on a still-bootstrapping run, or the deploy-time hook saw a
    transient broker disconnect and bailed out).
    """
    try:
        return await bootstrap_publisher_for_instance(strategy_instance_id)
    except PublisherBootstrapError as exc:
        status_code = _BOOTSTRAP_ERROR_STATUS.get(
            exc.code, status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(status_code, exc.detail) from exc


@router.get(
    "/{strategy_instance_id}/broker-activity",
    response_model=BrokerActivityPage,
    summary="Ad-hoc paginated query against broker_activity.jsonl",
)
async def broker_activity_backfill(
    strategy_instance_id: Annotated[str, Path(min_length=1)],
    after_seq: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "Return rows with ``seq > after_seq``. To paginate, pass "
                "the previous response's ``next_seq`` verbatim — no "
                "off-by-one arithmetic required."
            ),
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Max rows per page; pass the returned ``next_seq`` as ``after_seq`` on the next call.",
        ),
    ] = 100,
    cursor: Annotated[str | None, Query()] = None,
) -> BrokerActivityPage:
    """Deep-replay and forensic paginated query against the WAL."""
    publisher = await _ensure_publisher(strategy_instance_id)
    channel = publisher.event_channel
    channel.refresh()
    parsed_cursor = parse_event_cursor(cursor)
    if parsed_cursor is not None and parsed_cursor.stream_id != channel.stream_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "EVENT_STREAM_REPLACED",
                "durable_stream_id": channel.stream_id,
            },
        )
    effective_after_seq = parsed_cursor.seq if parsed_cursor is not None else after_seq
    rows = publisher.backfill(after_seq=effective_after_seq, limit=limit)
    # ``next_seq`` is the cursor the caller passes verbatim as the next
    # ``after_seq``: the highest seq returned in this page. ``None``
    # iff this page drained the WAL.
    last_persisted = publisher.last_persisted_seq()
    next_seq = (
        rows[-1].seq
        if rows and rows[-1].seq < last_persisted
        else None
    )
    high_water_seq = rows[-1].seq if rows else effective_after_seq
    return BrokerActivityPage(
        rows=rows,
        next_seq=next_seq,
        durable_stream_id=channel.stream_id,
        high_water_cursor=EventCursor(channel.stream_id, high_water_seq).encode(),
        next_cursor=(
            EventCursor(channel.stream_id, next_seq).encode()
            if next_seq is not None
            else None
        ),
    )


@router.get(
    "/{strategy_instance_id}/broker-activity/stream",
    summary="SSE stream of broker-activity rows (backfill + live)",
)
async def broker_activity_stream(
    strategy_instance_id: Annotated[str, Path(min_length=1)],
    since_seq: Annotated[
        int | None,
        Query(
            ge=0,
            description=(
                "Legacy sequence-only cursor. New clients use the composite "
                "cursor or Last-Event-ID so WAL replacement is detectable."
            ),
        ),
    ] = None,
    cursor: Annotated[str | None, Query()] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    publisher = await _ensure_publisher(strategy_instance_id)
    channel = publisher.event_channel
    channel.refresh()
    effective_since_seq = since_seq if since_seq is not None else 0
    stream_cursor = resolve_stream_cursor(
        channel=channel,
        query_cursor=cursor,
        last_event_id=last_event_id,
        legacy_since_seq=effective_since_seq,
        legacy_since_seq_provided=since_seq is not None,
    )

    def handle_stream_error(exc: BaseException) -> str:
        logger.exception(
            "broker-activity SSE stream error",
            extra={"strategy_instance_id": strategy_instance_id},
        )
        err = json.dumps({"error": str(exc)})
        return f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        stream_durable_event_channel(
            channel=channel,
            cursor=stream_cursor,
            encode_row=lambda row: row.model_dump_json(),
            handle_error=handle_stream_error,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = [
    "PublisherBootstrapError",
    "bootstrap_publisher_for_instance",
    "router",
]
