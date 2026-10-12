"""A stand-in for the asyncpg connection a closer look at a saved run reads through, for tests that need no database (#3030).

``saved_runs`` and ``run_detail`` are the real code: only the connection under them is replaced. It answers the few
statements they send, by the table each names, and refuses any other loudly (a double that answers whatever it is asked
hides the read it was written to watch). It keeps what the tests ask of it:

* ``in_transaction``: whether a transaction is open right now, and ``transactions``, how each was opened;
* ``log``: every statement, with the number of the transaction it ran in (``None`` outside one).

``Pool(fake)`` stands in for ``with_connection``: it hands the fake to the function and records that a pooled connection
is held for the length of the call, which is what ``with_connection`` does with the real pool.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

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
        "first_session_open_ms": FIRST_OPEN_MS,
        "last_session_open_ms": FIRST_OPEN_MS,
        "sessions_requested": 1,
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


def a_line_row(session_open_ms: int, status: str, profit: str, trades: int) -> dict[str, Any]:
    """A row of ``saved_runs.read_session_lines``: what a session says of itself for a comparison."""
    return {"session_open_ms": session_open_ms, "status": status, "profit_usd": Decimal(profit), "trades": trades}


class FakeConnection:
    def __init__(
        self,
        runs: Sequence[dict[str, Any]] = (),
        *,
        sessions: Sequence[dict[str, Any]] = (),
        lines: dict[int, Sequence[dict[str, Any]]] | None = None,
        trade_rows: Sequence[dict[str, Any]] | Callable[[], Sequence[dict[str, Any]]] = (),
    ) -> None:
        self.runs = {row["id"]: row for row in runs}
        self.sessions = {row["session_open_ms"]: row for row in sessions}
        self.lines = lines or {}
        self._trade_rows = trade_rows
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

    def trade_rows(self) -> Sequence[dict[str, Any]]:
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
