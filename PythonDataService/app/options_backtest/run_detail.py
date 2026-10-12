"""Reads of a saved run that need one consistent view: its trades, one of its sessions, and two runs side by side (#3030).

Each function opens one read-only transaction at repeatable-read isolation, so everything it returns is of the same
run in the same state: a trade count and the page of trades beside it cannot disagree because a run was deleted
between the two questions.

Only a complete run has sessions and trades, so every function refuses a run that is not complete. The order of the
refusals is the same everywhere: the run is missing (``RunNotFound``), the run is not complete (``RunNotComplete``),
the session or leg asked about is not in the run (``SessionNotInRun``, ``LegNotInRun``). A route turns each into its
status; nothing here knows HTTP.

A run whose stored setup no longer validates still opens (see ``saved_runs.RunHeader``). What reads stored rows
answers all the same: the trades, one session, the export. What needs the setup says so: the legs a trade filter can
name come from it, so with no setup a leg filter is judged by the stored trades alone; two runs' settings cannot be
compared without both setups, which is ``TwoRunsSetupCannotBeRead``.

The decisions that need no database (is the run complete, is the leg one the setup has, may these two runs be compared)
are functions of the headers already read: ``require_a_complete_run``, ``require_the_leg`` and ``judge_pair``.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.options_backtest import saved_runs
from app.options_backtest.saved_runs import IsLive, RunHeader, SessionLine, StoredSession, StoredTrade


class RunNotFound(LookupError):
    """No run has that id. ``role`` is ``a`` or ``b`` when two runs were asked for to be compared."""

    def __init__(self, run_id: int, role: str | None = None) -> None:
        self.run_id = run_id
        self.role = role
        super().__init__(f"no run has the id {run_id}")


class RunNotComplete(RuntimeError):
    """The run exists and has not finished successfully, so it has no sessions or trades to read."""

    def __init__(self, run_id: int, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"run {run_id} is {status}, and only a complete run has trades and sessions")


class SessionNotInRun(LookupError):
    """The run did not test a session that opened at that instant."""

    def __init__(self, run_id: int, session_open_ms: int) -> None:
        self.run_id = run_id
        self.session_open_ms = session_open_ms
        super().__init__(f"run {run_id} did not test a session that opened at {session_open_ms}")


class LegNotInRun(LookupError):
    """The run's setup has no leg of that id."""

    def __init__(self, run_id: int, leg_id: str) -> None:
        self.run_id = run_id
        self.leg_id = leg_id
        super().__init__(f"run {run_id} has no leg {leg_id!r}")


class RunsNotComplete(RuntimeError):
    """Two runs were asked to be compared and one or both are not complete: each, with its place and its status."""

    def __init__(self, runs: tuple[tuple[str, int, str], ...]) -> None:
        self.runs = runs
        super().__init__("; ".join(f"run {run_id} ({role}) is {status}" for role, run_id, status in runs))


class TwoRunsSetupCannotBeRead(RuntimeError):
    """Two runs were asked to be compared and a stored setup no longer validates: each, with its place and its reason.

    Not ``saved_runs.RunSetupCannotBeRead``, which is about one run whose result is to be rebuilt.
    """

    def __init__(self, runs: tuple[tuple[str, int, str], ...]) -> None:
        self.runs = runs
        super().__init__(
            "; ".join(
                f"the stored setup of run {run_id} ({role}) can no longer be read: {reason}"
                for role, run_id, reason in runs
            )
        )


class RunsOfDifferentUnderlyings(RuntimeError):
    """Two runs were asked to be compared and they trade different underlyings."""

    def __init__(self, a: tuple[int, str], b: tuple[int, str]) -> None:
        self.a = a
        self.b = b
        super().__init__(f"run {a[0]} (a) trades {a[1]} and run {b[0]} (b) trades {b[1]}")


@dataclass(frozen=True)
class TradePage:
    """One page of a run's leg trades, and where the rest are."""

    run_id: int
    # Every trade that matches the filters, across all pages.
    total: int
    offset: int
    limit: int
    trades: tuple[StoredTrade, ...]

    @property
    def next_offset(self) -> int | None:
        """The ``offset`` to ask for the next page; ``None`` when this page reaches the last trade."""
        reached = self.offset + len(self.trades)
        return reached if reached < self.total else None


def require_a_complete_run(run_id: int, header: RunHeader | None) -> RunHeader:
    """The header of a run that exists and is complete; ``RunNotFound`` or ``RunNotComplete`` (naming its status)."""
    if header is None:
        raise RunNotFound(run_id)
    if header.record.status != "complete":
        raise RunNotComplete(run_id, header.record.status)
    return header


