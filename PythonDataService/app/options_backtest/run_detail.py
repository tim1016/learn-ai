"""Reads of a saved run that need one consistent view: its trades, one of its sessions, and two runs side by side (#3030).

Each function opens one read-only transaction at repeatable-read isolation, so everything it returns is of the same
run in the same state: a trade count and the page of trades beside it cannot disagree because a run was deleted
between the two questions.

Only a complete run has sessions and trades, so every function refuses a run that is not complete. The order of the
refusals is the same everywhere: the run is missing (``RunNotFound``), the run is not complete (``RunNotComplete``),
the session or leg asked about is not in the run (``SessionNotInRun``, ``LegNotInRun``). A route turns each into its
status; nothing here knows HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.options_backtest import saved_runs
from app.options_backtest.saved_runs import IsLive, RunHeader, StoredSession, StoredTrade


class RunNotFound(LookupError):
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
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


async def _complete_run(conn: asyncpg.Connection, run_id: int, is_live: IsLive) -> RunHeader:
    header = await saved_runs.get_run_header(conn, run_id, is_live=is_live)
    if header is None:
        raise RunNotFound(run_id)
    if header.record.status != "complete":
        raise RunNotComplete(run_id, header.record.status)
    return header


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
        header = await _complete_run(conn, run_id, is_live)
        if session_open_ms is not None and not await saved_runs.has_session(conn, run_id, session_open_ms):
            raise SessionNotInRun(run_id, session_open_ms)
        if leg_id is not None and leg_id not in {leg.leg_id for leg in header.setup.legs}:
            raise LegNotInRun(run_id, leg_id)
        total = await saved_runs.count_trades(conn, run_id, session_open_ms=session_open_ms, leg_id=leg_id)
        trades = await saved_runs.read_trades(
            conn, run_id, session_open_ms=session_open_ms, leg_id=leg_id, limit=limit, offset=offset
        )
    return TradePage(run_id=run_id, total=total, offset=offset, limit=limit, trades=trades)


async def read_every_trade(conn: asyncpg.Connection, run_id: int, *, is_live: IsLive) -> tuple[StoredTrade, ...]:
    """Every leg trade of the run, in order, with no page and no limit: what an export holds."""
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await _complete_run(conn, run_id, is_live)
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
        header = await _complete_run(conn, run_id, is_live)
        session = await saved_runs.read_session(conn, run_id, session_open_ms)
    if session is None:
        raise SessionNotInRun(run_id, session_open_ms)
    return SessionRecord(header=header, session=session)
