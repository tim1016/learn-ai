"""Composer for the broker-health wire payload.

The cockpit binds to ``IbkrConnectionHealth.connection_state`` to decide
the link colour and detail string. That state has two sources:

* ``IbkrClient`` — observable for itself (socket up, soft-loss flag,
  account, server version, last own-event timestamp).
* ``AutoReconnectMonitor`` — owns the "is_attempting" overlay, the
  current attempt number, and the cumulative recovery count.

Neither side needs to know about the other. This module is the single
place that knows both, so the wire model can be built without leaking
ownership across the abstraction boundary.

Used by ``GET /api/broker/health`` and ``POST /connect|/reconnect``
endpoints in ``routers.broker``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.broker.ibkr.config import get_settings
from app.broker.ibkr.models import (
    BrokerConnectionState,
    BrokerHealthCondition,
    BrokerSafetyVerdict,
    IbkrConnectionHealth,
)
from app.broker.ibkr.recovery_state_machine import recovery_state_from_connection_state
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
    from app.broker.ibkr.client import IbkrClient


def build_broker_health(
    client: IbkrClient,
    monitor: AutoReconnectMonitor | None,
    *,
    safety_verdict: BrokerSafetyVerdict | None = None,
) -> IbkrConnectionHealth:
    """Compose the client's view with the monitor's overlay.

    Precedence: a monitor-driven reconnect attempt overrides the
    client's own state (``connection_state == "reconnecting"``) so the
    cockpit never claims "Connected" while the monitor is mid-attempt
    even if the socket flickered up briefly between retries.

    ``last_transition_ms`` is the max of both sides' transition stamps
    so the cockpit's "since" age reflects whichever event happened most
    recently.
    """
    base = client.health()
    operator_disconnected = (
        getattr(client, "desired_connected", True) is False and base.connection_state == "disconnected"
    )
    if monitor is None:
        return _with_condition(
            base.model_copy(update={"safety_verdict": safety_verdict}),
            operator_disconnected=operator_disconnected,
        )

    if getattr(monitor, "is_hard_down", False) is True and not operator_disconnected:
        state: BrokerConnectionState = "hard_down"
    elif monitor.is_attempting:
        state: BrokerConnectionState = "reconnecting"
    elif getattr(monitor, "is_recovering", False) is True:
        state = "recovering"
    else:
        state = base.connection_state
    derived_recovery_state = recovery_state_from_connection_state(state)
    monitor_recovery_state = monitor.recovery_state
    if operator_disconnected or (
        monitor_recovery_state == "HEALTHY" and derived_recovery_state not in {None, "HEALTHY"}
    ):
        recovery_state = derived_recovery_state
    else:
        recovery_state = monitor_recovery_state or derived_recovery_state
    return _with_condition(
        base.model_copy(
            update={
                "connection_state": state,
                "recovery_state": recovery_state,
                "reconnect_attempt": monitor.current_attempt or None,
                "successful_reconnect_count": monitor.successful_reconnect_count,
                "last_transition_ms": max(base.last_transition_ms, monitor.last_transition_ms),
                "safety_verdict": safety_verdict,
            }
        ),
        operator_disconnected=operator_disconnected,
    )


def synthetic_disconnected_health(
    *,
    state: BrokerConnectionState = "disconnected",
    disabled: bool = False,
    reason: str | None = None,
    safety_verdict: BrokerSafetyVerdict | None = None,
) -> IbkrConnectionHealth:
    """Wire-level health snapshot when no client exists yet (broker
    disabled, or operator hasn't called ``/connect`` yet).

    Single factory for the three near-identical synthetic constructors
    that used to live inline in ``routers.broker`` — keeps the
    field-by-field boilerplate in one place so adding a new field to
    the model is a one-line edit there, not three.
    """
    s = get_settings()
    now_ms = now_ms_utc()
    return _with_condition(
        IbkrConnectionHealth(
            mode=s.mode,
            host=s.host,
            port=s.port,
            client_id=s.client_id,
            connected=False,
            disabled=disabled,
            reason=reason,
            account_id=None,
            is_paper=None,
            server_version=None,
            fetched_at_ms=now_ms,
            safety_verdict=safety_verdict,
            connection_state=state,
            recovery_state=recovery_state_from_connection_state(state),
            last_transition_ms=now_ms,
        )
    )


def _with_condition(
    health: IbkrConnectionHealth,
    *,
    operator_disconnected: bool = False,
) -> IbkrConnectionHealth:
    return health.model_copy(
        update={
            "condition": _broker_health_condition(
                health,
                operator_disconnected=operator_disconnected,
            )
        }
    )


def _broker_health_condition(
    health: IbkrConnectionHealth,
    *,
    operator_disconnected: bool,
) -> BrokerHealthCondition:
    state = health.connection_state
    account = health.account_id or "unknown account"
    if state == "connected":
        account_kind = "paper" if health.is_paper is True else "live" if health.is_paper is False else "broker"
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_CONNECTED",
            severity="ok",
            title=f"Data-plane {account_kind} session connected",
            summary=(
                f"The FastAPI data-plane IBKR client is connected to {account}. "
                "This proves account-level broker evidence can refresh; per-bot proof still requires a live runtime."
            ),
        )
    if state == "soft_lost":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_SOFT_LOST",
            severity="warning",
            title="Data-plane broker feed lost",
            summary="The FastAPI data-plane client is still socket-connected, but IBKR reported a link/feed loss.",
            remediation="Wait for recovery or use Reconnect if the state does not clear.",
        )
    if state == "subscriptions_stale":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_SUBSCRIPTIONS_STALE",
            severity="warning",
            title="Data-plane subscriptions stale",
            summary="The data-plane broker session is connected, but market-data subscriptions need refresh after recovery.",
            remediation="Let recovery resubscribe streams, then refresh broker evidence.",
        )
    if state == "degraded_data_farm":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_DATA_FARM_DEGRADED",
            severity="critical",
            title="IBKR data farm degraded",
            summary="The data-plane broker session is connected, but IBKR data-farm evidence is degraded.",
            remediation="Do not rely on market-data freshness until IBKR data-farm health recovers.",
        )
    if state == "reconnecting":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_RECONNECTING",
            severity="warning",
            title="Data-plane broker reconnecting",
            summary="The FastAPI data-plane client is reconnecting to IBKR.",
            remediation="Wait for reconnect to complete before trusting refreshed account evidence.",
        )
    if state == "recovering":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_RECOVERING",
            severity="warning",
            title="Data-plane broker recovering evidence",
            summary="The data-plane broker link is back, but stream and account-evidence recovery is still running.",
            remediation="Wait for recovery to complete before submitting or reconciling.",
        )
    if state == "hard_down":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_HARD_DOWN",
            severity="critical",
            title="Data-plane broker session down",
            summary=(
                "IB Gateway/TWS may be logged in, but the FastAPI data-plane IBKR client is not connected. "
                "Account positions, connected-account identity, and reconciliation evidence cannot refresh."
            ),
            remediation="Use the IBKR Connect/Reconnect control after confirming Gateway API access is enabled.",
        )
    if state == "disabled":
        return BrokerHealthCondition(
            code="DATA_PLANE_BROKER_DISABLED",
            severity="info",
            title="Data-plane broker disabled",
            summary="The FastAPI data-plane broker client is disabled for this process.",
        )
    return BrokerHealthCondition(
        code="DATA_PLANE_BROKER_DISCONNECTED",
        severity="info" if operator_disconnected else "warning",
        title="Data-plane broker session disconnected",
        summary=(
            "The FastAPI data-plane IBKR client is disconnected by operator request."
            if operator_disconnected
            else (
                "The FastAPI data-plane IBKR client is disconnected. IB Gateway/TWS may still be logged in, "
                "but account evidence cannot refresh until this app session connects."
            )
        ),
        remediation=(
            None if operator_disconnected else "Use the IBKR Connect control to establish the data-plane session."
        ),
    )
