"""Held-minute mark rules for Options Golden Search risk evidence (ADR 0080, #3405).

Formula: a held leg's mark at minute t is the close of its minute bar for t, and equity is
  ``MinuteObservation.equity_usd`` = cash + Σ(signed contracts × multiplier × mark).
  ``options-held-minute-close-v1``: a leg with no bar for t has no mark, so equity at t is unavailable.
  ``options-held-minute-close-v2``: such a leg takes the close of its most recent earlier bar since its position
  opened, in the same session, on the same contract; it has no mark only if the position has traded no bar yet.
Reference: owner decision of 2026-10-21 in ADR 0080, "Missing held-minute marks"; slice Q (#3405) of #3395.
Canonical implementation: this file for which mark a minute takes; ``app/options_backtest/observation.py`` owns
  the observed facts and the equity sum, and ``replay.py`` the fills, which no mark rule touches.
Validated against: tests/research/options_golden_search/test_marks.py.

The retained observations stay the raw facts a replay published: a minute with no bar is a minute with no bar, so
the same evidence can be read under either rule and a stored v1 trace is never rewritten. A rule is applied when the
evidence is valued, session by session. A carried price is valuation only: no rule here creates a fill, a stop
level, a momentum read or a settlement, and no price crosses a session, a contract or a later minute.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest.observation import MARK_RULE_VERSION, HeldMark, MinuteObservation

MARK_RULE_V1 = MARK_RULE_VERSION
MARK_RULE_V2 = "options-held-minute-close-v2"
MarkRule = Literal["options-held-minute-close-v1", "options-held-minute-close-v2"]


@dataclass(frozen=True)
class CarriedMark:
    """One held leg's mark in one minute that took an earlier close: a leg-minute, so two legs count twice."""

    at_ms: int
    leg_id: str
    contract: OptionContract
    close: Decimal
    traded_at_ms: int
    run: int


@dataclass(frozen=True)
class SessionValuation:
    """One session's minutes with the marks a rule values them at, and the marks that were carried."""

    observations: tuple[MinuteObservation, ...]
    carried: tuple[CarriedMark, ...] = ()

    @property
    def longest_carried_run(self) -> int:
        """Most consecutive minutes any one position's mark was carried; a real bar ends a run."""
        return max((mark.run for mark in self.carried), default=0)


@dataclass(frozen=True)
class _Traded:
    contract: OptionContract
    close: Decimal
    traded_at_ms: int
    run: int


def value_session(observations: Sequence[MinuteObservation], rule: str) -> SessionValuation:
    """Value one session's minute observations under ``rule``; a session never lends a price to another."""
    if rule == MARK_RULE_V1:
        return SessionValuation(tuple(observations))
    if rule != MARK_RULE_V2:
        raise ValueError(f"unknown held-minute mark rule: {rule!r}")
    previous: dict[str, _Traded] = {}
    valued: list[MinuteObservation] = []
    carried: list[CarriedMark] = []
    for observation in observations:
        current: dict[str, _Traded] = {}
        marks: list[HeldMark] = []
        filled = False
        for mark in observation.held:
            if mark.close is not None:
                current[mark.leg_id] = _Traded(mark.contract, mark.close, observation.at_ms, 0)
                marks.append(mark)
                continue
            last = previous.get(mark.leg_id)
            if last is None or last.contract != mark.contract:
                # Nothing has traded since this position opened: the mark stays missing.
                marks.append(mark)
                continue
            current[mark.leg_id] = replace(last, run=last.run + 1)
            carried.append(CarriedMark(observation.at_ms, mark.leg_id, mark.contract, last.close, last.traded_at_ms, last.run + 1))
            marks.append(replace(mark, close=last.close))
            filled = True
        # A leg absent from this minute has no position to carry from; a later one starts afresh.
        previous = current
        valued.append(replace(observation, held=tuple(marks)) if filled else observation)
    return SessionValuation(tuple(valued), tuple(carried))