def require_the_leg(header: RunHeader, leg_id: str | None) -> None:
    """``LegNotInRun`` unless ``leg_id`` is none or a leg of the run's setup.

    A run whose setup can no longer be read cannot say which legs it had, so the filter is let through: the page of
    trades then holds the stored trades of that leg, and an empty one says only that none was stored.
    """
    if leg_id is None or header.setup is None:
        return
    if leg_id not in {leg.leg_id for leg in header.setup.legs}:
        raise LegNotInRun(header.record.id, leg_id)


async def read_trade_page(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    is_live: IsLive,
    session_open_ms: int | None,
    leg_id: str | None,
    limit: int,
    offset: int,
) -> TradePage:
    """A page of the run's leg trades, in order, narrowed to a session and/or a leg when those are given.

    A session the run did not test, or a leg its setup does not have, is refused and not answered with an empty page:
    an empty page would read as "that session made no trade".
    """
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        header = require_a_complete_run(run_id, await saved_runs.get_run_header(conn, run_id, is_live=is_live))
        if session_open_ms is not None and not await saved_runs.has_session(conn, run_id, session_open_ms):
            raise SessionNotInRun(run_id, session_open_ms)
        require_the_leg(header, leg_id)
        total = await saved_runs.count_trades(conn, run_id, session_open_ms=session_open_ms, leg_id=leg_id)
        trades = await saved_runs.read_trades(
            conn, run_id, session_open_ms=session_open_ms, leg_id=leg_id, limit=limit, offset=offset
        )
    return TradePage(run_id=run_id, total=total, offset=offset, limit=limit, trades=trades)


async def read_every_trade(conn: asyncpg.Connection, run_id: int, *, is_live: IsLive) -> tuple[StoredTrade, ...]:
    """Every leg trade of the run, in order, with no page and no limit: what an export holds."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        require_a_complete_run(run_id, await saved_runs.get_run_header(conn, run_id, is_live=is_live))
        return await saved_runs.read_trades(conn, run_id)


@dataclass(frozen=True)
class SessionRecord:
    """One session of a complete run as stored, with the run it belongs to."""

    header: RunHeader
    session: StoredSession


async def read_session_record(
    conn: asyncpg.Connection, run_id: int, session_open_ms: int, *, is_live: IsLive
) -> SessionRecord:
    """One session of a complete run with its trades and events, as stored."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        header = require_a_complete_run(run_id, await saved_runs.get_run_header(conn, run_id, is_live=is_live))
        session = await saved_runs.read_session(conn, run_id, session_open_ms)
    if session is None:
        raise SessionNotInRun(run_id, session_open_ms)
    return SessionRecord(header=header, session=session)


@dataclass(frozen=True)
class ComparisonInputs:
    """Two complete runs of one underlying: their records and setups, and the lines of their sessions."""

    a: RunHeader
    b: RunHeader
    a_lines: tuple[SessionLine, ...]
    b_lines: tuple[SessionLine, ...]


def judge_pair(a_id: int, a: RunHeader | None, b_id: int, b: RunHeader | None) -> tuple[RunHeader, RunHeader]:
    """Both headers when the two runs may be compared, else the first reason they may not.

    ``RunNotFound`` for the first of the two that is missing (``a`` before ``b``); ``RunsNotComplete`` naming every one
    that is not complete; ``TwoRunsSetupCannotBeRead`` naming every one whose stored setup no longer validates (its
    settings cannot be compared without it); ``RunsOfDifferentUnderlyings`` for a pair that trade different
    underlyings. The same run twice is allowed and compares equal to itself.
    """
    if a is None:
        raise RunNotFound(a_id, "a")
    if b is None:
        raise RunNotFound(b_id, "b")
    pair = (("a", a_id, a), ("b", b_id, b))
    not_complete = tuple(
        (role, run_id, header.record.status) for role, run_id, header in pair if header.record.status != "complete"
    )
    if not_complete:
        raise RunsNotComplete(not_complete)
    unreadable = tuple(
        (role, run_id, header.setup_cannot_open_reason or "") for role, run_id, header in pair if header.setup is None
    )
    if unreadable:
        raise TwoRunsSetupCannotBeRead(unreadable)
    if a.record.underlying != b.record.underlying:
        raise RunsOfDifferentUnderlyings((a_id, a.record.underlying), (b_id, b.record.underlying))
    return a, b


async def read_comparison_inputs(
    conn: asyncpg.Connection, a_id: int, b_id: int, *, is_live: IsLive
) -> ComparisonInputs:
    """What comparing run ``a_id`` with run ``b_id`` reads, from one consistent view; refused as ``judge_pair`` says."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        first = await saved_runs.get_run_header(conn, a_id, is_live=is_live)
        second = first if b_id == a_id else await saved_runs.get_run_header(conn, b_id, is_live=is_live)
        a, b = judge_pair(a_id, first, b_id, second)
        a_lines = await saved_runs.read_session_lines(conn, a_id)
        b_lines = a_lines if b_id == a_id else await saved_runs.read_session_lines(conn, b_id)
    return ComparisonInputs(a=a, b=b, a_lines=a_lines, b_lines=b_lines)
