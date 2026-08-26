"""Pure debounce table for the stream-health account hold.

#1777 WP4. Finding S10: the hold was raised and released only inside
ENTER-purpose effect execution, so a single bad sample froze entries
account-wide and, on a quiet fleet, a stale hold never released at all.
The lifecycle moves to an independent fixed-cadence sync; this module is
the decision half of it.

Keeping the decision a pure function of the observation sequence is what
makes the timing promises testable over virtual time -- no clock, no
repository, no broker. Same shape as
``app/broker/ibkr/recovery_state_machine.py``, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Raise only after this many consecutive fresh unhealthy observations;
# release on the first fresh healthy one. Asymmetric deliberately: a false
# raise pauses a working account, a false release lets an order at a dead
# channel. We pay two ticks of latency to avoid the former and none to get
# out of the latter.
RAISE_AFTER_CONSECUTIVE_UNHEALTHY = 2

type HoldSampleVerdict = Literal["healthy", "unhealthy", "unknown"]
type HoldSyncAction = Literal["raise", "release", "none"]


@dataclass(frozen=True)
class HoldSample:
    """One channel-health observation, already reduced across channels.

    ``observed_at_ms`` is the provider's own observation time, not the
    sync's wall clock -- that distinction is what makes a replayed reading
    detectable.
    """

    verdict: HoldSampleVerdict
    observed_at_ms: int | None = None


@dataclass(frozen=True)
class HoldDebounceState:
    """Process-local debounce progress.

    Deliberately not persisted: #1777 fixes the restart semantics as
    "hold survives (it is fold-derived), counter resets". A restart
    therefore costs one fresh healthy sample to release, or two fresh
    unhealthy ones to re-raise.
    """

    counted_observed_at_ms: int | None = None
    consecutive_unhealthy: int = 0


@dataclass(frozen=True)
class HoldDebounceStep:
    state: HoldDebounceState
    action: HoldSyncAction


def advance_hold_debounce(
    state: HoldDebounceState,
    sample: HoldSample,
) -> HoldDebounceStep:
    """Fold one observation into the debounce and say what to do about it.

    ``raise``/``release`` are *assertions*, not deltas: a persisting
    outage re-asserts ``raise`` on every fresh sample. Suppressing the
    duplicate write is the ledger's job (``observe_account_hold`` appends
    only when the evidence identity changes), which keeps this table from
    having to know what was already written.
    """
    if sample.verdict == "unknown" or sample.observed_at_ms is None:
        # A provider that could not produce a fresh observation proves
        # nothing in either direction; hold the line.
        return HoldDebounceStep(state=state, action="none")

    counted = state.counted_observed_at_ms
    if counted is not None and sample.observed_at_ms <= counted:
        # Sample identity: a replayed or out-of-order reading is not new
        # evidence, so it can neither accumulate toward a raise nor
        # release one.
        return HoldDebounceStep(state=state, action="none")

    if sample.verdict == "healthy":
        return HoldDebounceStep(
            state=HoldDebounceState(
                counted_observed_at_ms=sample.observed_at_ms,
                consecutive_unhealthy=0,
            ),
            action="release",
        )

    consecutive_unhealthy = state.consecutive_unhealthy + 1
    return HoldDebounceStep(
        state=HoldDebounceState(
            counted_observed_at_ms=sample.observed_at_ms,
            consecutive_unhealthy=consecutive_unhealthy,
        ),
        action=(
            "raise"
            if consecutive_unhealthy >= RAISE_AFTER_CONSECUTIVE_UNHEALTHY
            else "none"
        ),
    )


__all__ = [
    "RAISE_AFTER_CONSECUTIVE_UNHEALTHY",
    "HoldDebounceState",
    "HoldDebounceStep",
    "HoldSample",
    "HoldSampleVerdict",
    "HoldSyncAction",
    "advance_hold_debounce",
]
