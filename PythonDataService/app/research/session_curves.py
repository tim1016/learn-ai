"""Runs' daily curves as one read sends them: the session closes once, each curve against them.

Formula, for a run's stored equity ``E_t`` at each session close ``t`` and
its starting capital ``C``:
  * cumulative return ``r_t = E_t / C − 1``;
  * fall from peak ``d_t = E_t / max_{s ≤ t} E_s − 1``; undefined, and read
    as none, while the peak is not above 0;
  * deepest fall ``min_t d_t``, at a session close: the engine's
    ``max_drawdown_pct`` reads every bar and can be deeper.
``sessions_ms`` is every session close any of the read's runs scored, in
order. A curve's values run from ``first_session``, an index into that
list, to the run's last session; a session inside that span the run did
not score is null, never a carried value.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2930 "Every run
  keeps a daily equity curve"; https://github.com/tim1016/learn-ai/issues/2943
  for the sessions sent once.
Canonical implementation: app/research/sweep/daily_equity.py for the two
  series; this file lays them against one list of sessions.
Validated against: tests/research/test_session_curves.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.research.sweep.daily_equity import cumulative_return_series, drawdown_series


def _along(at: Sequence[int], values: Sequence[float]) -> list[float | None]:
    """``values`` laid from the first of the session indexes ``at`` to the last, null where the run has none."""
    if not at:
        return []
    first = min(at)
    line: list[float | None] = [None] * (max(at) - first + 1)
    for index, value in zip(at, values, strict=True):
        line[index - first] = value
    return line


def session_curves(stored: Mapping[str, Sequence[tuple[int, float]]], capital: float) -> tuple[list[int], dict[str, dict[str, Any]]]:
    """The runs' curves against one list of session closes: the list, and each run's curve by its key.

    A curve is the run's cumulative return and its fall from peak from its
    first session to its last, null at a session between them it did not score.
    """
    sessions = sorted({ms for daily in stored.values() for ms, _ in daily})
    position = {ms: index for index, ms in enumerate(sessions)}
    curves: dict[str, dict[str, Any]] = {}
    for key, daily in stored.items():
        try:
            falls: list[float] | None = [value for _, value in drawdown_series(daily)]
        except ValueError:
            # The run's peak equity never rose above 0, so a fall from it has no meaning.
            falls = None
        at = [position[ms] for ms, _ in daily]
        curves[key] = {
            "first_session": min(at, default=0),
            "cumulative_return": _along(at, [value for _, value in cumulative_return_series(daily, capital)]),
            "drawdown": None if falls is None else _along(at, falls),
            "deepest_drawdown": min(falls) if falls else None,
        }
    return sessions, curves
