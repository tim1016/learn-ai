"""Pure transition table for IBKR connectivity recovery.

ADR 0018 separates broker link signals from our recovery response. Keep that
decision as a pure function so monitor integration and future ResumeGuard
receipt wiring share the same transition vocabulary.
"""

from __future__ import annotations

from typing import Final, Literal

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
    "open_probe_failed",
    "recovery_succeeded",
    "recovery_failed",
]


# The transition table itself. Every signal names its next state; a signal
# not listed leaves the state untouched. Nothing consumes side-effect hints
# -- the monitor decides its own actions from the state it lands in -- so
# this is a mapping, not a struct.
#
# Deliberately permissive for restore/success signals from any state: the
# monitor observes asynchronous IBKR callbacks and operator actions, so a
# clean restore should always collapse back to HEALTHY.
_NEXT_STATE: Final[dict[RecoverySignal, RecoveryState]] = {
    "link_lost": "LINK_INTERRUPTED",
    "restored_data_maintained": "HEALTHY",
    "restored_data_lost": "RESTORING",
    "socket_down": "SOCKET_DOWN",
    "wait_expired": "SOCKET_DOWN",
    "probe_failed": "SOCKET_DOWN",
    "reconnect_started": "RECONNECTING",
    "reconnect_succeeded": "RESTORING",
    "reconnect_failed": "SOCKET_DOWN",
    # Not terminal: HARD_DOWN is the breaker's OPEN state (ADR 0046). The
    # fast ladder is spent; the monitor keeps probing on a slow cadence.
    "reconnect_exhausted": "HARD_DOWN",
    # A failed slow probe re-asserts HARD_DOWN. Falling back to SOCKET_DOWN
    # would restart the fast ladder on the very next tick.
    "open_probe_failed": "HARD_DOWN",
    "recovery_succeeded": "HEALTHY",
    "recovery_failed": "SOCKET_DOWN",
}


def transition_recovery_state(
    current: RecoveryState,
    signal: RecoverySignal,
) -> RecoveryState:
    """Return the recovery state ``signal`` leads to from ``current``."""
    return _NEXT_STATE.get(signal, current)


def recovery_state_from_connection_state(
    connection_state: str | None,
) -> RecoveryState | None:
    """Project broker connection state into the ADR 0018 recovery vocabulary."""
    if connection_state in {"connected", "degraded_data_farm"}:
        return "HEALTHY"
    if connection_state == "soft_lost":
        return "LINK_INTERRUPTED"
    if connection_state in {"subscriptions_stale", "recovering"}:
        return "RESTORING"
    if connection_state == "reconnecting":
        return "RECONNECTING"
    if connection_state == "hard_down":
        return "HARD_DOWN"
    if connection_state == "disconnected":
        return "SOCKET_DOWN"
    return None


__all__ = [
    "RecoverySignal",
    "RecoveryState",
    "recovery_state_from_connection_state",
    "transition_recovery_state",
]
