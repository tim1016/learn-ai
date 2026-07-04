"""Service layer for the broker session mirror."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import NotConnectedError, get_client
from app.broker.ibkr.config import get_settings
from app.broker.ibkr.health import build_broker_health, synthetic_disconnected_health
from app.broker.ibkr.models import IbkrConnectionHealth
from app.broker.safety_verdict import derive_broker_safety_verdict
from app.engine.live import host_daemon_client
from app.engine.live.engine_runtime import (
    ENGINE_RUNTIME_FILENAME,
    EngineRuntimeSnapshot,
    read_engine_runtime_snapshot,
)
from app.routers.broker_dependencies import is_broker_disabled
from app.schemas.broker_session import (
    BrokerSessionMirrorSnapshot,
    GatewaySocketsSnapshot,
)
from app.schemas.live_runs import HostRunnerInstancesStatus
from app.services.broker_session_events import (
    BrokerSessionEventService,
    get_broker_session_event_service,
)
from app.services.broker_session_reconciler import (
    RuntimeIndexEntry,
    reconcile_broker_session_roster,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class BrokerSessionMirrorService:
    """Compose the read-only mirror snapshot from host and data-plane facts."""

    def __init__(
        self,
        *,
        event_service: BrokerSessionEventService | None = None,
    ) -> None:
        self._event_service = event_service or get_broker_session_event_service()

    async def snapshot(self) -> BrokerSessionMirrorSnapshot:
        settings = get_settings()
        as_of_ms = now_ms_utc()
        degradation_reasons: list[str] = []

        socket_snapshot: GatewaySocketsSnapshot | None = None
        registry_snapshot: HostRunnerInstancesStatus | None = None
        daemon_url = settings.live_runner_daemon_url.strip()
        if daemon_url:
            socket_result, socket_snapshot = await host_daemon_client.fetch_gateway_sockets(
                daemon_url,
                gateway_port=settings.port,
            )
            if socket_snapshot is None:
                degradation_reasons.append(
                    socket_result.detail or "host daemon socket probe unavailable"
                )
            registry_result, registry_payload = await host_daemon_client.fetch_instances(daemon_url)
            if registry_payload is not None:
                try:
                    registry_snapshot = HostRunnerInstancesStatus.model_validate(registry_payload)
                except ValueError as exc:
                    degradation_reasons.append(f"host daemon registry contract mismatch: {exc}")
            else:
                degradation_reasons.append(
                    registry_result.detail or "host daemon process registry unavailable"
                )
        else:
            degradation_reasons.append("host daemon URL is not configured")

        runtime_index = _build_runtime_index(Path(settings.live_runs_root))
        data_plane_health = _data_plane_health()
        rows = reconcile_broker_session_roster(
            socket_rows=socket_snapshot.sockets if socket_snapshot is not None else [],
            registry_snapshot=registry_snapshot,
            runtime_index=runtime_index,
            data_plane_health=data_plane_health,
            as_of_ms=as_of_ms,
            socket_probe_available=socket_snapshot is not None,
        )
        event_counts_by_client_id = self._event_service.counts_by_client_id()
        rows = [
            row.model_copy(
                update={
                    "event_counts": event_counts_by_client_id.get(row.client_id, {})
                    if row.client_id is not None
                    else {}
                }
            )
            for row in rows
        ]
        observer_status = "online" if socket_snapshot is not None else "degraded"
        ghost_detection_status = "available" if socket_snapshot is not None else "unknown"
        return BrokerSessionMirrorSnapshot(
            as_of_ms=as_of_ms,
            gateway_port=settings.port,
            observer_status=observer_status,
            ghost_detection_status=ghost_detection_status,
            rows=rows,
            degradation_reasons=degradation_reasons,
        )


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
            reason="IBKR_BROKER_ENABLED=false — host runner owns the IBKR session",
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


def _build_runtime_index(live_runs_root: Path) -> dict[str, RuntimeIndexEntry]:
    out: dict[str, RuntimeIndexEntry] = {}
    if not live_runs_root.is_dir():
        return out
    for run_dir in live_runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        entry = _runtime_entry_from_run_dir(run_dir)
        if entry is not None:
            out[str(run_dir.resolve())] = entry
    return out


def _runtime_entry_from_run_dir(run_dir: Path) -> RuntimeIndexEntry | None:
    ledger = _read_json_object(run_dir / "run_ledger.json")
    if ledger is None:
        return None
    run_id = ledger.get("run_id") or run_dir.name
    strategy_instance_id = ledger.get("strategy_instance_id")
    if not isinstance(run_id, str) or not isinstance(strategy_instance_id, str):
        return None
    if not strategy_instance_id:
        return None
    runtime = read_engine_runtime_snapshot(run_dir / ENGINE_RUNTIME_FILENAME)
    return RuntimeIndexEntry(
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        run_dir=str(run_dir.resolve()),
        account_id=_str_or_none(ledger.get("account_id")),
        pid=runtime.pid if runtime is not None else None,
        client_id=_runtime_client_id(runtime),
        connection_state=runtime.broker.connection_state if runtime is not None else None,
        posture=runtime.broker.effective_posture if runtime is not None else None,
        connection_epoch=runtime.broker.connection_epoch if runtime is not None else None,
        last_event_ms=runtime.broker.observation_at_ms if runtime is not None else None,
    )


def _runtime_client_id(_runtime: EngineRuntimeSnapshot | None) -> int | None:
    return _runtime.broker.client_id if _runtime is not None else None


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


_SERVICE = BrokerSessionMirrorService()
