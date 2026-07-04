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
    BrokerSafetyVerdict,
    IbkrConnectionHealth,
)
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
    if monitor is None:
        return base.model_copy(update={"safety_verdict": safety_verdict})

    if getattr(monitor, "is_hard_down", False) is True:
        state: BrokerConnectionState = "hard_down"
    elif monitor.is_attempting:
        state: BrokerConnectionState = "reconnecting"
    elif getattr(monitor, "is_recovering", False) is True:
        state = "recovering"
    else:
        state = base.connection_state
    return base.model_copy(
        update={
            "connection_state": state,
            "reconnect_attempt": monitor.current_attempt or None,
            "successful_reconnect_count": monitor.successful_reconnect_count,
            "last_transition_ms": max(
                base.last_transition_ms, monitor.last_transition_ms
            ),
            "safety_verdict": safety_verdict,
        }
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
    return IbkrConnectionHealth(
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
        last_transition_ms=now_ms,
    )
