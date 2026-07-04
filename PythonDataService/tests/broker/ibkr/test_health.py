"""Tests for the broker-health wire-payload composer.

``build_broker_health`` is the single place that knows both halves of
the connection state machine — the client's directly-observable view
and the monitor's reconnect-attempt overlay. These tests pin the
overlay rules: monitor "is_attempting" wins over the client's view,
``last_transition_ms`` is the max of both sides, monitor-free callers
get the client's view unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.health import (
    build_broker_health,
    synthetic_disconnected_health,
)
from app.broker.ibkr.models import IbkrConnectionHealth


def _fake_client_health(**overrides) -> IbkrConnectionHealth:
    base = dict(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=42,
        connected=True,
        account_id="DU1234567",
        is_paper=True,
        server_version=178,
        fetched_at_ms=1_700_000_000_000,
        connection_state="connected",
        recovery_state="HEALTHY",
        last_transition_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return IbkrConnectionHealth(**base)


def _fake_client(health: IbkrConnectionHealth) -> MagicMock:
    client = MagicMock()
    client.health = MagicMock(return_value=health)
    return client


def _fake_monitor(
    *,
    is_attempting: bool = False,
    current_attempt: int = 0,
    successful_reconnect_count: int = 0,
    last_transition_ms: int = 0,
    is_recovering: bool = False,
    is_hard_down: bool = False,
    recovery_state: str = "HEALTHY",
) -> MagicMock:
    monitor = MagicMock()
    monitor.is_attempting = is_attempting
    monitor.current_attempt = current_attempt
    monitor.successful_reconnect_count = successful_reconnect_count
    monitor.last_transition_ms = last_transition_ms
    monitor.is_recovering = is_recovering
    monitor.is_hard_down = is_hard_down
    monitor.recovery_state = recovery_state
    return monitor


def test_build_broker_health_without_monitor_returns_client_view_unchanged() -> None:
    """When the monitor singleton hasn't been installed (broker-disabled
    mode, tests), the composer is a thin pass-through over the client's
    own ``health()``."""
    client = _fake_client(_fake_client_health())

    out = build_broker_health(client, monitor=None)

    assert out.connection_state == "connected"
    assert out.recovery_state == "HEALTHY"
    assert out.reconnect_attempt is None
    assert out.successful_reconnect_count == 0


def test_build_broker_health_overlays_reconnecting_when_monitor_is_attempting() -> None:
    """The monitor's "is_attempting" wins over the client's view — even
    when the underlying socket is briefly up between retries, the cockpit
    must see ``reconnecting``, not the misleading ``connected``."""
    client = _fake_client(_fake_client_health(connection_state="connected"))
    monitor = _fake_monitor(
        is_attempting=True,
        current_attempt=3,
        successful_reconnect_count=2,
        last_transition_ms=1_700_000_005_000,
        recovery_state="RECONNECTING",
    )

    out = build_broker_health(client, monitor)

    assert out.connection_state == "reconnecting"
    assert out.recovery_state == "RECONNECTING"
    assert out.reconnect_attempt == 3
    assert out.successful_reconnect_count == 2


def test_build_broker_health_preserves_client_state_when_monitor_idle() -> None:
    """When the monitor is idle, the client's own ``soft_lost`` /
    ``disconnected`` / ``connected`` survives untouched."""
    client = _fake_client(_fake_client_health(connection_state="soft_lost"))
    monitor = _fake_monitor(
        is_attempting=False,
        successful_reconnect_count=4,
        recovery_state="LINK_INTERRUPTED",
    )

    out = build_broker_health(client, monitor)

    assert out.connection_state == "soft_lost"
    assert out.recovery_state == "LINK_INTERRUPTED"
    assert out.reconnect_attempt is None
    # Monitor's cumulative recovery count is still surfaced — operators
    # want to know how flaky the bridge has been even when it's currently
    # not mid-attempt.
    assert out.successful_reconnect_count == 4


def test_build_broker_health_overlays_recovering_after_reconnect() -> None:
    client = _fake_client(_fake_client_health(connection_state="connected"))
    monitor = _fake_monitor(
        is_attempting=False,
        is_recovering=True,
        recovery_state="RESTORING",
    )

    out = build_broker_health(client, monitor)

    assert out.connection_state == "recovering"
    assert out.recovery_state == "RESTORING"


def test_build_broker_health_overlays_hard_down_after_attempts_exhaust() -> None:
    client = _fake_client(_fake_client_health(connection_state="disconnected"))
    monitor = _fake_monitor(
        is_hard_down=True,
        last_transition_ms=1_700_000_005_000,
        recovery_state="HARD_DOWN",
    )

    out = build_broker_health(client, monitor)

    assert out.connection_state == "hard_down"
    assert out.recovery_state == "HARD_DOWN"
    assert out.last_transition_ms == 1_700_000_005_000


def test_build_broker_health_last_transition_is_max_of_both_sides() -> None:
    """``last_transition_ms`` is the max of the client's own event
    timestamp and the monitor's last attempt-boundary timestamp — whichever
    happened more recently is what the cockpit's "since" age renders."""
    client = _fake_client(_fake_client_health(last_transition_ms=1000))
    monitor = _fake_monitor(is_attempting=True, last_transition_ms=2000)

    out = build_broker_health(client, monitor)

    assert out.last_transition_ms == 2000


@pytest.mark.parametrize(
    "state,disabled,reason",
    [
        ("disconnected", False, None),
        ("disabled", True, "IBKR_BROKER_ENABLED=false"),
    ],
)
def test_synthetic_disconnected_health_factory_collapses_constructor_duplication(
    state, disabled, reason
) -> None:
    """The factory replaces three near-identical inline IbkrConnectionHealth
    constructors in ``routers/broker.py``. The fields the cockpit needs
    (state, disabled flag, reason) must reach the wire without ceremony."""
    out = synthetic_disconnected_health(
        state=state, disabled=disabled, reason=reason
    )

    assert out.connected is False
    assert out.connection_state == state
    assert out.recovery_state == (
        "SOCKET_DOWN" if state == "disconnected" else None
    )
    assert out.disabled is disabled
    assert out.reason == reason
    assert out.last_transition_ms > 0
    assert out.fetched_at_ms > 0
