from __future__ import annotations

import pytest

from app.broker.ibkr.recovery_state_machine import (
    recovery_state_from_connection_state,
    transition_recovery_state,
)


def test_link_loss_enters_interrupted_without_reconnect_hint() -> None:
    transition = transition_recovery_state("HEALTHY", "link_lost")

    assert transition.state == "LINK_INTERRUPTED"
    assert transition.should_reconnect is False
    assert transition.should_run_recovery is False
    assert transition.terminal is False


def test_data_maintained_restore_fast_paths_to_healthy() -> None:
    transition = transition_recovery_state(
        "LINK_INTERRUPTED", "restored_data_maintained"
    )

    assert transition.state == "HEALTHY"
    assert transition.should_reconnect is False
    assert transition.should_run_recovery is False


def test_data_lost_restore_requires_recovery_callbacks() -> None:
    transition = transition_recovery_state("LINK_INTERRUPTED", "restored_data_lost")

    assert transition.state == "RESTORING"
    assert transition.should_run_recovery is True
    assert transition.should_reconnect is False


def test_wait_expiry_promotes_to_socket_down_reconnect() -> None:
    transition = transition_recovery_state("LINK_INTERRUPTED", "wait_expired")

    assert transition.state == "SOCKET_DOWN"
    assert transition.should_reconnect is True


def test_exhausted_reconnect_attempts_open_the_breaker_without_latching() -> None:
    """HARD_DOWN is the breaker's OPEN state, not a dead end.

    It used to be marked terminal, which matched a monitor that stopped
    trying once the fast ladder was spent. The monitor now probes the open
    breaker on a slow cadence, so nothing about HARD_DOWN is final.
    """
    transition = transition_recovery_state("RECONNECTING", "reconnect_exhausted")

    assert transition.state == "HARD_DOWN"
    assert transition.terminal is False


def test_failed_open_probe_keeps_the_breaker_open() -> None:
    """A failed slow probe must re-assert HARD_DOWN rather than fall back to
    SOCKET_DOWN, which would restart the fast ladder on the next tick."""
    transition = transition_recovery_state("RECONNECTING", "open_probe_failed")

    assert transition.state == "HARD_DOWN"
    assert transition.should_reconnect is False


@pytest.mark.parametrize(
    "connection_state,expected_recovery_state",
    [
        ("connected", "HEALTHY"),
        ("degraded_data_farm", "HEALTHY"),
        ("soft_lost", "LINK_INTERRUPTED"),
        ("subscriptions_stale", "RESTORING"),
        ("recovering", "RESTORING"),
        ("reconnecting", "RECONNECTING"),
        ("hard_down", "HARD_DOWN"),
        ("disconnected", "SOCKET_DOWN"),
        ("disabled", None),
        (None, None),
    ],
)
def test_projects_connection_state_into_recovery_vocabulary(
    connection_state: str | None,
    expected_recovery_state: str | None,
) -> None:
    assert (
        recovery_state_from_connection_state(connection_state)
        == expected_recovery_state
    )
