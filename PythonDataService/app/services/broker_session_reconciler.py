"""Pure reconciliation for read-only IBKR socket evidence.

The retired bot registry and run-ledger attribution are intentionally absent.
The mirror classifies only host-observed sockets, current Account Clerk leases,
and the data-plane IBKR connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.broker_session import (
    BrokerSessionGlobalEvent,
    BrokerSessionRosterRow,
    GatewaySocketRow,
)
from app.schemas.live_runs import AccountClerkHealth


@dataclass(frozen=True)
class BrokerSessionReconciliationResult:
    rows: list[BrokerSessionRosterRow]
    global_events: list[BrokerSessionGlobalEvent]


def reconcile_broker_session_snapshot(
    *,
    socket_rows: list[GatewaySocketRow],
    data_plane_health: IbkrConnectionHealth | None,
    as_of_ms: int,
    account_clerks: list[AccountClerkHealth] | None = None,
    socket_probe_available: bool = True,
) -> BrokerSessionReconciliationResult:
    """Classify socket evidence without restoring bot/process attribution."""

    visible: list[GatewaySocketRow] = []
    proxy: list[GatewaySocketRow] = []
    clerk: list[GatewaySocketRow] = []
    for socket in socket_rows:
        if _is_gvproxy_socket(socket):
            proxy.append(socket)
        elif _is_current_account_clerk_socket(socket, account_clerks or []):
            clerk.append(socket)
        else:
            visible.append(socket)

    events: list[BrokerSessionGlobalEvent] = []
    if proxy:
        events.append(_infrastructure_event("GATEWAY_NETWORK_PROXY", proxy, as_of_ms))
    if clerk:
        events.append(_infrastructure_event("ACCOUNT_CLERK_BROKER_CLIENT", clerk, as_of_ms))
    if data_plane_health is not None:
        events.append(_data_plane_global_event(data_plane_health, as_of_ms))

    rows = [
        BrokerSessionRosterRow(
            row_id=_socket_row_id(socket, index),
            identity_type="ghost",
            recency="current" if socket_probe_available else "unknown",
            socket_present=True,
            pid=socket.pid,
            command=socket.command,
            run_dir=socket.run_dir,
            local_port=socket.local_port,
            remote_host=socket.remote_host,
            remote_port=socket.remote_port,
            as_of_ms=as_of_ms,
            attention_codes=["GHOST_SOCKET"],
        )
        for index, socket in enumerate(visible)
    ]
    return BrokerSessionReconciliationResult(rows=rows, global_events=events)


def _data_plane_global_event(
    health: IbkrConnectionHealth,
    as_of_ms: int,
) -> BrokerSessionGlobalEvent:
    connected = health.connection_state == "connected" and health.connected
    if connected or health.connection_state == "disabled":
        severity = "info"
    elif health.connection_state in {"degraded_data_farm", "hard_down"}:
        severity = "critical"
    elif health.connected or health.connection_state in {
        "soft_lost",
        "subscriptions_stale",
        "reconnecting",
        "recovering",
        "disconnected",
    }:
        severity = "warning"
    else:
        severity = "neutral"
    summary = (
        "The read-only data-plane IBKR client is connected."
        if connected
        else "The read-only data-plane IBKR client is not healthy."
    )
    if health.disabled:
        summary = "The read-only data-plane IBKR client is disabled."
    return BrokerSessionGlobalEvent(
        code="DATA_PLANE_BROKER_CLIENT",
        label="Data-plane broker client",
        severity=severity,
        summary=summary,
        current=health.connected,
        source="data_plane",
        observed_at_ms=health.last_transition_ms or health.fetched_at_ms or as_of_ms,
        client_id=health.client_id,
    )


def _infrastructure_event(
    code: str,
    sockets: list[GatewaySocketRow],
    as_of_ms: int,
) -> BrokerSessionGlobalEvent:
    clerk = code == "ACCOUNT_CLERK_BROKER_CLIENT"
    label = "Account Clerk broker client" if clerk else "Gateway network proxy"
    noun = "Account Clerk broker client" if clerk else "virtual-machine network proxy socket"
    summary = f"{len(sockets)} {noun}{'' if len(sockets) == 1 else 's'} observed as infrastructure."
    return BrokerSessionGlobalEvent(
        code=code,
        label=label,
        severity="info",
        summary=summary,
        current=True,
        source="network",
        observed_at_ms=as_of_ms,
    )


def _is_gvproxy_socket(socket: GatewaySocketRow) -> bool:
    return "gvproxy" in " ".join([socket.command, *socket.argv]).lower()


def _is_current_account_clerk_socket(
    socket: GatewaySocketRow,
    clerks: list[AccountClerkHealth],
) -> bool:
    identity = _account_clerk_socket_identity(socket)
    if identity is None:
        return False
    pid, account_id, generation = identity
    return any(
        item.pid == pid
        and item.account_id == account_id
        and item.generation == generation
        and item.status == "RUNNING"
        and item.lease_valid
        for item in clerks
    )


def _account_clerk_socket_identity(socket: GatewaySocketRow) -> tuple[int, str, int] | None:
    if socket.pid is None or "app.engine.live.account_clerk" not in socket.argv:
        return None
    try:
        account_id = socket.argv[socket.argv.index("--account-id") + 1]
        generation = int(socket.argv[socket.argv.index("--generation") + 1])
    except (IndexError, ValueError):
        return None
    return socket.pid, account_id, generation


def _socket_row_id(socket: GatewaySocketRow, index: int) -> str:
    pid = "unknown" if socket.pid is None else str(socket.pid)
    local = "?" if socket.local_port is None else str(socket.local_port)
    remote = "?" if socket.remote_port is None else str(socket.remote_port)
    return f"socket:{pid}:{local}:{remote}:{index}"
