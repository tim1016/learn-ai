"""Daily account-equity snapshots for Contract C4.

The broker remains the authoritative source for an account's present equity.
This append-only SQLite ledger preserves our own daily observations so a
sovereign cross-check curve accumulates without depending on broker history.
All persisted instants are integer milliseconds since Unix epoch UTC.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.broker.alpaca.paths import resolve_contained_path
from app.broker.contract.models import BrokerAccountSnapshot
from app.lean_sidecar.trading_calendar import (
    previous_completed_session_close_ms,
    session_windows_ms_utc,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

_SQLITE_BUSY_TIMEOUT_MS = 5_000
_SESSION_LOOKAHEAD_DAYS = 14
_CAPTURE_RETRY_DELAY_SECONDS = 60.0

type AccountSnapshotProvider = Callable[[], Awaitable[BrokerAccountSnapshot]]
type Sleep = Callable[[float], Awaitable[None]]
type SessionCloseResolver = Callable[[int], int]
type CompletedSessionCloseResolver = Callable[[int], int | None]


class DuplicateDailySovereignEquitySnapshotError(ValueError):
    """A daily row was already recorded for the same account and session."""


class UnapprovedAccountEquitySnapshotError(ValueError):
    """The broker answered for an account other than the one the binding pinned (#2669).

    A boot whose Alpaca did not answer starts the scheduler before the pin is
    checked; until a reconnect refuses the binding and stops it, the account
    the credentials reach may not be the approved one. Its equity is never
    recorded: the ledger is append-only, so a row written for it could never
    be taken back.
    """

    def __init__(self, *, account_id: str, expected_account_id: str) -> None:
        super().__init__(
            f"broker answered for account {account_id!r}, not the approved account "
            f"{expected_account_id!r}; no daily sovereign equity snapshot is recorded"
        )
        self.account_id = account_id
        self.expected_account_id = expected_account_id


@dataclass(frozen=True)
class DailySovereignEquitySnapshot:
    """One immutable broker-equity observation at a scheduled session close."""

    account_id: str
    session_close_ms: int
    equity: float
    observed_at_ms: int


class DailySovereignEquitySnapshotCapturer(Protocol):
    """The scheduler's narrow boundary to the C4 capture operation."""

    async def capture(self, *, session_close_ms: int) -> DailySovereignEquitySnapshot: ...


class DailySovereignEquitySnapshotStore:
    """Append-only SQLite storage for daily, account-scoped equity snapshots."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        return self._database_path

    def append(self, snapshot: DailySovereignEquitySnapshot) -> DailySovereignEquitySnapshot:
        """Persist one daily snapshot, refusing duplicate or mutable rows."""
        _validate_snapshot(snapshot)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._database_path) as connection:
            _configure_connection(connection)
            _initialize_schema(connection)
            try:
                connection.execute(
                    "INSERT INTO daily_sovereign_equity_snapshots "
                    "(account_id, session_close_ms, equity, observed_at_ms) VALUES (?, ?, ?, ?)",
                    (
                        snapshot.account_id,
                        snapshot.session_close_ms,
                        snapshot.equity,
                        snapshot.observed_at_ms,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                raise DuplicateDailySovereignEquitySnapshotError(
                    "daily sovereign equity snapshot already exists for "
                    f"account {snapshot.account_id!r} at {snapshot.session_close_ms}"
                ) from exc
        return snapshot


class DailySovereignEquitySnapshotWriter:
    """Capture broker equity and append it to the C4 SQLite ledger."""

    def __init__(
        self,
        *,
        store: DailySovereignEquitySnapshotStore,
        account_snapshot_provider: AccountSnapshotProvider,
        expected_account_id: str | None = None,
    ) -> None:
        self._store = store
        self._account_snapshot_provider = account_snapshot_provider
        self._expected_account_id = expected_account_id

    async def capture(self, *, session_close_ms: int) -> DailySovereignEquitySnapshot:
        """Read the broker's account equity and append one immutable daily row.

        ``expected_account_id`` is the binding's pinned account; a read that
        answers for any other account raises
        :class:`UnapprovedAccountEquitySnapshotError` and writes nothing --
        the same comparison the authority selection's pin check makes. With
        no pin, every account the credentials reach is recorded.
        """
        account = await self._account_snapshot_provider()
        if self._expected_account_id is not None and account.account_id != self._expected_account_id:
            raise UnapprovedAccountEquitySnapshotError(
                account_id=account.account_id, expected_account_id=self._expected_account_id
            )
        snapshot = DailySovereignEquitySnapshot(
            account_id=account.account_id,
            session_close_ms=session_close_ms,
            equity=account.equity,
            observed_at_ms=account.observed_at_ms,
        )
        return await asyncio.to_thread(self._store.append, snapshot)


class DailySovereignEquitySnapshotScheduler:
    """Run the C4 writer once after each calendar-derived NYSE close."""

    def __init__(
        self,
        *,
        writer: DailySovereignEquitySnapshotCapturer,
        clock: Clock = now_ms_utc,
        sleep: Sleep = asyncio.sleep,
        session_close_resolver: SessionCloseResolver | None = None,
        latest_completed_session_close_resolver: CompletedSessionCloseResolver | None = None,
    ) -> None:
        self._writer = writer
        self._clock = clock
        self._sleep = sleep
        self._session_close_resolver = session_close_resolver or next_nyse_session_close_ms
        self._latest_completed_session_close_resolver = (
            latest_completed_session_close_resolver or previous_completed_session_close_ms
        )
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the singleton service-lifetime scheduler."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name="daily-sovereign-equity-snapshot",
            )

    async def stop(self) -> None:
        """Cancel the pending wait and finish shutdown cleanly."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def capture_next_session_close(self) -> DailySovereignEquitySnapshot:
        """Wait for the next NYSE close, then capture its account-equity row."""
        session_close_ms = await self._wait_for_next_session_close()
        return await self._writer.capture(session_close_ms=session_close_ms)

    async def _wait_for_next_session_close(self) -> int:
        now_ms = self._clock()
        session_close_ms = self._session_close_resolver(now_ms)
        delay_ms = session_close_ms - now_ms
        if delay_ms <= 0:
            raise RuntimeError(
                "calendar returned a non-future session close: "
                f"now_ms={now_ms}, session_close_ms={session_close_ms}"
            )
        await self._sleep(delay_ms / 1_000)
        return session_close_ms

    async def _run(self) -> None:
        latest_completed_close_ms = self._latest_completed_session_close_resolver(
            self._clock()
        )
        if latest_completed_close_ms is not None:
            await self._capture_after_session_close(latest_completed_close_ms)
        while True:
            session_close_ms = await self._wait_for_next_session_close()
            await self._capture_after_session_close(session_close_ms)

    async def _capture_after_session_close(self, session_close_ms: int) -> None:
        while True:
            try:
                await self._writer.capture(session_close_ms=session_close_ms)
                return
            except DuplicateDailySovereignEquitySnapshotError:
                logger.info(
                    "daily sovereign equity snapshot already recorded",
                    extra={
                        "action": "daily_sovereign_equity_snapshot_already_recorded",
                        "session_close_ms": session_close_ms,
                    },
                )
                return
            except UnapprovedAccountEquitySnapshotError as exc:
                # A retry reads the same credentials, so it reaches the same
                # account: this close is refused, not retried.
                logger.error(
                    "daily sovereign equity snapshot refused: the broker answered for an "
                    "account the binding did not approve",
                    extra={
                        "action": "daily_sovereign_equity_snapshot_unapproved_account",
                        "session_close_ms": session_close_ms,
                        "account_id": exc.account_id,
                        "expected_account_id": exc.expected_account_id,
                    },
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "daily sovereign equity snapshot failed; retrying",
                    extra={
                        "action": "daily_sovereign_equity_snapshot_failed",
                        "session_close_ms": session_close_ms,
                    },
                )
                await self._sleep(_CAPTURE_RETRY_DELAY_SECONDS)


def next_nyse_session_close_ms(now_ms: int) -> int:
    """Return the first canonical NYSE close strictly after ``now_ms``."""
    _validate_ms(now_ms, field_name="now_ms", allow_zero=True)
    start_date = datetime.fromtimestamp(now_ms / 1_000, tz=UTC).date()
    windows = session_windows_ms_utc(
        start_date,
        start_date + timedelta(days=_SESSION_LOOKAHEAD_DAYS),
    )
    for window in windows:
        if window.close_ms_utc > now_ms:
            return window.close_ms_utc
    raise LookupError(f"no NYSE session close within {_SESSION_LOOKAHEAD_DAYS} days after {now_ms}")


def sovereign_equity_snapshot_database_path(artifacts_root: Path) -> Path:
    """Return the dedicated C4 database path beneath the Alpaca artifact root."""
    return resolve_contained_path(artifacts_root, "daily_sovereign_equity_snapshots.sqlite3")


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_sovereign_equity_snapshots (
            account_id TEXT NOT NULL CHECK (length(account_id) > 0),
            session_close_ms INTEGER NOT NULL CHECK (session_close_ms > 0),
            equity REAL NOT NULL,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms > 0),
            PRIMARY KEY (account_id, session_close_ms)
        );

        CREATE TRIGGER IF NOT EXISTS daily_sovereign_equity_snapshots_no_update
        BEFORE UPDATE ON daily_sovereign_equity_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'daily_sovereign_equity_snapshots is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS daily_sovereign_equity_snapshots_no_delete
        BEFORE DELETE ON daily_sovereign_equity_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'daily_sovereign_equity_snapshots is append-only');
        END;
        """
    )


def _validate_snapshot(snapshot: DailySovereignEquitySnapshot) -> None:
    if not snapshot.account_id.strip():
        raise ValueError("account_id must not be empty")
    _validate_ms(snapshot.session_close_ms, field_name="session_close_ms")
    _validate_ms(snapshot.observed_at_ms, field_name="observed_at_ms")
    if not math.isfinite(snapshot.equity):
        raise ValueError("equity must be finite")


def _validate_ms(value: int, *, field_name: str, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an int64 ms UTC value")
    minimum = 0 if allow_zero else 1
    if not minimum <= value <= MAX_TIMESTAMP_MS:
        raise ValueError(f"{field_name} must be an ms-UTC instant between {minimum} and {MAX_TIMESTAMP_MS}")


__all__ = [
    "DailySovereignEquitySnapshot",
    "DailySovereignEquitySnapshotCapturer",
    "DailySovereignEquitySnapshotScheduler",
    "DailySovereignEquitySnapshotStore",
    "DailySovereignEquitySnapshotWriter",
    "DuplicateDailySovereignEquitySnapshotError",
    "UnapprovedAccountEquitySnapshotError",
    "next_nyse_session_close_ms",
    "sovereign_equity_snapshot_database_path",
]
