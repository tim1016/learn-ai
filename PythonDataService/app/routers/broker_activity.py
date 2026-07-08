"""HTTP surface for the broker-activity reconciliation stream (ADR 0014).

Two endpoints per strategy instance:

- ``GET /api/live-instances/{strategy_instance_id}/broker-activity/stream``
  — SSE channel. Subscribes the cockpit FIRST (so live rows are buffered
  while we drain WAL), then emits every WAL row with ``seq > since_seq``
  as a backfill, then transitions seamlessly into live rows — deduping
  any row that arrived in the WAL during the drain. This is the standard
  cockpit flow; the client passes the highest ``seq`` it already has and
  never misses a row across the REST/SSE handoff.
- ``GET /api/live-instances/{strategy_instance_id}/broker-activity``
  — REST paginated query against the WAL. Kept as a forensic utility
  for ad-hoc lookups (operator tools, log inspection); the cockpit
  does NOT use this path — it subscribes directly to the SSE stream
  with ``since_seq`` and gets backfill + live in one channel.

The router is render-only: the publisher (``broker_activity_publisher``)
authors every row server-side per the truthfulness contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse

from app.broker.ibkr.client import get_client
from app.broker.ibkr.config import get_settings
from app.broker.ibkr.orders import (
    executions_for_reconnect_recovery,
    stream_order_events,
)
from app.engine.live.identity import _INSTANCE_ID_RE
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
)
from app.operator.incidents.store import IncidentStore
from app.routers.live_runs import _confine, _validate_path_segment
from app.schemas.broker_activity import (
    BrokerActivityPage,
    ReconciliationTimingPolicy,
)
from app.services.broker_activity_publisher import BrokerActivityPublisher
from app.services.broker_activity_publisher_registry import get_publisher_registry


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
    safe = _validate_path_segment(
        strategy_instance_id, field="strategy_instance_id"
    )
    if _INSTANCE_ID_RE.fullmatch(safe) is None:
        raise ValueError(f"invalid strategy_instance_id: {strategy_instance_id!r}")
    return safe


def _live_state_path_for_request(
    artifacts_root: FsPath, strategy_instance_id: str
) -> FsPath:
    """Build a CodeQL-visible confined live-state sidecar path."""
    safe_sid = _validate_strategy_instance_id_for_path(strategy_instance_id)
    sidecar_dir = _confine(artifacts_root / "live_state", safe_sid)
    return sidecar_dir / "live_state.json"


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
    envelope_path = _live_state_path_for_request(
        artifacts_root, safe_strategy_instance_id
    )
    # _live_state_path_for_request validates the URL segment, confines it
    # below artifacts/live_state, and returns the confined path directly.
    # codeql[py/path-injection]
    if not envelope_path.is_file():
        raise PublisherBootstrapError(
            "no_envelope",
            f"no live envelope for strategy_instance_id={safe_strategy_instance_id!r}",
        )
    try:
        envelope = LiveStateSidecarRepo(envelope_path).read()
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
) -> BrokerActivityPage:
    """Forensic / ad-hoc paginated query against the WAL.

    NOT the standard cockpit flow — cockpit clients subscribe directly
    to ``/broker-activity/stream?since_seq=<N>`` which does its own WAL
    backfill and seamlessly transitions to live without a handoff gap.
    Use this endpoint for operator tools, log inspection, or any
    out-of-band lookup.
    """
    publisher = await _ensure_publisher(strategy_instance_id)
    rows = publisher.backfill(after_seq=after_seq, limit=limit)
    # ``next_seq`` is the cursor the caller passes verbatim as the next
    # ``after_seq``: the highest seq returned in this page. ``None``
    # iff this page drained the WAL.
    last_persisted = publisher.last_persisted_seq()
    next_seq = (
        rows[-1].seq
        if rows and rows[-1].seq < last_persisted
        else None
    )
    return BrokerActivityPage(rows=rows, next_seq=next_seq)


@router.get(
    "/{strategy_instance_id}/broker-activity/stream",
    summary="SSE stream of broker-activity rows (backfill + live)",
)
async def broker_activity_stream(
    strategy_instance_id: Annotated[str, Path(min_length=1)],
    since_seq: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "Replay every WAL row with ``seq > since_seq`` as the "
                "backfill, then transition to live without a gap. "
                "Cold-start clients pass 0; reconnecting clients pass "
                "the highest seq they have."
            ),
        ),
    ] = 0,
) -> StreamingResponse:
    publisher = await _ensure_publisher(strategy_instance_id)

    async def event_source():
        # Subscribe FIRST so any row authored during the WAL drain is
        # buffered to this client's queue; the live-mode loop below
        # then dedupes against ``last_emitted_seq`` so we never deliver
        # the same seq twice across the backfill/live boundary.
        queue = publisher.subscribe()
        try:
            # 1. WAL backfill — every row the client hasn't seen.
            backlog = publisher.backfill(after_seq=since_seq)
            last_emitted_seq = since_seq
            for row in backlog:
                yield f"event: row\ndata: {row.model_dump_json()}\n\n"
                last_emitted_seq = row.seq
            # 2. Live mode — drain the queue, skipping anything already
            # covered by the backfill above (a row may have landed in
            # both the WAL and the queue while we were draining).
            while True:
                row = await queue.get()
                if row is None:
                    yield "event: end\ndata: {}\n\n"
                    return
                if row.seq <= last_emitted_seq:
                    continue  # already delivered in the backfill
                yield f"event: row\ndata: {row.model_dump_json()}\n\n"
                last_emitted_seq = row.seq
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "broker-activity SSE stream error",
                extra={"strategy_instance_id": strategy_instance_id},
            )
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        finally:
            publisher.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = [
    "PublisherBootstrapError",
    "bootstrap_publisher_for_instance",
    "router",
]
