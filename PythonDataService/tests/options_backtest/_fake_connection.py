"""A stand-in for the asyncpg connection a closer look at a saved run reads through, for tests that need no database (#3030).

``saved_runs`` and ``run_detail`` are the real code: only the connection under them is replaced. It answers the few
statements they send, by the table each names, and refuses any other loudly (a double that answers whatever it is asked
hides the read it was written to watch). It keeps what the tests ask of it:

* ``in_transaction``: whether a transaction is open right now, and ``transactions``, how each was opened;
* ``log``: every statement, with the number of the transaction it ran in (``None`` outside one);
* a cursor (``cursor``), which, as asyncpg's, cannot be made outside a transaction. It hands out the trade rows ``n`` at
  a time and counts them, and can be told to fail on its Nth fetch.

``Pool(fake)`` stands in for ``with_connection``: it hands the fake to the function and records that a pooled connection
is held for the length of the call, which is what ``with_connection`` does with the real pool.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from itertools import islice
from typing import Any

from app.options_backtest import saved_runs
from app.options_backtest.saved_runs import StoredTrade
from tests.options_backtest.setup_fixtures import setup

CREATED_AT_MS = 1_791_800_000_000
FIRST_OPEN_MS = 1_791_379_800_000


def a_run_row(
    run_id: int,
    status: str = "complete",
    *,
    job_id: str | None = None,
    underlying: str = "SPY",
    setup_json: str | None = None,
    first_open_ms: int = FIRST_OPEN_MS,
    last_open_ms: int = FIRST_OPEN_MS,
    sessions_requested: int = 1,
) -> dict[str, Any]:
    """The row of the runs table ``saved_runs.get_run_header`` reads: a run of one session, stored in ``status``."""
    complete = status == "complete"
    return {
        "id": run_id,
        "created_at_ms": CREATED_AT_MS + run_id,
        "finished_at_ms": None if status == "running" else CREATED_AT_MS + run_id + 5,
        "job_id": job_id if job_id is not None else f"job-{run_id}",
        "status": status,
        "setup_name": None,
        "underlying": underlying,
        "first_session_open_ms": first_open_ms,
        "last_session_open_ms": last_open_ms,
        "sessions_requested": sessions_requested,
        "fee_policy_text": "No fees are charged.",
        "calendar_identity": "NYSE",
        "calendar_package_version": "1",
        "failure_code": "DOWNLOAD_FAILED" if status == "failed" else None,
        "failure_reason": "The data could not be fetched." if status == "failed" else None,
        "failure_detail_json": "[]" if status == "failed" else None,
        "sessions_tested": 1 if complete else None,
        "sessions_traded": 0 if complete else None,
        "trades": 0 if complete else None,
        "trades_won": 0 if complete else None,
        "net_profit_usd": Decimal("0") if complete else None,
        "ending_cash_usd": Decimal("10000") if complete else None,
        "setup_json": setup().model_dump_json() if setup_json is None else setup_json,
    }


def a_skipped_session_row(session_open_ms: int = FIRST_OPEN_MS) -> dict[str, Any]:
    """A session skipped on its window, as stored: no trades, no events and no pins, so serving it reads no lake file."""
    return {
        "session_open_ms": session_open_ms,
        "session_close_ms": session_open_ms + 390 * 60_000,
        "status": "skipped",
        "skip_reason": "window_does_not_fit",
        "cash_before_usd": Decimal("10000"),
        "cash_after_usd": Decimal("10000"),
        "cash_change_usd": Decimal("0"),
        "profit_usd": Decimal("0"),
        "underlying_sha256": None,
        "listing_sha256": None,
        "option_days_json": "[]",
        "events_json": json.dumps([]),
    }


def _the_columns_of(statement: str) -> list[str]:
    columns = re.search(r"\((.*?)\)\s*VALUES", statement, re.S)
    assert columns is not None
    return [name.strip() for name in columns.group(1).split(",")]


def _as_asyncpg_returns_it(value: Any) -> Any:
    """A whole number with zeros at its end in the exponent form asyncpg gives a NUMERIC: 10000 as 1E+4."""
    if isinstance(value, Decimal) and value.is_finite() and value != 0 and value.as_tuple().exponent == 0:
        return value.normalize()
    return value


def a_trade_row(stored: StoredTrade, run_id: int = 7) -> dict[str, Any]:
    """The row the store keeps for a trade, as ``SELECT *`` gives it back: every number in the form asyncpg returns."""
    values = saved_runs._trade_row(run_id, stored.session_open_ms, stored.trade_number, stored.trade)
    return dict(zip(_the_columns_of(saved_runs._INSERT_TRADE), map(_as_asyncpg_returns_it, values), strict=True))


def many_trade_rows(stored: StoredTrade, count: int, run_id: int = 7) -> Iterator[dict[str, Any]]:
    """``count`` rows of a trade that differ in their trade number alone, made one at a time as they are asked for."""
    template = a_trade_row(stored, run_id)
    for number in range(count):
        yield {**template, "trade_number": number}


def a_line_row(session_open_ms: int, status: str, profit: str, trades: int) -> dict[str, Any]:
    """A row of ``saved_runs.read_session_lines``: what a session says of itself for a comparison."""
    return {"session_open_ms": session_open_ms, "status": status, "profit_usd": Decimal(profit), "trades": trades}


class FakeCursor:
    """What ``await connection.cursor(...)`` gives: rows that ``fetch`` hands out ``n`` at a time, from where it stood.

    The rows are taken from an iterator as they are asked for, so a test of a very large run never holds it whole.
    """

    def __init__(self, connection: FakeConnection, rows: Iterator[dict[str, Any]]) -> None:
        self._connection = connection
        self._rows = rows
        # The ``n`` of every fetch, and how many rows each gave.
        self.asked: list[int] = []
        self.gave: list[int] = []

    async def fetch(self, n: int) -> list[dict[str, Any]]:
        self._connection.log.append(("cursor fetch", self._connection.transaction_number))
        self.asked.append(n)
        if self._connection.fail_at_fetch == len(self.asked):
            raise ConnectionError("the connection to the database was lost")
        taken = list(islice(self._rows, n))
        self.gave.append(len(taken))
        return taken


class FakeConnection:
    def __init__(
        self,
        runs: Sequence[dict[str, Any]] = (),
        *,
        sessions: Sequence[dict[str, Any]] = (),
        lines: dict[int, Sequence[dict[str, Any]]] | None = None,
        trade_rows: Iterable[dict[str, Any]] | Callable[[], Iterable[dict[str, Any]]] = (),
        fail_at_fetch: int | None = None,
    ) -> None:
        self.runs = {row["id"]: row for row in runs}
        self.sessions = {row["session_open_ms"]: row for row in sessions}
        self.lines = lines or {}
        self._trade_rows = trade_rows
        self.fail_at_fetch = fail_at_fetch
        self.cursors: list[FakeCursor] = []
        self.in_transaction = False
        self.transaction_number: int | None = None
        # How each transaction was opened: ``(isolation, readonly)``.
        self.transactions: list[tuple[str | None, bool]] = []
        self.log: list[tuple[str, int | None]] = []

    @asynccontextmanager
    async def transaction(self, *, isolation: str | None = None, readonly: bool = False) -> AsyncIterator[None]:
        assert not self.in_transaction, "a transaction is already open on this connection"
        self.transactions.append((isolation, readonly))
        self.in_transaction = True
        self.transaction_number = len(self.transactions)
        try:
            yield
        finally:
            self.in_transaction = False
            self.transaction_number = None

    def trade_rows(self) -> Iterable[dict[str, Any]]:
        return self._trade_rows() if callable(self._trade_rows) else self._trade_rows

    def _ran(self, sql: str) -> None:
        self.log.append((sql.split("FROM", 1)[-1].split()[0] if "FROM" in sql else sql[:30], self.transaction_number))

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self._ran(sql)
        if "FROM research_options_backtest_runs WHERE id = $1" in sql:
            return self.runs.get(args[0])
        if "FROM research_options_backtest_run_sessions WHERE run_id = $1 AND session_open_ms = $2" in sql:
            return self.sessions.get(args[1])
        raise AssertionError(f"the fake connection has no answer for: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._ran(sql)
        if "SELECT count(*) FROM research_options_backtest_run_trades" in sql:
            return len(self.trade_rows())
        if "SELECT EXISTS (SELECT 1 FROM research_options_backtest_run_sessions" in sql:
            return args[1] in self.sessions
        raise AssertionError(f"the fake connection has no answer for: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._ran(sql)
        # The lines' statement counts trades in a sub-select, so it is told apart first.
        if "FROM research_options_backtest_run_sessions s WHERE s.run_id = $1" in sql:
            return list(self.lines.get(args[0], ()))
        if sql.startswith("SELECT * FROM research_options_backtest_run_trades"):
            return list(self.trade_rows())
        raise AssertionError(f"the fake connection has no answer for: {sql}")

    async def cursor(self, sql: str, *args: Any) -> FakeCursor:
        assert self.in_transaction, "a cursor cannot be made outside of a transaction (asyncpg refuses it)"
        self._ran(sql)
        if not sql.startswith("SELECT * FROM research_options_backtest_run_trades"):
            raise AssertionError(f"the fake connection has no cursor for: {sql}")
        made = FakeCursor(self, iter(self.trade_rows()))
        self.cursors.append(made)
        return made


class Pool:
    """``with_connection`` over a fake connection. ``held`` is true for exactly as long as a call to ``fn`` is running."""

    def __init__(self, fake: FakeConnection) -> None:
        self.fake = fake
        self.held = False

    async def __call__(self, fn: Callable[..., Awaitable[Any]], /, *args: Any, **kwargs: Any) -> Any:
        self.held = True
        try:
            return await fn(self.fake, *args, **kwargs)
        finally:
            self.held = False
