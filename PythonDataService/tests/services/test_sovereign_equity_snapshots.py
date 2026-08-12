"""Contract C4 tests for daily, broker-independent equity snapshots."""

from __future__ import annotations

import asyncio
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.broker.contract.models import BrokerAccountSnapshot
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.services.sovereign_equity_snapshots import (
    DailySovereignEquitySnapshot,
    DailySovereignEquitySnapshotScheduler,
    DailySovereignEquitySnapshotStore,
    DailySovereignEquitySnapshotWriter,
    DuplicateDailySovereignEquitySnapshotError,
    next_nyse_session_close_ms,
)


def _ms_utc(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def _account_snapshot(*, account_id: str = "alpaca-account", equity: float = 100_125.75) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode="paper",
        account_status="ACTIVE",
        currency="USD",
        cash=50_000.0,
        equity=equity,
        buying_power=100_000.0,
        portfolio_value=equity,
        long_market_value=50_125.75,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=None,
        observed_at_ms=_ms_utc(2026, 11, 27, 18, 0),
    )


@pytest.mark.asyncio
async def test_writer_appends_account_scoped_daily_equity_row_to_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "sovereign-equity.sqlite3"
    session_close_ms = session_close_ms_utc(datetime(2026, 11, 27, tzinfo=UTC).date())

    async def load_account() -> BrokerAccountSnapshot:
        return _account_snapshot()

    writer = DailySovereignEquitySnapshotWriter(
        store=DailySovereignEquitySnapshotStore(database_path),
        account_snapshot_provider=load_account,
    )

    appended = await writer.capture(session_close_ms=session_close_ms)

    assert appended == DailySovereignEquitySnapshot(
        account_id="alpaca-account",
        session_close_ms=session_close_ms,
        equity=100_125.75,
        observed_at_ms=_ms_utc(2026, 11, 27, 18, 0),
    )
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT account_id, session_close_ms, equity, observed_at_ms "
            "FROM daily_sovereign_equity_snapshots"
        ).fetchall()
    assert rows == [("alpaca-account", session_close_ms, 100_125.75, _ms_utc(2026, 11, 27, 18, 0))]


def test_store_refuses_duplicate_or_mutable_daily_equity_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "sovereign-equity.sqlite3"
    store = DailySovereignEquitySnapshotStore(database_path)
    snapshot = DailySovereignEquitySnapshot(
        account_id="alpaca-account",
        session_close_ms=_ms_utc(2026, 7, 8, 20, 0),
        equity=100_000.0,
        observed_at_ms=_ms_utc(2026, 7, 8, 20, 0),
    )
    store.append(snapshot)

    with pytest.raises(DuplicateDailySovereignEquitySnapshotError):
        store.append(snapshot)
    with sqlite3.connect(database_path) as connection, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM daily_sovereign_equity_snapshots")


@pytest.mark.parametrize("equity", [math.inf, math.nan])
def test_store_refuses_non_finite_broker_equity(tmp_path: Path, equity: float) -> None:
    store = DailySovereignEquitySnapshotStore(tmp_path / "sovereign-equity.sqlite3")

    with pytest.raises(ValueError, match="finite"):
        store.append(
            DailySovereignEquitySnapshot(
                account_id="alpaca-account",
                session_close_ms=_ms_utc(2026, 7, 8, 20, 0),
                equity=equity,
                observed_at_ms=_ms_utc(2026, 7, 8, 20, 0),
            )
        )


@pytest.mark.asyncio
async def test_scheduler_uses_calendar_derived_early_close_boundary() -> None:
    now_ms = _ms_utc(2026, 11, 27, 17, 59)
    expected_close_ms = session_close_ms_utc(datetime(2026, 11, 27, tzinfo=UTC).date())
    captured_closes: list[int] = []
    slept_for: list[float] = []

    class Writer:
        async def capture(self, *, session_close_ms: int) -> DailySovereignEquitySnapshot:
            captured_closes.append(session_close_ms)
            return DailySovereignEquitySnapshot(
                account_id="alpaca-account",
                session_close_ms=session_close_ms,
                equity=100_000.0,
                observed_at_ms=session_close_ms,
            )

    async def record_sleep(seconds: float) -> None:
        slept_for.append(seconds)

    scheduler = DailySovereignEquitySnapshotScheduler(
        writer=Writer(),
        clock=lambda: now_ms,
        sleep=record_sleep,
    )

    await scheduler.capture_next_session_close()

    assert next_nyse_session_close_ms(now_ms) == expected_close_ms
    assert slept_for == [60.0]
    assert captured_closes == [expected_close_ms]


@pytest.mark.asyncio
async def test_scheduler_retries_the_same_session_close_after_capture_failure() -> None:
    session_close_ms = _ms_utc(2026, 7, 8, 20, 0)
    captured_closes: list[int] = []
    slept_for: list[float] = []
    captured = asyncio.Event()

    class Writer:
        async def capture(self, *, session_close_ms: int) -> DailySovereignEquitySnapshot:
            captured_closes.append(session_close_ms)
            if len(captured_closes) == 1:
                raise RuntimeError("Alpaca account read failed")
            captured.set()
            return DailySovereignEquitySnapshot(
                account_id="alpaca-account",
                session_close_ms=session_close_ms,
                equity=100_000.0,
                observed_at_ms=session_close_ms,
            )

    async def record_sleep(seconds: float) -> None:
        slept_for.append(seconds)
        await asyncio.sleep(0)

    scheduler = DailySovereignEquitySnapshotScheduler(
        writer=Writer(),
        clock=lambda: session_close_ms - 1_000,
        sleep=record_sleep,
        session_close_resolver=lambda _now_ms: session_close_ms,
        latest_completed_session_close_resolver=lambda _now_ms: None,
    )
    scheduler.start()
    await asyncio.wait_for(captured.wait(), timeout=1)
    await scheduler.stop()

    assert captured_closes[:2] == [session_close_ms, session_close_ms]
    assert slept_for[:2] == [1.0, 60.0]


@pytest.mark.asyncio
async def test_scheduler_captures_latest_missed_close_before_waiting_for_next() -> None:
    missed_close_ms = _ms_utc(2026, 7, 8, 20, 0)
    next_close_ms = _ms_utc(2026, 7, 9, 20, 0)
    captured_closes: list[int] = []
    captured = asyncio.Event()

    class Writer:
        async def capture(self, *, session_close_ms: int) -> DailySovereignEquitySnapshot:
            captured_closes.append(session_close_ms)
            captured.set()
            return DailySovereignEquitySnapshot(
                account_id="alpaca-account",
                session_close_ms=session_close_ms,
                equity=100_000.0,
                observed_at_ms=session_close_ms,
            )

    async def wait_forever(_seconds: float) -> None:
        await asyncio.Event().wait()

    scheduler = DailySovereignEquitySnapshotScheduler(
        writer=Writer(),
        clock=lambda: missed_close_ms + 60_000,
        sleep=wait_forever,
        session_close_resolver=lambda _now_ms: next_close_ms,
        latest_completed_session_close_resolver=lambda _now_ms: missed_close_ms,
    )
    scheduler.start()
    await asyncio.wait_for(captured.wait(), timeout=1)
    await scheduler.stop()

    assert captured_closes == [missed_close_ms]
