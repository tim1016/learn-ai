"""Pure transition table for IBKR connectivity recovery.

ADR 0018 separates broker link signals from our recovery response. Keep that
decision as a pure function so monitor integration and future ResumeGuard
receipt wiring share the same transition vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RecoveryState = Literal[
    "HEALTHY",
    "LINK_INTERRUPTED",
    "RESTORING",
    "SOCKET_DOWN",
    "RECONNECTING",
    "HARD_DOWN",
]

RecoverySignal = Literal[
    "link_lost",
    "restored_data_maintained",
    "restored_data_lost",
    "socket_down",
    "wait_expired",
    "probe_failed",
    "reconnect_started",
    "reconnect_succeeded",
    "reconnect_failed",
    "reconnect_exhausted",
    "recovery_succeeded",
    "recovery_failed",
]


@dataclass(frozen=True)
class RecoveryTransition:
    state: RecoveryState
    should_reconnect: bool = False
    should_run_recovery: bool = False
    terminal: bool = False


def transition_recovery_state(
    current: RecoveryState,
    signal: RecoverySignal,
) -> RecoveryTransition:
    """Return the next recovery state and side-effect hints.

    The function is deliberately permissive for restore/success signals from
    any state: the monitor observes asynchronous IBKR callbacks and operator
    actions, so a clean restore should always collapse back to HEALTHY.
    """
    if signal == "link_lost":
        return RecoveryTransition(state="LINK_INTERRUPTED")
    if signal == "restored_data_maintained":
        return RecoveryTransition(state="HEALTHY")
    if signal == "restored_data_lost":
        return RecoveryTransition(state="RESTORING", should_run_recovery=True)
    if signal in {"socket_down", "wait_expired", "probe_failed"}:
        return RecoveryTransition(state="SOCKET_DOWN", should_reconnect=True)
    if signal == "reconnect_started":
        return RecoveryTransition(state="RECONNECTING")
    if signal == "reconnect_succeeded":
        return RecoveryTransition(state="RESTORING", should_run_recovery=True)
    if signal == "reconnect_failed":
        return RecoveryTransition(state="SOCKET_DOWN", should_reconnect=True)
    if signal == "reconnect_exhausted":
        return RecoveryTransition(state="HARD_DOWN", terminal=True)
    if signal == "recovery_succeeded":
        return RecoveryTransition(state="HEALTHY")
    if signal == "recovery_failed":
        return RecoveryTransition(state="SOCKET_DOWN", should_reconnect=True)
    return RecoveryTransition(state=current)


__all__ = [
    "RecoverySignal",
    "RecoveryState",
    "RecoveryTransition",
    "transition_recovery_state",
]
