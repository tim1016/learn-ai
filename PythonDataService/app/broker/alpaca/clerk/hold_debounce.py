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

**Sampling contract: every observation is a fresh pull.** The providers
behind :func:`~app.broker.alpaca.clerk.stream_health.sample_channels` are
closures that recompute on every call, and an absent provider reports
``unknown`` rather than returning a cached reading. There is therefore no
such thing here as a replayed or out-of-order sample, and this table does
not carry the machinery to detect one: no observation timestamp on the
sample, no last-counted timestamp in the state.

That machinery existed and could never fire. The only channels whose
``observed_at_ms`` ever freezes are the already-broken ones -- a
disconnected channel reports ``connection_changed_at_ms``, when it *broke*
-- while a healthy reading always carries a current timestamp. A freshness
window over it could therefore never prevent a false release, only a true
raise: an outage simply aged out of being actionable. Keyed on a wall clock
it would also have *suppressed* a legitimate sample across an NTP step
backwards. If a provider is ever added that can hand back a cached reading,
it owes a monotonic revision on the sample and this table owes the check
back; the timestamp it used to carry was never that revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Raise only after this many consecutive unhealthy observations; release on
# the first healthy one. Asymmetric deliberately: a false raise pauses a
# working account, a false release lets an order at a dead channel. We pay
# two ticks of latency to avoid the former and none to get out of the latter.
RAISE_AFTER_CONSECUTIVE_UNHEALTHY = 2

type HoldSampleVerdict = Literal["healthy", "unhealthy", "unknown"]
type HoldSyncAction = Literal["raise", "release", "none"]


@dataclass(frozen=True)
class HoldDebounceState:
    """Process-local debounce progress.

    Deliberately not persisted: #1777 fixes the restart semantics as
    "hold survives (it is fold-derived), counter resets". A restart
    therefore costs one healthy sample to release, or two unhealthy ones
    to re-raise.
    """

    consecutive_unhealthy: int = 0


@dataclass(frozen=True)
class HoldDebounceStep:
    state: HoldDebounceState
    action: HoldSyncAction


def advance_hold_debounce(
    state: HoldDebounceState,
    verdict: HoldSampleVerdict,
) -> HoldDebounceStep:
    """Fold one observation into the debounce and say what to do about it.

    ``raise``/``release`` are *assertions*, not deltas: a persisting
    outage re-asserts ``raise`` on every sample. Suppressing the duplicate
    write is the ledger's job (``raise_account_hold`` appends only when
    the envelope changes), which keeps this table from having to
    know what was already written.
    """
    if verdict == "unknown":
        # A provider that could not produce an observation proves nothing
        # in either direction; hold the line.
        return HoldDebounceStep(state=state, action="none")

    if verdict == "healthy":
        return HoldDebounceStep(
            state=HoldDebounceState(consecutive_unhealthy=0),
            action="release",
        )

    consecutive_unhealthy = state.consecutive_unhealthy + 1
    return HoldDebounceStep(
        state=HoldDebounceState(consecutive_unhealthy=consecutive_unhealthy),
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
    "HoldSampleVerdict",
    "HoldSyncAction",
    "advance_hold_debounce",
]
