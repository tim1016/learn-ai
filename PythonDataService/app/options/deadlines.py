"""When an option position must be closed, and when an entry on a contract is too late.

Formula: with ``S(D)`` the session close on the New York date ``D``:
    close deadline(E)  = S(E) - expiry close lead           for a contract expiring on E
    entry cutoff(E)    = close deadline(E) - entry wait - cancel allowance - margin
    sold-leg close(D)  = S(D) - sold-leg close lead         on each session D a sold leg is held
    the close a held structure owes on D = the earlier of the two that apply to it
    entry cutoff on D for a structure = the close it would owe on D - entry wait - cancel allowance - margin
    last session close(E) = S(E), or S of the session before E when E has none
    past its last session at t  <=>  t >= last session close(E)
    the close owed at t = the close owed on the New York date of t, while t < S(that date)
    the close missed at t, for a structure still held = the earlier of
        last session close(E) - expiry close lead      for a leg past its last session at t
        S(L) - sold-leg close lead                     with a sold leg, L the last session ended by t,
                                                       when the structure was held before that instant
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

from app.lean_sidecar.trading_calendar import (
    SessionWindow,
    current_trading_session_window,
    previous_completed_session_close_ms,
    session_close_ms_utc,
    session_window_for_date,
)
from app.options.structure import ResolvedStructure
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

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
    entry_wait_ms: int = 20_000
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


def last_session_close_ms(expiry: date) -> int:
    """When a contract that expires on ``expiry`` stops trading: the close of its last session.

    Its last session is the one on its expiry date, a half day's close
    included. A date the calendar has no session on is counted back to the
    session before it: nothing traded on the date itself.
    """
    try:
        return session_close_ms_utc(expiry)
    except LookupError:
        return previous_completed_session_close_ms(et_midnight_ms(expiry))


def past_last_session(expiry: date, at_ms: int) -> bool:
    """Whether a contract that expires on ``expiry`` has stopped trading by ``at_ms``.

    From that instant nothing is bought or sold in it. What it is then worth
    is its exercise value, which no order's price states.
    """
    return at_ms >= last_session_close_ms(expiry)


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

    A date the calendar has no session on owes none: a day with no session
    has no close (ADR 0076 decision 20). The Clerk asks this of the date of
    its own clock, which need not be a session's.

    A structure with a sold leg owes one every session it is held. One holding
    a contract that expires on the session owes the expiry-day close. When
    both apply, the earlier binds: a close takes every leg, so there is
    nothing left for the later one.
    """
    owed = []
    try:
        if any(leg.contract.expiry_date == session for leg in structure.legs):
            owed.append(DueClose(CloseKind.EXPIRY_DAY, close_deadline_ms(session, terms)))
        if structure.has_sold_leg:
            owed.append(DueClose(CloseKind.SOLD_LEG, sold_leg_close_ms(session, terms)))
    except LookupError:
        # The calendar has no session on ``session``, so no close is counted back from one.
        return None
    return min(owed, key=lambda close: close.due_ms, default=None)


def entry_cutoff_on(structure: ResolvedStructure, session: date, terms: CloseTerms) -> int | None:
    """From when no entry on ``structure`` is opened on ``session``, or None when it would owe no close that day.

    An entry is opened only when its whole life ends before the close its
    structure would owe that day: the wait for its fill, the time to prove
    a cancel, and a margin. For one bought contract expiring on ``session``
    this is ``entry_cutoff_ms``. On a date the calendar has no session on
    there is no close and so no cutoff: whether an order may be sent that
    day is the session's to say, not a cutoff's.
    """
    due = close_due_on(structure, session, terms)
    if due is None:
        return None
    return due.due_ms - terms.entry_wait_ms - terms.cancel_allowance_ms - terms.entry_margin_ms


@dataclass(frozen=True, slots=True)
class ScheduledClose:
    """A close a held structure owes in one session, with that session's own open and close.

    The session is named by its open, which is how a trading date is kept
    as one instant. The close is no longer owed in that session once
    ``session_close_ms`` has come: from then on it was missed.
    """

    kind: CloseKind
    due_ms: int
    session_open_ms: int
    session_close_ms: int

    def due(self, at_ms: int) -> bool:
        """Whether the close is to be made by ``at_ms``: its time has come and its session has not ended."""
        return self.due_ms <= at_ms < self.session_close_ms


def _in_session(close: DueClose, session: SessionWindow) -> ScheduledClose:
    return ScheduledClose(close.kind, close.due_ms, session.open_ms_utc, session.close_ms_utc)


def close_owed_at(structure: ResolvedStructure, terms: CloseTerms, *, at_ms: int) -> ScheduledClose | None:
    """The close a held structure owes in the session of the New York date of ``at_ms``, or None.

    None when that date has no session, when its session has ended, and when
    the structure owes no close that day. Before the session opens it is the
    close of the session to come. Whether its time has come is
    ``ScheduledClose.due``.
    """
    session = current_trading_session_window(at_ms)
    if session is None or at_ms >= session.close_ms_utc:
        return None
    owed = close_due_on(structure, session.session_date, terms)
    return None if owed is None else _in_session(owed, session)


def _last_session_ended_by(at_ms: int) -> SessionWindow:
    """The latest session whose close is at or before ``at_ms``."""
    today = current_trading_session_window(at_ms)
    if today is not None and at_ms >= today.close_ms_utc:
        return today
    return session_window_for_date(et_date_at_ms(previous_completed_session_close_ms(at_ms)))


def close_missed_at(
    structure: ResolvedStructure, terms: CloseTerms, *, held_since_ms: int, at_ms: int
) -> ScheduledClose | None:
    """The close a structure still held at ``at_ms`` owed in a session that has ended, or None when it missed none.

    A contract still held after its last session missed its expiry-day
    close, however long ago that session was. A structure with a sold leg
    still held after a session's close missed that session's close, when it
    was already held before that close was due: an entry taken the next
    morning missed nothing. When both were missed the earlier is told.

    It says only that the close was owed and the structure is still held.
    Whether an order can still close it is ``past_last_session``'s to say.
    """
    missed: list[ScheduledClose] = []
    for expiry in sorted({leg.contract.expiry_date for leg in structure.legs}):
        if past_last_session(expiry, at_ms):
            last = session_window_for_date(et_date_at_ms(last_session_close_ms(expiry)))
            missed.append(
                _in_session(DueClose(CloseKind.EXPIRY_DAY, last.close_ms_utc - terms.expiry_close_lead_ms), last)
            )
    if structure.has_sold_leg:
        ended = _last_session_ended_by(at_ms)
        due = DueClose(CloseKind.SOLD_LEG, ended.close_ms_utc - terms.sold_leg_close_lead_ms)
        if held_since_ms < due.due_ms:
            missed.append(_in_session(due, ended))
    return min(missed, key=lambda close: close.due_ms, default=None)
