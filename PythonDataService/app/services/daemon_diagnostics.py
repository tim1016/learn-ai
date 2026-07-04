"""Composed daemon diagnostics service.

The async service gathers daemon, registry, mirror, and connectivity facts. The
pure builder in :mod:`app.services.daemon_diagnostics_builder` authors the
operator-facing diagnostic meaning.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pydantic import ValidationError

from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.daemon_connectivity_monitor import (
    DaemonConnectivityState,
)
from app.engine.live.daemon_connectivity_monitor import (
    get_monitor as get_daemon_connectivity_monitor,
)
from app.engine.live.daemon_transport import DaemonResult
from app.schemas.broker_session import BrokerSessionMirrorSnapshot
from app.schemas.daemon_diagnostics import DaemonDiagnosticReport
from app.schemas.live_runs import HostRunnerInstancesStatus
from app.services.broker_session_mirror import (
    BrokerSessionMirrorService,
    get_broker_session_mirror_service,
)
from app.services.daemon_diagnostics_authoring import (
    project_daemon_diagnostic_report,
    redact_host_runner_health,
)
from app.services.daemon_diagnostics_builder import (
    build_daemon_diagnostic_report,
    build_run_dir_visibility,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

__all__ = [
    "DaemonDiagnosticsService",
    "build_daemon_diagnostic_report",
    "get_daemon_diagnostics_service",
    "project_daemon_diagnostic_report",
    "redact_host_runner_health",
]


class DaemonDiagnosticsService:
    """Compose fresh daemon diagnostics from existing authorities."""

    def __init__(
        self,
        *,
        mirror_service: BrokerSessionMirrorService | None = None,
    ) -> None:
        self._mirror_service = mirror_service or get_broker_session_mirror_service()

    async def report(
        self,
        *,
        strategy_instance_id: str | None = None,
    ) -> DaemonDiagnosticReport:
        settings = get_settings()
        daemon_url = (settings.live_runner_daemon_url or "").strip()
        fetched_at_ms = now_ms_utc()
        connectivity = _current_connectivity_state()

        if daemon_url:
            (daemon_result, health), (_registry_result, instances), mirror = await asyncio.gather(
                host_daemon_client.fetch_health(daemon_url),
                _fetch_instances(daemon_url),
                self._safe_mirror_snapshot(fetched_at_ms=fetched_at_ms),
            )
        else:
            daemon_result = DaemonResult(
                kind="UNREACHABLE",
                detail="host daemon URL is not configured",
                error_category="not_configured",
            )
            health = None
            instances = None
            mirror = await self._safe_mirror_snapshot(fetched_at_ms=fetched_at_ms)

        run_dir_visibility = (
            build_run_dir_visibility(Path(settings.live_runs_root), instances)
            if instances is not None
            else None
        )
        return build_daemon_diagnostic_report(
            daemon_result=daemon_result,
            health=health,
            instances=instances,
            mirror=mirror,
            connectivity=connectivity,
            fetched_at_ms=fetched_at_ms,
            strategy_instance_ids=[strategy_instance_id] if strategy_instance_id else None,
            run_dir_visibility=run_dir_visibility,
        )

    async def _safe_mirror_snapshot(self, *, fetched_at_ms: int) -> BrokerSessionMirrorSnapshot:
        try:
            return await self._mirror_service.snapshot()
        except Exception as exc:
            logger.warning("daemon diagnostics could not read broker session mirror: %s", exc)
            settings = get_settings()
            return BrokerSessionMirrorSnapshot(
                as_of_ms=fetched_at_ms,
                gateway_port=settings.port,
                observer_status="degraded",
                ghost_detection_status="unknown",
                rows=[],
                degradation_reasons=[f"broker session mirror unavailable: {exc}"],
            )


async def _fetch_instances(
    daemon_url: str,
) -> tuple[DaemonResult, HostRunnerInstancesStatus | None]:
    result, payload = await host_daemon_client.fetch_instances(daemon_url)
    if payload is None:
        return result, None
    try:
        return result, HostRunnerInstancesStatus.model_validate(payload)
    except ValidationError as exc:
        return DaemonResult.incompatible_contract(detail=str(exc)), None


def _current_connectivity_state() -> DaemonConnectivityState | None:
    monitor = get_daemon_connectivity_monitor()
    return monitor.state if monitor is not None else None


def get_daemon_diagnostics_service() -> DaemonDiagnosticsService:
    return _SERVICE


_SERVICE = DaemonDiagnosticsService()
