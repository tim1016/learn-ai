"""Trading-session calendar for the lake, in the lake's own vocabulary.

**Not a calendar.** The canonical NYSE calendar is
``app.lean_sidecar.trading_calendar``, and it is the only place in this repo
that constructs an ``mcal`` calendar (``.claude/rules/temporal-rigor.md``,
"Calendar authority"). This module is a thin adapter over it: it answers the
same question in the shape the lake's catalog needs — a session list plus a
:class:`NonSessionRecord` for every skipped day, saying *why* it was skipped.

Until #1839 it was a second calendar, with a hand-maintained holiday list
covering 2024-2026 and an optional parser for LEAN's market-hours database.
Both are gone. The list was not merely a duplicate, it was wrong outside its
three years: over 2020-2023 it called 37 market holidays trading sessions, so
any window reaching back that far — the backfill job's whole purpose, and
inside the coverage endpoint's five-year cap — required artifacts for
Christmas Day and recorded phantom sessions in the catalog. The consequence
was visible enough that #1836 built a detector for it
(``backfill._missing_bar_failures``'s ``session_not_produced``, whose
docstring names this module as the divergent calendar); that detector now
guards against a divergence that cannot arise, which is where a detector
should end up.

The market-hours database is still bootstrapped as a Phase-0 lake artifact —
LEAN reads it off the mount and refuses to initialize without it. What no
longer happens is *this* module reading it to decide what a trading day is,
which made the lake's session set disagree with the two consumers that
already used the canonical calendar: the sidecar's coverage demand
(``lake_mount.resolve_lake_artifacts``) and the backfill job's iteration.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
"""

from __future__ import annotations

from datetime import date, timedelta

from app.data_lake.types import NonSessionRecord
from app.lean_sidecar.trading_calendar import expected_sessions


def trading_sessions_for(
    market: str,
    start_trading_date: date,
    end_trading_date: date,
) -> tuple[list[date], list[NonSessionRecord]]:
    """Return (sessions, non_sessions) for the inclusive window.

    Sessions come from the canonical calendar, unmodified. Half-day early
    closes ARE sessions (the lake stores the truncated day's full minute
    coverage); only full closures become non-sessions, which is what the
    canonical calendar already means by "not a session".

    The non-session reason is derived rather than looked up: a skipped day is
    a weekend if the date says so and a market holiday otherwise. That keeps
    exactly one source for *which* days are sessions while still recording
    the distinction the catalog wants.
    """
    if market != "usa":
        raise ValueError(f"market {market!r} not supported: the canonical calendar is NYSE")

    sessions = expected_sessions(start_trading_date, end_trading_date)
    session_dates = set(sessions)

    non_sessions: list[NonSessionRecord] = []
    current = start_trading_date
    while current <= end_trading_date:
        if current not in session_dates:
            non_sessions.append(
                NonSessionRecord(
                    market=market,
                    trading_date=current,
                    reason="weekend" if current.weekday() >= 5 else "market_holiday",
                )
            )
        current += timedelta(days=1)
    return sessions, non_sessions
