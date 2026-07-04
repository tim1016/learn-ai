"""Pure reconciler for the broker session mirror.

The host daemon proves socket existence; the process registry claims which
bot owns a process; run artifacts provide durable attribution. This module
does the join without shelling out or touching the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.ibkr.models import IbkrConnectionHealth
from app.operator.notices.broker_session import orphaned_socket_notice
from app.schemas.broker_session import (
    BrokerSessionAttentionCode,
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
    posture: str | None = None
    connection_epoch: int | None = None
    last_event_ms: int | None = None


_LIVE_REGISTRY_STATES = {
    HostRunnerProcessState.running,
    HostRunnerProcessState.stopping,
}


def reconcile_broker_session_roster(
    *,
    socket_rows: list[GatewaySocketRow],
    registry_snapshot: HostRunnerInstancesStatus | None,
    runtime_index: dict[str, RuntimeIndexEntry],
    data_plane_health: IbkrConnectionHealth | None,
    as_of_ms: int,
    socket_probe_available: bool = True,
    stale_after_ms: int = 25_000,
) -> list[BrokerSessionRosterRow]:
    """Reconcile intent, registry claim, and OS socket truth into roster rows."""

    runtime_by_dir = {_normalise_path(key): value for key, value in runtime_index.items()}
    registry_instances = registry_snapshot.instances if registry_snapshot is not None else []
    registry_by_pid = _registry_by_pid(registry_instances)
    registry_by_dir = _registry_by_run_dir(registry_instances)

    matched_registry_ids: set[str] = set()
    rows: list[BrokerSessionRosterRow] = []

    for index, socket in enumerate(socket_rows):
        run_dir_key = _normalise_optional_path(socket.run_dir)
        runtime = runtime_by_dir.get(run_dir_key) if run_dir_key is not None else None
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
            elif not _registry_claims_live_socket(registry, socket):
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

    if data_plane_health is not None and data_plane_health.connected:
        rows.append(_data_plane_row(data_plane_health, as_of_ms))

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


def _data_plane_row(
    health: IbkrConnectionHealth,
    as_of_ms: int,
) -> BrokerSessionRosterRow:
    return BrokerSessionRosterRow(
        row_id=f"system:data-plane:{health.client_id}",
        identity_type="system",
        recency="current",
        socket_present=True,
        account_id=health.account_id,
        client_id=health.client_id,
        remote_host=health.host,
        remote_port=health.port,
        connection_state=health.connection_state,
        last_event_ms=health.last_transition_ms,
        as_of_ms=as_of_ms,
    )


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
