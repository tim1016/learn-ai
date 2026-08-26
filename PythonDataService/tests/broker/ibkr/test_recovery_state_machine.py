from __future__ import annotations

import pytest

from app.broker.ibkr.recovery_state_machine import (
    recovery_state_from_connection_state,
    transition_recovery_state,
)


@pytest.mark.parametrize(
    "current,signal,expected",
    [
        ("HEALTHY", "link_lost", "LINK_INTERRUPTED"),
        ("LINK_INTERRUPTED", "restored_data_maintained", "HEALTHY"),
        ("LINK_INTERRUPTED", "restored_data_lost", "RESTORING"),
        ("LINK_INTERRUPTED", "wait_expired", "SOCKET_DOWN"),
        ("SOCKET_DOWN", "reconnect_started", "RECONNECTING"),
        ("RECONNECTING", "reconnect_succeeded", "RESTORING"),
        ("RECONNECTING", "reconnect_failed", "SOCKET_DOWN"),
        ("RESTORING", "recovery_succeeded", "HEALTHY"),
        ("RESTORING", "recovery_failed", "SOCKET_DOWN"),
    ],
)
def test_signal_leads_to_its_recovery_state(
    current: str, signal: str, expected: str
) -> None:
    assert transition_recovery_state(current, signal) == expected  # type: ignore[arg-type]


def test_exhausted_reconnect_attempts_open_the_breaker_without_latching() -> None:
    """HARD_DOWN is the breaker's OPEN state, not a dead end (ADR 0046).

    It used to carry a `terminal` marker, matching a monitor that stopped
    trying once the fast ladder was spent. The monitor now probes the open
    breaker on a slow cadence, so nothing about HARD_DOWN is final.
    """
    assert transition_recovery_state("RECONNECTING", "reconnect_exhausted") == "HARD_DOWN"


def test_failed_open_probe_keeps_the_breaker_open() -> None:
    """A failed slow probe must re-assert HARD_DOWN rather than fall back to
    SOCKET_DOWN, which would restart the fast ladder on the next tick."""
    assert transition_recovery_state("RECONNECTING", "open_probe_failed") == "HARD_DOWN"


def test_an_unmapped_signal_leaves_the_state_untouched() -> None:
    assert transition_recovery_state("RESTORING", "not_a_signal") == "RESTORING"  # type: ignore[arg-type]


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
