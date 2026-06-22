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
from app.broker.ibkr.orders import stream_order_events
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.schemas.broker_activity import (
    BrokerActivityPage,
    ReconciliationTimingPolicy,
)
from app.services.broker_activity_publisher import (
    BrokerActivityPublisher,
    get_publisher_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/live-instances",
    tags=["broker-activity"],
)


async def _ensure_publisher(
    strategy_instance_id: str,
) -> BrokerActivityPublisher:
    """Return the publisher for the instance, bootstrapping one on demand.

    Lazy registration: if no publisher is running for this instance, we
    check whether (a) a ``live_state.json`` envelope exists on disk —
    i.e. the instance is currently active — and (b) the IBKR client is
    connected — i.e. the publisher would have something to consume.
    When both are true, we construct and register a publisher. When
    either is false, the endpoint surfaces a 404 / 503.

    This pattern keeps the deploy-lifecycle changes out of slice 1 —
    auto-start on deploy lands in slice 3 alongside the reconnect
    protocol (when the publisher lifecycle is touched anyway).
    """
    registry = get_publisher_registry()
    existing = registry.get(strategy_instance_id)
    if existing is not None and existing.is_running:
        return existing

    settings = get_settings()
    artifacts_root = FsPath(settings.live_runs_root).parent
    envelope_path = stable_live_state_path(artifacts_root, strategy_instance_id)
    if not envelope_path.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no live envelope for strategy_instance_id="
            f"{strategy_instance_id!r}",
        )
    try:
        envelope = LiveStateSidecarRepo(envelope_path).read()
    except LiveStateSidecarCorruptError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"live envelope is corrupt: {exc}",
        ) from exc
    if envelope is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"live envelope empty for {strategy_instance_id!r}",
        )

    try:
        client = get_client()
        if not client.is_connected():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "IBKR broker disconnected; cannot start broker-activity publisher.",
            )
    except RuntimeError as exc:  # client never installed (broker disabled)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR broker disabled; broker-activity surface unavailable.",
        ) from exc

    from app.engine.live.run import _latest_run_dir_for_instance

    run_dir = _latest_run_dir_for_instance(artifacts_root, strategy_instance_id)
    if run_dir is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no run directory for {strategy_instance_id!r}",
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
        strategy_instance_id=strategy_instance_id,
        bot_order_namespace=envelope.bot_order_namespace,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        timing_policy=timing_policy,
        event_source_factory=partial(stream_order_events, client),
    )
    return await registry.register(
        publisher, strategy_instance_id=strategy_instance_id
    )


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


__all__ = ["router"]
