"""Service layer for the broker session mirror."""

from __future__ import annotations

import asyncio
import logging

from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import NotConnectedError, get_client
from app.broker.ibkr.config import get_settings
from app.broker.ibkr.health import build_broker_health, synthetic_disconnected_health
from app.broker.ibkr.models import IbkrConnectionHealth
from app.broker.safety_verdict import derive_broker_safety_verdict
from app.engine.live import host_daemon_client
from app.routers.broker_dependencies import is_broker_disabled
from app.schemas.broker_session import (
    BrokerSessionMirrorSnapshot,
    GatewaySocketsSnapshot,
    summarize_broker_session_rows,
)
from app.services.broker_session_events import (
    BrokerSessionEventService,
    get_broker_session_event_service,
)
from app.services.broker_session_history import (
    BrokerSessionHistoryService,
    get_broker_session_history_service,
)
from app.services.broker_session_reconciler import reconcile_broker_session_snapshot
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class BrokerSessionMirrorService:
    """Compose the read-only mirror snapshot from host and data-plane facts."""

    def __init__(
        self,
        *,
        event_service: BrokerSessionEventService | None = None,
        history_service: BrokerSessionHistoryService | None = None,
    ) -> None:
        self._event_service = event_service or get_broker_session_event_service()
        self._history_service = history_service or get_broker_session_history_service()

    async def snapshot(
        self,
        *,
        record_history: bool = False,
    ) -> BrokerSessionMirrorSnapshot:
        """Compose one mirror snapshot.

        ``record_history`` is opt-in for the mirror's own surfaces (the
        session-mirror page and its stream). Composed read paths must not write
        durable history; unbounded per-assembly appends grew the history log to
        1.3GB and OOM-killed the data plane at boot on 2026-07-10.
        """
        settings = get_settings()
        as_of_ms = now_ms_utc()
        degradation_reasons: list[str] = []

        socket_snapshot: GatewaySocketsSnapshot | None = None
        daemon_url = settings.live_runner_daemon_url.strip()
        if daemon_url:
            socket_result, socket_snapshot = await host_daemon_client.fetch_gateway_sockets(
                daemon_url,
                gateway_port=settings.port,
            )
            if socket_snapshot is None:
                degradation_reasons.append(socket_result.detail or "host daemon socket probe unavailable")
        else:
            degradation_reasons.append("host daemon URL is not configured")

        data_plane_health = _data_plane_health()
        reconciliation = reconcile_broker_session_snapshot(
            socket_rows=socket_snapshot.sockets if socket_snapshot is not None else [],
            data_plane_health=data_plane_health,
            as_of_ms=as_of_ms,
            account_clerks=socket_snapshot.account_clerks if socket_snapshot is not None else [],
            socket_probe_available=socket_snapshot is not None,
        )
        rows = reconciliation.rows
        if socket_snapshot is not None:
            rows.extend(
                await asyncio.to_thread(
                    self._history_service.past_closed_rows,
                    current_rows=rows,
                )
            )
        try:
            event_attachments_by_row_id = await asyncio.to_thread(
                self._event_service.events_for_rows,
                rows,
            )
        except OSError as exc:
            logger.warning("failed to read broker session event attachments: %s", exc)
            degradation_reasons.append(f"broker event log unavailable: {exc}")
            event_attachments_by_row_id = {}
        enriched_rows = []
        for row in rows:
            attachment = event_attachments_by_row_id.get(row.row_id)
            enriched_rows.append(
                row.model_copy(
                    update={
                        "event_counts": attachment.event_counts
                        if attachment is not None
                        else {},
                        "events": attachment.events if attachment is not None else [],
                    }
                )
            )
        rows = enriched_rows
        observer_status = "online" if socket_snapshot is not None else "degraded"
        ghost_detection_status = "available" if socket_snapshot is not None else "unknown"
        snapshot = BrokerSessionMirrorSnapshot(
            as_of_ms=as_of_ms,
            gateway_port=settings.port,
            observer_status=observer_status,
            ghost_detection_status=ghost_detection_status,
            global_events=reconciliation.global_events,
            rows=rows,
            summary=summarize_broker_session_rows(rows),
            degradation_reasons=degradation_reasons,
        )
        if record_history:
            try:
                await asyncio.to_thread(
                    self._history_service.append_snapshot, snapshot
                )
            except OSError as exc:
                logger.warning("failed to append broker session history: %s", exc)
        return snapshot


def get_broker_session_mirror_service() -> BrokerSessionMirrorService:
    return _SERVICE


def _data_plane_health() -> IbkrConnectionHealth:
    settings = get_settings()
    safety_verdict = derive_broker_safety_verdict(
        configured_mode=settings.mode,
        readonly_flag=None,
        port=settings.port,
        connected_account=None,
    )
    if is_broker_disabled():
        return synthetic_disconnected_health(
            state="disabled",
            disabled=True,
            reason="IBKR_BROKER_ENABLED=false — IBKR data-plane access is disabled",
            safety_verdict=safety_verdict,
        )
    try:
        client = get_client()
    except NotConnectedError:
        return synthetic_disconnected_health(safety_verdict=safety_verdict)
    return build_broker_health(
        client,
        get_monitor(),
        safety_verdict=derive_broker_safety_verdict(
            configured_mode=client.settings.mode,
            readonly_flag=None,
            port=client.settings.port,
            connected_account=client.connected_account,
        ),
    )


_SERVICE = BrokerSessionMirrorService()
