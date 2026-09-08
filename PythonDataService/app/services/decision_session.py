"""One run's decision session: the minutes it decides on, and when it flushes.

A run either decides on the calendar's regular session or on the executing
broker's declared extended window (ADR 0059 D5.2). That is one fact, resolved
once at the run's boundary, and this is the object that carries it.

**Why an object rather than a keyword.** The window used to travel as
``ExtendedHoursWindow | None`` through fifteen signatures, and "an extended run
with no declared window" — a state Start admission already refuses — was
answered six different ways along that path: decide on nothing silently, skip
the session-close flush, offer no continuity policy, raise ``ValueError``
(twice), refuse the replay proof. :meth:`RunDecisionSession.resolve` is the one
place that judgement is made; every caller refuses loudly on ``None`` at its own
boundary, and everything downstream holds a session that cannot be incoherent.

The extended membership test is a pair of integer comparisons against
``session_authority.declared_session_bounds``, which memoises one trading day's
bounds — the predicate runs once per bar, over ledgers of up to 200k of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import DecisionSession
from app.services.decision_clock import next_trigger_function
from app.services.session_authority import declared_session_bounds, session_state_at_ms
from app.utils.timestamps import ny_datetime

if TYPE_CHECKING:
    from app.marketdata.feed import MarketDataBar
    from app.services.source_bar_ledger import RetainedSourceBar


@dataclass(frozen=True)
class RunDecisionSession:
    """Which minutes one run decides on, and the clock that schedules them."""

    kind: DecisionSession
    window: ExtendedHoursWindow | None

    def __post_init__(self) -> None:
        if (self.kind == "extended") != (self.window is not None):
            raise ValueError("an extended decision session requires the broker's declared window")

    @classmethod
    def resolve(
        cls, *, use_rth: bool, window: ExtendedHoursWindow | None
    ) -> RunDecisionSession | None:
        """The run's session, or ``None`` when an extended run has no declared window.

        ``None`` is the ONE place "extended run, no declared window" is judged.
        Start admission refuses such a deploy before it can stream, so a caller
        that still sees ``None`` is looking at a state error and must refuse
        loudly — never decide on nothing, and never skip a flush, quietly.
        """
        if use_rth:
            return cls(kind="rth", window=None)
        return None if window is None else cls(kind="extended", window=window)

    def includes(self, bar: MarketDataBar | RetainedSourceBar) -> bool:
        """Whether this run decides on ``bar``.

        A bar is filtered by its **open** (``start_ms``) and shaped by its
        **close** (``end_ms``): the session that produced the bar is the one it
        opened in, while the order it drives is priced and placed at the instant
        the decision exists. ``program_leg.shape_program_leg`` reads the close
        for exactly that reason; the two are deliberately different instants.

        A regular-hours run keeps trusting the feed's label (ruling R3 — the
        feed owns bar labelling, and labelled test bars carry synthetic
        timestamps). An extended run compares the bar's open against the
        declared window's memoised bounds for that trading day, which is the
        same verdict as resolving the instant's phase into PRE/RTH/POST and
        several hundred times cheaper.
        """
        if self.kind == "rth":
            return bar.session_phase == "RTH"
        bounds = declared_session_bounds(ny_datetime(bar.start_ms).date(), self.window)
        return bounds is not None and bounds.open_ms <= bar.start_ms < bounds.close_ms

    def includes_instant(self, now_ms: int) -> bool:
        """Whether ``now_ms`` falls inside this run's decision session.

        The instant-based twin of :meth:`includes`, for a minute no bar was ever
        assembled for — the continuity floor asks about exactly those. A
        regular-hours run resolves the phase through the calendar (there is no
        label to trust); an extended run compares against the same memoised
        bounds the bar filter uses, so the floor and the filter can never
        disagree about which minutes matter.
        """
        if self.kind == "rth":
            return session_state_at_ms(now_ms=now_ms).phase == "RTH"
        bounds = declared_session_bounds(ny_datetime(now_ms).date(), self.window)
        return bounds is not None and bounds.open_ms <= now_ms < bounds.close_ms

    def close_ms(self, session_date: date) -> int:
        """The instant at which the run force-flushes ``session_date``'s last bucket."""
        if self.kind == "rth":
            return session_close_ms_utc(session_date)
        bounds = declared_session_bounds(session_date, self.window)
        if bounds is None:
            raise ValueError(f"{session_date.isoformat()} is not a trading day")
        return bounds.close_ms

    def next_trigger_function(self, timeframe_ms: int) -> Callable[[int], int]:
        """Bind this session's clock into the callable the continuity loop schedules against."""
        return next_trigger_function(timeframe_ms, window=self.window)


__all__ = ["RunDecisionSession"]
