from __future__ import annotations

from app.broker.ibkr.recovery_state_machine import transition_recovery_state


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


def test_exhausted_reconnect_attempts_are_terminal_hard_down() -> None:
    transition = transition_recovery_state("RECONNECTING", "reconnect_exhausted")

    assert transition.state == "HARD_DOWN"
    assert transition.terminal is True
