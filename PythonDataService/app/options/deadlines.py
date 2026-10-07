"""When an option position must be closed, and when an entry on a contract is too late.

Formula: with ``S(D)`` the session close on the New York date ``D``:
    close deadline(E)  = S(E) - expiry close lead           for a contract expiring on E
    entry cutoff(E)    = close deadline(E) - entry wait - cancel allowance - margin
    sold-leg close(D)  = S(D) - sold-leg close lead         on each session D a sold leg is held
    the close a held structure owes on D = the earlier of the two that apply to it
Reference: ADR 0076 (#2913: the deadlines an option position is held to). Every
  session close is ``app/lean_sidecar/trading_calendar.py``'s, so a half day's
  deadlines are a half day's and a holiday has none.
Canonical implementation: this file.
Validated against: tests/options/test_deadlines.py.

An expiry date is not a deadline. The expiry anchor names the date
(``app/options/expiry.py``). The last trade is counted as the session close on
that date; a product that trades past it on its last day is counted as
stopping with the session, as the resolver counts it. The broker's own
window, the earliest it may act on an expiring position, is not known here:
it is to be verified on the account, and until then nothing below is held to
it.

An entry is admitted only when its whole life fits before the close
deadline: the wait for its fill, the time to prove a cancel, and a margin.
That is why the cutoff is earlier than the deadline, and it is the lead the
resolver is given (``ResolverLimits.entry_cutoff_lead_ms``).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from enum import StrEnum

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.structure import ResolvedStructure

_MINUTE_MS = 60_000


@dataclass(frozen=True, slots=True, kw_only=True)
class CloseTerms:
    """How long before a session's close an option position is closed, and how long an entry may take.
    Set when a bot is deployed and fixed for its life."""

    # Before the session close on a held contract's expiry date.
    expiry_close_lead_ms: int = 45 * _MINUTE_MS
    # Before the close of every session a sold leg is held in. A sold leg is not held overnight.
    sold_leg_close_lead_ms: int = 10 * _MINUTE_MS
    # How long a resting entry waits for its fill before it is cancelled.
    entry_wait_ms: int = 30_000
    # For a cancel to be proven ended by a lookup of the order.
    cancel_allowance_ms: int = 30_000
    entry_margin_ms: int = 30_000

    def __post_init__(self) -> None:
        for term in fields(self):
            value = getattr(self, term.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{term.name} is a whole number of milliseconds, zero or more; got {value!r}")

    @property
    def entry_cutoff_lead_ms(self) -> int:
        """How long before the session close on a contract's expiry date an entry on it must be selected."""
        return self.expiry_close_lead_ms + self.entry_wait_ms + self.cancel_allowance_ms + self.entry_margin_ms


def close_deadline_ms(expiry: date, terms: CloseTerms) -> int:
    """When a position holding a contract that expires on ``expiry`` is closed. Raises ``LookupError`` when
    the calendar has no session that day."""
    return session_close_ms_utc(expiry) - terms.expiry_close_lead_ms


def entry_cutoff_ms(expiry: date, terms: CloseTerms) -> int:
    """From when no entry is opened on a contract that expires on ``expiry``."""
    return session_close_ms_utc(expiry) - terms.entry_cutoff_lead_ms


def sold_leg_close_ms(session: date, terms: CloseTerms) -> int:
    """When a position with a sold leg is closed on ``session``."""
    return session_close_ms_utc(session) - terms.sold_leg_close_lead_ms


class CloseKind(StrEnum):
    EXPIRY_DAY = "expiry_day_close"
    SOLD_LEG = "sold_leg_close"


@dataclass(frozen=True, slots=True)
class DueClose:
    """A close a held structure owes: the whole structure, in one order, by ``due_ms``."""

    kind: CloseKind
    due_ms: int


def close_due_on(structure: ResolvedStructure, session: date, terms: CloseTerms) -> DueClose | None:
    """The close a held structure owes on ``session``, or None when it owes none that day.

    A structure with a sold leg owes one every session it is held. One holding
    a contract that expires on the session owes the expiry-day close. When
    both apply, the earlier binds: a close takes every leg, so there is
    nothing left for the later one.
    """
    owed = []
    if any(leg.contract.expiry_date == session for leg in structure.legs):
        owed.append(DueClose(CloseKind.EXPIRY_DAY, close_deadline_ms(session, terms)))
    if structure.has_sold_leg:
        owed.append(DueClose(CloseKind.SOLD_LEG, sold_leg_close_ms(session, terms)))
    return min(owed, key=lambda close: close.due_ms, default=None)
