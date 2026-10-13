"""How far the cash provably carried an options backtest run (#3108, PRD #3095).

Formula: with ``S`` the run's starting cash and, after a money step, ``c`` the cash and ``k`` the collateral held
    (``cover_book.collateral``), in whole dollars and exact:
    free cash                  f = c - k = S + F - k, where F = c - S is the cash that has come in or gone out
    cash this path needed      = max(0, max over the stretch of (k - F))
    a closing group is         funded    when c_after >= k_after
                               unfunded  when c_after <  k_after
                               and it relied on its own relief when it bought back a sold leg while a bought
                               contract of that kind and expiry stayed held and (c_before - c_after) > (c_before - k_before)
    capital-invalid event      = the first unfunded closing group, or the first session that ended with a position
                                 marked and not closed
    funded continuously through session N of M: N is the last session that ended before the event
Reference: the owner's rules in #3095 and #3108 (PRD revision 2 of 2026-10-12): the totals keep every session at its
  mark, as before, and capital evidence is a separate verdict. Alpaca's support page on buying to close a short
  option, which "does not always provide margin relief to fund the closing trade itself":
  https://alpaca.markets/support/why-cant-i-buy-to-close-my-short-option-the-platform-says-i-have-insufficient-options-buying-power-but-im-trying-to-close-a-position-not-open-one
Canonical implementation: this file.
Validated against: tests/options_backtest/test_capital_evidence.py, whose expectations are worked by hand from the
  rules, and through a replay in tests/options_backtest/test_run_capital.py.

Why a second verdict. The totals keep a position left open at the end of a session at its mark and settle the mark
into the next session's cash. That is right for what a setup made, and wrong as evidence that the cash could carry
it: a mark returns money no trade produced, and a close the replay always makes (a close is never refused) can
leave free cash below zero where a broker could have refused it. So capital evidence ends at the first event of
either kind. Later sessions still ran, on the stated assumption that the position was settled at its mark, and are
not continuously funded.

The stretch. It is every money step before the event: the sessions that ended before it whole, and the steps of the
event's own session that came before it (an unfunded closing group is not in it; a session that ended marked is in
it whole, since the mark is the event and no step of the session depended on it). A run with no event is all
stretch. N counts the sessions of the run in order, skipped ones too, and a session that holds the event is not
among the N that ended before it.

What is whole-run and what is stretch. The cash this path needed is worked over the stretch alone: past the event the
cash carried contains a mark, and a figure built on it would not be a fact about the path. The other four figures
are what the replay held and saw over every session it ran: collateral and the assignment obligation come from what
was held and not from cash, the least free cash is meant to show how far below zero a close took it, and the count
of closes that relied on their own relief says how often a run leaned on the broker's goodwill. They are kept
separate from one another and from the drawdown, which is marked equity's at the ends of sessions: none stands in
for another.

The cash this path needed is true of this recorded path only. It says nothing of what the setup needs: it is not a
minimum and not a safe amount. A starting cash below it would have refused an entry of this path for cash, and
when an entry of the stretch was refused for cash a larger starting cash would have let it through and traded a
different path, so the figure is then marked incomplete.

Only a closing group can be unfunded: an opening group is refused, all of it, unless the cash after it carries the
collateral of what would then be held. Every figure is an exact ``Decimal``; every time is ``int64 ms UTC``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import NamedTuple

from app.options_backtest import fills
from app.options_backtest.results import EventKind, FillGroup, MoneyStep, SessionOutcome, SessionStatus

_NOTHING = Decimal(0)


class CapitalInvalidKind(StrEnum):
    """What ended capital evidence."""

    # A closing group left the cash below the collateral of what was then held: free cash below zero.
    UNFUNDED_CLOSE = "unfunded_close"
    # A session ended with a position marked and not closed, so the next session's cash holds money a mark returned.
    POSITION_MARKED = "position_marked"


@dataclass(frozen=True)
class CapitalInvalidEvent:
    """The first event that ended capital evidence, named by its session and, for a close, its minute."""

    kind: CapitalInvalidKind
    session_open_ms: int
    # The minute of the unfunded closing group. None for a session that ended marked: the session ends at the event.
    at_ms: int | None
    # Free cash after the unfunded closing group, below zero. None for a session that ended marked.
    free_cash_after_usd: Decimal | None


@dataclass(frozen=True)
class CapitalSession:
    """What one session says of its money, and nothing else of it.

    ``refused_for_cash_at_ms`` are the minutes at which an entry was refused for want of cash: they say whether
    the cash this path needed is the whole story of what a larger starting cash would have done.
    """

    session_open_ms: int
    status: SessionStatus
    money_steps: tuple[MoneyStep, ...]
    refused_for_cash_at_ms: tuple[int, ...] = ()

    @classmethod
    def of_outcome(cls, session_open_ms: int, outcome: SessionOutcome) -> CapitalSession:
        """A replayed session as capital evidence reads it. Only a replay has its money steps: a session read back
        from storage has none, which is indistinguishable from a session that filled nothing."""
        return cls(
            session_open_ms=session_open_ms,
            status=outcome.status,
            money_steps=outcome.money_steps,
            refused_for_cash_at_ms=tuple(
                event.at_ms for event in outcome.events if event.kind is EventKind.ENTRY_REFUSED_FOR_CASH
            ),
        )


@dataclass(frozen=True)
class CapitalEvidence:
    """How far the cash provably carried a run, and each figure of its money on its own."""

    sessions_tested: int
    # N: the sessions that ended before the event, or all of them when there was none.
    sessions_funded_continuously: int
    # None when the cash carried the whole run.
    ended_by: CapitalInvalidEvent | None
    # The most collateral held at any money step, whole run. Nothing for a run that sold nothing.
    most_collateral_held_usd: Decimal
    # The cash this path needed over the continuously funded stretch, and whether it is incomplete.
    cash_needed_usd: Decimal
    # An entry in the stretch was refused for cash, so a larger starting cash would have traded a different path.
    cash_needed_is_incomplete: bool
    # The least free cash seen at any step, below zero included, whole run; the starting cash when nothing filled.
    least_free_cash_usd: Decimal
    # The largest assignment obligation held at any step, whole run: gross, and not a loss.
    largest_assignment_obligation_usd: Decimal
    # Closing groups that bought back a sold leg with its cover held and for more than the free cash before them.
    closes_relied_on_own_relief: int


class _End(NamedTuple):
    """Where the stretch stops: the session by its place, the first step it does not hold, and the event."""

    session_at: int
    step_at: int
    event: CapitalInvalidEvent


def _is_unfunded(step: MoneyStep) -> bool:
    return step.group is FillGroup.CLOSING and step.cash_after_usd < step.collateral_after_usd


def _relied_on_its_own_relief(step: MoneyStep) -> bool:
    """A closing group that bought back a sold leg under cover and cost more than the free cash before it."""
    if step.group is not FillGroup.CLOSING or not step.bought_back_with_cover_held:
        return False
    with localcontext(fills.EXACT):
        cost = step.cash_before_usd - step.cash_after_usd
        free_before = step.cash_before_usd - step.collateral_before_usd
    return cost > free_before


def _first_capital_invalid_event(sessions: Sequence[CapitalSession]) -> _End | None:
    for session_at, session in enumerate(sessions):
        for step_at, step in enumerate(session.money_steps):
            if _is_unfunded(step):
                with localcontext(fills.EXACT):
                    free_after = step.cash_after_usd - step.collateral_after_usd
                event = CapitalInvalidEvent(
                    CapitalInvalidKind.UNFUNDED_CLOSE, session.session_open_ms, step.at_ms, free_after
                )
                return _End(session_at, step_at, event)
        if session.status is SessionStatus.INCOMPLETE:
            event = CapitalInvalidEvent(CapitalInvalidKind.POSITION_MARKED, session.session_open_ms, None, None)
            return _End(session_at, len(session.money_steps), event)
    return None


def _stretch(sessions: Sequence[CapitalSession], end: _End | None) -> tuple[list[MoneyStep], int]:
    """The money steps before the event, and how many entries were refused for cash among them."""
    steps: list[MoneyStep] = []
    refused = 0
    for session_at, session in enumerate(sessions):
        if end is not None and session_at > end.session_at:
            break
        if end is not None and session_at == end.session_at:
            steps.extend(session.money_steps[: end.step_at])
            # An entry refused in the minute of an unfunded close was judged before that close: opening fills come
            # first in a minute. A session that ended marked is in the stretch whole.
            cutoff = end.event.at_ms
            refused += sum(1 for at_ms in session.refused_for_cash_at_ms if cutoff is None or at_ms <= cutoff)
        else:
            steps.extend(session.money_steps)
            refused += len(session.refused_for_cash_at_ms)
    return steps, refused


def capital_evidence(sessions: Sequence[CapitalSession], *, starting_cash: Decimal) -> CapitalEvidence:
    """How far the cash provably carried a run of ``sessions`` in date order that started with ``starting_cash``.

    ``sessions`` is every session of the run, skipped ones included. A run read back from storage has no money
    steps and must not be asked: ``CapitalSession.of_outcome`` is for a replay.
    """
    end = _first_capital_invalid_event(sessions)
    every_step = [step for session in sessions for step in session.money_steps]
    stretch, refused_in_the_stretch = _stretch(sessions, end)
    with localcontext(fills.EXACT):
        needed = _NOTHING
        for step in stretch:
            flow = step.cash_after_usd - starting_cash
            needed = max(needed, step.collateral_after_usd - flow)
        free_cash_seen = [starting_cash]
        for step in every_step:
            free_cash_seen.append(step.cash_before_usd - step.collateral_before_usd)
            free_cash_seen.append(step.cash_after_usd - step.collateral_after_usd)
    return CapitalEvidence(
        sessions_tested=len(sessions),
        sessions_funded_continuously=len(sessions) if end is None else end.session_at,
        ended_by=None if end is None else end.event,
        most_collateral_held_usd=max([_NOTHING, *(step.collateral_after_usd for step in every_step)]),
        cash_needed_usd=needed,
        cash_needed_is_incomplete=refused_in_the_stretch > 0,
        least_free_cash_usd=min(free_cash_seen),
        largest_assignment_obligation_usd=max([_NOTHING, *(step.obligation_after_usd for step in every_step)]),
        closes_relied_on_own_relief=sum(1 for step in every_step if _relied_on_its_own_relief(step)),
    )
