"""Pure reconciler for the broker session mirror.

The host daemon proves socket existence; the process registry claims which
bot owns a process; run artifacts provide durable attribution. This module
does the join without shelling out or touching the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.ibkr.models import IbkrConnectionHealth
from app.broker.ibkr.recovery_state_machine import recovery_state_from_connection_state
from app.operator.notices.broker_session import orphaned_socket_notice
from app.schemas.broker_session import (
    BrokerSessionAttentionCode,
    BrokerSessionGlobalEvent,
    BrokerSessionRegistryClaim,
    BrokerSessionRosterRow,
    GatewaySocketRow,
)
from app.schemas.live_runs import (
    HostRunnerInstance,
    HostRunnerInstancesStatus,
    HostRunnerProcessState,
)


@dataclass(frozen=True)
class RuntimeIndexEntry:
    """Durable run/runtime facts keyed by run directory."""

    strategy_instance_id: str
    run_id: str
    run_dir: str
    account_id: str | None = None
    pid: int | None = None
    client_id: int | None = None
    connection_state: str | None = None
    recovery_state: str | None = None
    posture: str | None = None
    connection_epoch: int | None = None
    last_event_ms: int | None = None


@dataclass(frozen=True)
class BrokerSessionReconciliationResult:
    """Broker-session mirror rows plus global infrastructure events."""

    rows: list[BrokerSessionRosterRow]
    global_events: list[BrokerSessionGlobalEvent]


_LIVE_REGISTRY_STATES = {
    HostRunnerProcessState.running,
    HostRunnerProcessState.stopping,
}


def reconcile_broker_session_snapshot(
    *,
    socket_rows: list[GatewaySocketRow],
    registry_snapshot: HostRunnerInstancesStatus | None,
    runtime_index: dict[str, RuntimeIndexEntry],
    data_plane_health: IbkrConnectionHealth | None,
    as_of_ms: int,
    socket_probe_available: bool = True,
    stale_after_ms: int = 25_000,
) -> BrokerSessionReconciliationResult:
    """Reconcile session rows and lift infrastructure into global events."""

    visible_socket_rows: list[GatewaySocketRow] = []
    gvproxy_socket_rows: list[GatewaySocketRow] = []
    global_events: list[BrokerSessionGlobalEvent] = []
    for socket in socket_rows:
        if _is_gvproxy_socket(socket):
            gvproxy_socket_rows.append(socket)
        else:
            visible_socket_rows.append(socket)
    if gvproxy_socket_rows:
        global_events.append(_gvproxy_global_event(gvproxy_socket_rows, as_of_ms))

    rows = reconcile_broker_session_roster(
        socket_rows=visible_socket_rows,
        registry_snapshot=registry_snapshot,
        runtime_index=runtime_index,
        as_of_ms=as_of_ms,
        socket_probe_available=socket_probe_available,
        stale_after_ms=stale_after_ms,
    )
    if data_plane_health is not None:
        global_events.append(_data_plane_global_event(data_plane_health, as_of_ms))
    return BrokerSessionReconciliationResult(rows=rows, global_events=global_events)


def reconcile_broker_session_roster(
    *,
    socket_rows: list[GatewaySocketRow],
    registry_snapshot: HostRunnerInstancesStatus | None,
    runtime_index: dict[str, RuntimeIndexEntry],
    as_of_ms: int,
    socket_probe_available: bool = True,
    stale_after_ms: int = 25_000,
) -> list[BrokerSessionRosterRow]:
    """Reconcile intent, registry claim, and OS socket truth into roster rows."""

    runtime_by_dir = {_normalise_path(key): value for key, value in runtime_index.items()}
    runtime_by_run_id = {value.run_id: value for value in runtime_index.values()}
    registry_instances = registry_snapshot.instances if registry_snapshot is not None else []
    registry_by_pid = _registry_by_pid(registry_instances)
    registry_by_dir = _registry_by_run_dir(registry_instances)

    matched_registry_ids: set[str] = set()
    rows: list[BrokerSessionRosterRow] = []

    for index, socket in enumerate(socket_rows):
        run_dir_key = _normalise_optional_path(socket.run_dir)
        runtime = _runtime_for_socket_run_dir(
            run_dir_key=run_dir_key,
            runtime_by_dir=runtime_by_dir,
            runtime_by_run_id=runtime_by_run_id,
        )
        registry = _registry_for_socket(socket, registry_by_pid, registry_by_dir, run_dir_key)
        if registry is not None:
            matched_registry_ids.add(_registry_id(registry))
            if runtime is None:
                runtime = _runtime_from_registry(registry)

        attention: list[BrokerSessionAttentionCode] = []
        identity_type = "ghost"
        if runtime is not None:
            identity_type = "bot"
            if socket.pid is None:
                identity_type = "orphaned_bot_socket"
                attention.extend(["SOCKET_WITHOUT_LIVE_PID", "ORPHANED_BOT_SOCKET"])
            elif registry_snapshot is not None and not _registry_claims_live_socket(registry, socket):
                attention.append("REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE")
            if _runtime_signal_stale(runtime, as_of_ms, stale_after_ms):
                attention.append("CLIENT_SIGNAL_STALE")
        else:
            attention.append("GHOST_SOCKET")
        notice = (
            orphaned_socket_notice(
                strategy_instance_id=runtime.strategy_instance_id,
                run_id=runtime.run_id,
                client_id=runtime.client_id,
                local_port=socket.local_port,
                remote_port=socket.remote_port,
                run_dir=runtime.run_dir,
                observed_at_ms=as_of_ms,
            )
            if runtime is not None and identity_type == "orphaned_bot_socket"
            else None
        )

        rows.append(
            BrokerSessionRosterRow(
                row_id=_socket_row_id(socket, index),
                identity_type=identity_type,
                recency="current",
                socket_present=True,
                strategy_instance_id=runtime.strategy_instance_id if runtime else None,
                run_id=runtime.run_id if runtime else None,
                account_id=runtime.account_id if runtime else None,
                posture=runtime.posture if runtime else None,
                client_id=runtime.client_id if runtime else None,
                pid=socket.pid,
                command=socket.command,
                run_dir=runtime.run_dir if runtime else socket.run_dir,
                local_port=socket.local_port,
                remote_host=socket.remote_host,
                remote_port=socket.remote_port,
                connection_state=runtime.connection_state if runtime else None,
                recovery_state=_runtime_recovery_state(runtime),
                connection_epoch=runtime.connection_epoch if runtime else None,
                last_event_ms=runtime.last_event_ms if runtime else None,
                as_of_ms=as_of_ms,
                attention_codes=attention,
                registry_claim=_registry_claim(registry),
                notice=notice,
            )
        )

    matched_socket_pids = {row.pid for row in socket_rows if row.pid is not None}
    matched_socket_dirs = {
        path for path in (_normalise_optional_path(row.run_dir) for row in socket_rows) if path is not None
    }

    if not socket_probe_available:
        rows.extend(
            _last_known_runtime_rows(
                runtime_by_dir=runtime_by_dir,
                matched_socket_dirs=matched_socket_dirs,
                as_of_ms=as_of_ms,
                stale_after_ms=stale_after_ms,
            )
        )
    elif registry_snapshot is None:
        rows.extend(
            _runtime_only_rows_when_registry_unavailable(
                runtime_by_dir=runtime_by_dir,
                matched_socket_dirs=matched_socket_dirs,
                as_of_ms=as_of_ms,
                stale_after_ms=stale_after_ms,
            )
        )

    for registry in registry_instances:
        if _registry_id(registry) in matched_registry_ids:
            continue
        if not socket_probe_available:
            continue
        process = registry.process
        run_dir_key = _normalise_path(registry.run_dir)
        is_running_without_socket = (
            process.state in _LIVE_REGISTRY_STATES
            and (process.pid is None or process.pid not in matched_socket_pids)
            and run_dir_key not in matched_socket_dirs
        )
        if not is_running_without_socket:
            continue
        runtime = runtime_by_dir.get(run_dir_key) or _runtime_from_registry(registry)
        rows.append(
            BrokerSessionRosterRow(
                row_id=f"registry:{registry.strategy_instance_id}:{registry.run_id}",
                identity_type="bot",
                recency="unknown",
                socket_present=False,
                strategy_instance_id=runtime.strategy_instance_id,
                run_id=runtime.run_id,
                account_id=runtime.account_id,
                posture=runtime.posture,
                client_id=runtime.client_id,
                pid=process.pid,
                command=" ".join(process.command) if process.command else None,
                run_dir=runtime.run_dir,
                connection_state=runtime.connection_state,
                recovery_state=_runtime_recovery_state(runtime),
                connection_epoch=runtime.connection_epoch,
                last_event_ms=runtime.last_event_ms,
                as_of_ms=as_of_ms,
                attention_codes=_append_stale_attention(
                    ["STARTED_BUT_NO_SOCKET"],
                    runtime=runtime,
                    as_of_ms=as_of_ms,
                    stale_after_ms=stale_after_ms,
                ),
                registry_claim=_registry_claim(registry),
            )
        )

    return sorted(rows, key=_sort_key)


def _registry_by_pid(instances: list[HostRunnerInstance]) -> dict[int, HostRunnerInstance]:
    out: dict[int, HostRunnerInstance] = {}
    for instance in instances:
        pid = instance.process.pid
        if pid is not None:
            out[pid] = instance
    return out


def _registry_by_run_dir(instances: list[HostRunnerInstance]) -> dict[str, HostRunnerInstance]:
    return {_normalise_path(instance.run_dir): instance for instance in instances}


def _registry_for_socket(
    socket: GatewaySocketRow,
    registry_by_pid: dict[int, HostRunnerInstance],
    registry_by_dir: dict[str, HostRunnerInstance],
    run_dir_key: str | None,
) -> HostRunnerInstance | None:
    if socket.pid is not None:
        by_pid = registry_by_pid.get(socket.pid)
        if by_pid is not None:
            return by_pid
    return registry_by_dir.get(run_dir_key) if run_dir_key is not None else None


def _runtime_for_socket_run_dir(
    *,
    run_dir_key: str | None,
    runtime_by_dir: dict[str, RuntimeIndexEntry],
    runtime_by_run_id: dict[str, RuntimeIndexEntry],
) -> RuntimeIndexEntry | None:
    if run_dir_key is None:
        return None
    runtime = runtime_by_dir.get(run_dir_key)
    if runtime is not None:
        return runtime
    return runtime_by_run_id.get(Path(run_dir_key).name)


def _registry_claims_live_socket(
    registry: HostRunnerInstance | None,
    socket: GatewaySocketRow,
) -> bool:
    if registry is None:
        return False
    process = registry.process
    if process.state not in _LIVE_REGISTRY_STATES:
        return False
    return process.pid is None or socket.pid is None or process.pid == socket.pid


def _runtime_from_registry(instance: HostRunnerInstance) -> RuntimeIndexEntry:
    return RuntimeIndexEntry(
        strategy_instance_id=instance.strategy_instance_id,
        run_id=instance.run_id,
        run_dir=instance.run_dir,
        pid=instance.process.pid,
    )


def _registry_claim(
    instance: HostRunnerInstance | None,
) -> BrokerSessionRegistryClaim | None:
    if instance is None:
        return None
    process = instance.process
    return BrokerSessionRegistryClaim(
        state=process.state.value,
        run_id=instance.run_id,
        pid=process.pid,
        run_dir=instance.run_dir,
        started_at_ms=process.started_at_ms,
        ended_at_ms=process.ended_at_ms,
    )


def _data_plane_global_event(
    health: IbkrConnectionHealth,
    as_of_ms: int,
) -> BrokerSessionGlobalEvent:
    severity = _data_plane_event_severity(health)
    return BrokerSessionGlobalEvent(
        code="DATA_PLANE_BROKER_CLIENT",
        label="Data-plane broker client",
        severity=severity,
        summary=_data_plane_event_summary(health),
        current=health.connected,
        source="data_plane",
        observed_at_ms=health.last_transition_ms or health.fetched_at_ms or as_of_ms,
        client_id=health.client_id,
    )


def _data_plane_event_severity(
    health: IbkrConnectionHealth,
) -> str:
    if health.connection_state == "connected" and health.connected:
        return "info"
    if health.connection_state == "disabled":
        return "info"
    if health.connection_state in {"degraded_data_farm", "hard_down"}:
        return "critical"
    if health.connection_state in {
        "soft_lost",
        "subscriptions_stale",
        "reconnecting",
        "recovering",
        "disconnected",
    }:
        return "warning"
    if health.connected:
        return "warning"
    return "neutral"


def _data_plane_event_summary(
    health: IbkrConnectionHealth,
) -> str:
    if health.connection_state == "connected" and health.connected:
        return "The data-plane IBKR client is connected; this is global infrastructure, not a bot-owned session."
    if health.disabled:
        return "The data-plane IBKR client is disabled because live bot sessions are owned by the host runner."
    if health.connection_state == "degraded_data_farm":
        return "The data-plane IBKR client is socket-connected, but IBKR data-farm evidence is degraded."
    if health.connection_state in _DATA_PLANE_DEGRADED_COPY:
        return (
            f"The data-plane IBKR client reports {_DATA_PLANE_DEGRADED_COPY[health.connection_state]}; "
            "this global fact is separate from bot-owned sessions."
        )
    if health.connected:
        return "The data-plane IBKR client is socket-connected, but its broker state is not healthy."
    return "The data-plane IBKR client is not connected; this global fact is separate from bot-owned sessions."


_DATA_PLANE_DEGRADED_COPY = {
    "soft_lost": "broker feed loss",
    "subscriptions_stale": "stale broker subscriptions",
    "reconnecting": "broker reconnect in progress",
    "recovering": "broker stream recovery in progress",
    "hard_down": "exhausted broker recovery",
    "disconnected": "broker disconnect",
}


def _gvproxy_global_event(
    sockets: list[GatewaySocketRow],
    as_of_ms: int,
) -> BrokerSessionGlobalEvent:
    count = len(sockets)
    summary = (
        "A virtual-machine network proxy is connected to the IBKR gateway port. "
        "It is infrastructure, not a bot broker session."
    )
    if count > 1:
        summary = (
            f"{count} virtual-machine network proxy sockets are connected to the IBKR gateway port. "
            "They are infrastructure, not bot broker sessions."
        )
    return BrokerSessionGlobalEvent(
        code="GATEWAY_NETWORK_PROXY",
        label="Gateway network proxy",
        severity="info",
        summary=summary,
        current=True,
        source="network",
        observed_at_ms=as_of_ms,
    )


def _is_gvproxy_socket(socket: GatewaySocketRow) -> bool:
    haystack = " ".join([socket.command, *socket.argv]).lower()
    return "gvproxy" in haystack


def _last_known_runtime_rows(
    *,
    runtime_by_dir: dict[str, RuntimeIndexEntry],
    matched_socket_dirs: set[str],
    as_of_ms: int,
    stale_after_ms: int,
) -> list[BrokerSessionRosterRow]:
    rows: list[BrokerSessionRosterRow] = []
    for run_dir_key, runtime in runtime_by_dir.items():
        if run_dir_key in matched_socket_dirs:
            continue
        rows.append(
            BrokerSessionRosterRow(
                row_id=f"last-known:{runtime.strategy_instance_id}:{runtime.run_id}",
                identity_type="bot",
                recency="past_last_known",
                socket_present=False,
                strategy_instance_id=runtime.strategy_instance_id,
                run_id=runtime.run_id,
                account_id=runtime.account_id,
                posture=runtime.posture,
                client_id=runtime.client_id,
                pid=runtime.pid,
                run_dir=runtime.run_dir,
                connection_state=runtime.connection_state,
                recovery_state=_runtime_recovery_state(runtime),
                connection_epoch=runtime.connection_epoch,
                last_event_ms=runtime.last_event_ms,
                as_of_ms=as_of_ms,
                attention_codes=_append_stale_attention(
                    ["GHOST_DETECTION_UNAVAILABLE"],
                    runtime=runtime,
                    as_of_ms=as_of_ms,
                    stale_after_ms=stale_after_ms,
                ),
            )
        )
    return rows


def _runtime_only_rows_when_registry_unavailable(
    *,
    runtime_by_dir: dict[str, RuntimeIndexEntry],
    matched_socket_dirs: set[str],
    as_of_ms: int,
    stale_after_ms: int,
) -> list[BrokerSessionRosterRow]:
    rows: list[BrokerSessionRosterRow] = []
    for run_dir_key, runtime in runtime_by_dir.items():
        if run_dir_key in matched_socket_dirs:
            continue
        rows.append(
            BrokerSessionRosterRow(
                row_id=f"runtime-only:{runtime.strategy_instance_id}:{runtime.run_id}",
                identity_type="bot",
                recency=(
                    "unknown"
                    if _runtime_signal_stale(runtime, as_of_ms, stale_after_ms)
                    else "current"
                ),
                socket_present=False,
                strategy_instance_id=runtime.strategy_instance_id,
                run_id=runtime.run_id,
                account_id=runtime.account_id,
                posture=runtime.posture,
                client_id=runtime.client_id,
                pid=runtime.pid,
                run_dir=runtime.run_dir,
                connection_state=runtime.connection_state,
                recovery_state=_runtime_recovery_state(runtime),
                connection_epoch=runtime.connection_epoch,
                last_event_ms=runtime.last_event_ms,
                as_of_ms=as_of_ms,
                attention_codes=_append_stale_attention(
                    ["REGISTRY_SNAPSHOT_UNAVAILABLE", "SOCKET_ATTRIBUTION_UNAVAILABLE"],
                    runtime=runtime,
                    as_of_ms=as_of_ms,
                    stale_after_ms=stale_after_ms,
                ),
            )
        )
    return rows


def _append_stale_attention(
    codes: list[BrokerSessionAttentionCode],
    *,
    runtime: RuntimeIndexEntry,
    as_of_ms: int,
    stale_after_ms: int,
) -> list[BrokerSessionAttentionCode]:
    if _runtime_signal_stale(runtime, as_of_ms, stale_after_ms):
        return [*codes, "CLIENT_SIGNAL_STALE"]
    return codes


def _runtime_signal_stale(
    runtime: RuntimeIndexEntry,
    as_of_ms: int,
    stale_after_ms: int,
) -> bool:
    return (
        runtime.last_event_ms is not None
        and stale_after_ms >= 0
        and as_of_ms - runtime.last_event_ms > stale_after_ms
    )


def _runtime_recovery_state(runtime: RuntimeIndexEntry | None) -> str | None:
    if runtime is None:
        return None
    return runtime.recovery_state or recovery_state_from_connection_state(
        runtime.connection_state
    )


def _socket_row_id(socket: GatewaySocketRow, index: int) -> str:
    pid = "unknown" if socket.pid is None else str(socket.pid)
    local = "?" if socket.local_port is None else str(socket.local_port)
    remote = "?" if socket.remote_port is None else str(socket.remote_port)
    return f"socket:{pid}:{local}:{remote}:{index}"


def _registry_id(instance: HostRunnerInstance) -> str:
    return f"{instance.strategy_instance_id}:{instance.run_id}"


def _sort_key(row: BrokerSessionRosterRow) -> tuple[int, str]:
    identity_order = {
        "orphaned_bot_socket": 0,
        "ghost": 1,
        "bot": 2,
        "system": 3,
    }
    attention_order = 0 if row.attention_codes else 1
    return (
        attention_order,
        identity_order.get(row.identity_type, 99),
        row.strategy_instance_id or row.command or row.row_id,
    )


def _normalise_optional_path(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _normalise_path(value)


def _normalise_path(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return value
