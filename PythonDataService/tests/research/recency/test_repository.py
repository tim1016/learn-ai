"""Recency Chart persistence and reads on the Python-owned tables (PRD #1927).

Runs against the ephemeral database only (same attestation as the Grid
Search suites). The semantics under test are the ones the .NET service and
GraphQL query established — tombstone no-op, fingerprint sharing with
memberships, deleted-owner visibility, overlap-vs-entry windows,
representative selection — plus the one new rule: a redelivered cell
returns the existing run instead of creating a second one.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from app.research.recency import repository as repo
from app.research.recency.runner import RecencyRunSnapshot, RecencyTradeSnapshot
from app.research.recency.stats import select_window_heroes

pytestmark = pytest.mark.asyncio


def _trade(fingerprint: str, *, entry_ms: int, exit_ms: int, pnl: float = 10.0) -> RecencyTradeSnapshot:
    return RecencyTradeSnapshot(
        fingerprint=fingerprint, entry_ms=entry_ms, exit_ms=exit_ms, pnl_pts=1.5, pnl_pct=0.01, quantity=10, pnl=pnl, holding_sessions=1, is_synthetic_exit=False, signal_reason=""
    )


def _snapshot(launch_id: str, *, symbol: str, params_hash: str = "h1", trades: list[RecencyTradeSnapshot], sharpe: float | None = 1.25, strategy: str = "sma_crossover") -> RecencyRunSnapshot:
    return RecencyRunSnapshot(
        launch_id=launch_id, symbol=symbol, strategy_key=strategy, params={"short_window": 2.0}, params_hash=params_hash, total_pnl=sum(t.pnl for t in trades), sharpe=sharpe, trades=trades, study_id=None
    )


async def _launch(conn, unique: str, expected: int = 4) -> str:
    launch_id = f"launch-{unique}-{uuid.uuid4().hex[:6]}"
    assert await repo.create_launch(conn, launch_id=launch_id, config_json="{}", expected_runs=expected) is True
    return launch_id


async def test_a_launch_is_created_once_and_a_retried_dispatch_does_not_reset_it(conn, unique: str) -> None:
    launch_id = await _launch(conn, unique)
    await repo.persist_snapshot(conn, _snapshot(launch_id, symbol=unique, trades=[_trade(f"{unique}-a", entry_ms=100, exit_ms=200)]))

    assert await repo.create_launch(conn, launch_id=launch_id, config_json="{}", expected_runs=4) is False  # the retry

    row = await conn.fetchrow('SELECT "SucceededRuns", "Status" FROM "RecencyLaunches" WHERE "Id" = $1', launch_id)
    assert row["SucceededRuns"] == 1 and row["Status"] == "RUNNING"
    with pytest.raises(ValueError):
        await repo.create_launch(conn, launch_id=f"{launch_id}-zero", config_json="{}", expected_runs=0)


async def test_the_same_launch_id_with_a_different_configuration_is_refused(conn, unique: str) -> None:
    """A conflict answered by DO NOTHING alone would run the new grid under the first record's ConfigJson."""
    launch_id = await _launch(conn, unique)

    with pytest.raises(repo.LaunchConflictError):
        await repo.create_launch(conn, launch_id=launch_id, config_json='{"symbols": ["QQQ"]}', expected_runs=4)
    with pytest.raises(repo.LaunchConflictError):  # same grid text, a different cell count is not the same launch either
        await repo.create_launch(conn, launch_id=launch_id, config_json="{}", expected_runs=5)

    stored = await conn.fetchval('SELECT "ConfigJson" FROM "RecencyLaunches" WHERE "Id" = $1', launch_id)
    assert stored == "{}"


async def test_a_snapshot_for_a_tombstoned_launch_is_a_no_op(conn, unique: str) -> None:
    launch_id = await _launch(conn, unique)
    await repo.set_launch_deleted(conn, launch_id, deleted=True)

    outcome = await repo.persist_snapshot(conn, _snapshot(launch_id, symbol=unique, trades=[_trade(f"{unique}-a", entry_ms=100, exit_ms=200)]))

    assert outcome == repo.PersistOutcome(recency_run_id=None, skipped=True)
    assert await conn.fetchval('SELECT COUNT(*) FROM "RecencyRuns" WHERE "RecencyLaunchId" = $1', launch_id) == 0


async def test_a_snapshot_for_an_unknown_launch_is_refused(conn, unique: str) -> None:
    with pytest.raises(repo.LaunchNotFoundError):
        await repo.persist_snapshot(conn, _snapshot(f"never-{unique}", symbol=unique, trades=[]))


async def test_a_redelivered_cell_returns_the_existing_run_and_counts_once(conn, unique: str) -> None:
    launch_id = await _launch(conn, unique)
    snapshot = _snapshot(launch_id, symbol=unique, trades=[_trade(f"{unique}-a", entry_ms=100, exit_ms=200)])

    first = await repo.persist_snapshot(conn, snapshot)
    second = await repo.persist_snapshot(conn, snapshot)

    assert first.redelivered is False and second == repo.PersistOutcome(recency_run_id=first.recency_run_id, redelivered=True)
    assert await conn.fetchval('SELECT "SucceededRuns" FROM "RecencyLaunches" WHERE "Id" = $1', launch_id) == 1
    # A different cell in the same launch is a new execution.
    third = await repo.persist_snapshot(conn, replace(snapshot, params_hash="h2", trades=[_trade(f"{unique}-b", entry_ms=100, exit_ms=200)]))
    assert third.redelivered is False and third.recency_run_id != first.recency_run_id


async def test_a_shared_fingerprint_is_stored_once_with_a_membership_per_run(conn, unique: str) -> None:
    launch_a, launch_b = await _launch(conn, unique), await _launch(conn, unique)
    shared = _trade(f"{unique}-shared", entry_ms=100, exit_ms=200)

    a = await repo.persist_snapshot(conn, _snapshot(launch_a, symbol=unique, trades=[shared]))
    b = await repo.persist_snapshot(conn, _snapshot(launch_b, symbol=unique, trades=[shared, _trade(f"{unique}-only-b", entry_ms=300, exit_ms=400)]))

    assert await conn.fetchval('SELECT COUNT(*) FROM "RecencyTrades" WHERE "Fingerprint" = $1', shared.fingerprint) == 1
    memberships = await conn.fetch(
        'SELECT m."RecencyRunId" FROM "RecencyTradeMemberships" m JOIN "RecencyTrades" t ON t."Id" = m."RecencyTradeId" WHERE t."Fingerprint" = $1 ORDER BY 1',
        shared.fingerprint,
    )
    assert [row["RecencyRunId"] for row in memberships] == sorted([a.recency_run_id, b.recency_run_id])


async def test_deleting_the_owning_run_keeps_a_trade_another_live_run_vouches_for(conn, unique: str) -> None:
    launch_a, launch_b = await _launch(conn, unique), await _launch(conn, unique)
    shared = _trade(f"{unique}-shared", entry_ms=100, exit_ms=200)
    a = await repo.persist_snapshot(conn, _snapshot(launch_a, symbol=unique, trades=[shared]))
    b = await repo.persist_snapshot(conn, _snapshot(launch_b, symbol=unique, trades=[shared]))
    assert a.recency_run_id is not None and b.recency_run_id is not None

    await repo.set_run_deleted(conn, a.recency_run_id, deleted=True)  # the original owner
    views = await repo.list_trades(conn, from_ms=0, to_ms=1_000, symbols=[unique])

    assert [view.fingerprint for view in views] == [shared.fingerprint]
    assert views[0].recency_run_id == b.recency_run_id  # the representative is the surviving live run
    assert [m.recency_run_id for m in views[0].memberships] == [b.recency_run_id]
    assert views[0].pnl == 10.0 and isinstance(views[0].pnl, float)  # numbers, not Decimal strings

    await repo.set_run_deleted(conn, b.recency_run_id, deleted=True)
    assert await repo.list_trades(conn, from_ms=0, to_ms=1_000, symbols=[unique]) == []
    await repo.set_run_deleted(conn, a.recency_run_id, deleted=False)
    assert [v.recency_run_id for v in await repo.list_trades(conn, from_ms=0, to_ms=1_000, symbols=[unique])] == [a.recency_run_id]


async def test_the_representative_is_the_newest_live_run_matching_the_filters(conn, unique: str) -> None:
    launch = await _launch(conn, unique)
    shared = _trade(f"{unique}-shared", entry_ms=100, exit_ms=200)
    older = await repo.persist_snapshot(conn, _snapshot(launch, symbol=unique, params_hash="old", trades=[shared], strategy="sma_crossover"))
    newer = await repo.persist_snapshot(conn, _snapshot(launch, symbol=unique, params_hash="new", trades=[shared], strategy="rsi_mean_reversion"))
    await conn.execute('UPDATE "RecencyRuns" SET "CreatedAtMs" = "CreatedAtMs" + 1000 WHERE "Id" = $1', newer.recency_run_id)

    unfiltered = await repo.list_trades(conn, from_ms=0, to_ms=1_000, symbols=[unique])
    assert unfiltered[0].recency_run_id == newer.recency_run_id and unfiltered[0].params_hash == "new"
    assert [m.recency_run_id for m in unfiltered[0].memberships] == [newer.recency_run_id, older.recency_run_id]

    by_strategy = await repo.list_trades(conn, from_ms=0, to_ms=1_000, symbols=[unique], strategies=["sma_crossover"])
    assert by_strategy[0].recency_run_id == older.recency_run_id


async def test_the_chart_reads_overlap_but_the_hero_reads_entry_inside_the_window(conn, unique: str) -> None:
    launch = await _launch(conn, unique)
    # Entered before the window, exited inside it: drawn by the chart, ignored by the hero.
    straddling = _trade(f"{unique}-straddle", entry_ms=50, exit_ms=150, pnl=100.0)
    inside = _trade(f"{unique}-inside", entry_ms=120, exit_ms=130, pnl=5.0)
    loud = await repo.persist_snapshot(conn, _snapshot(launch, symbol=unique, params_hash="loud", trades=[straddling]))
    quiet = await repo.persist_snapshot(conn, _snapshot(launch, symbol=unique, params_hash="quiet", trades=[inside]))

    drawn = await repo.list_trades(conn, from_ms=100, to_ms=200, symbols=[unique])
    assert {view.fingerprint for view in drawn} == {straddling.fingerprint, inside.fingerprint}

    candidates = await repo.hero_candidates(conn, from_ms=100, to_ms=200, symbols=[unique])
    assert {c.recency_run_id for c in candidates} == {quiet.recency_run_id}
    heroes = select_window_heroes(candidates, 100, 200)
    assert [(h.recency_run_id, h.total_pnl) for h in heroes] == [(quiet.recency_run_id, 5.0)]
    assert loud.recency_run_id not in {c.recency_run_id for c in candidates}


async def test_terminal_status_rules_match_the_launch_service(conn, unique: str) -> None:
    launch = await _launch(conn, unique, expected=2)
    await repo.persist_snapshot(conn, _snapshot(launch, symbol=unique, trades=[_trade(f"{unique}-a", entry_ms=1, exit_ms=2)]))

    with pytest.raises(repo.LaunchAccountingError):
        await repo.set_terminal_status(conn, launch, status="COMPLETED", succeeded_runs=1, failed_runs=0)  # 1 of 2 accounted for
    with pytest.raises(repo.LaunchAccountingError):
        await repo.set_terminal_status(conn, launch, status="FAILED", succeeded_runs=2, failed_runs=1)  # more than expected
    assert await repo.set_terminal_status(conn, launch, status="CANCELLED") is True  # abort: no counts needed
    row = await conn.fetchrow('SELECT "Status", "SucceededRuns", "CompletedAtMs" FROM "RecencyLaunches" WHERE "Id" = $1', launch)
    assert row["Status"] == "CANCELLED" and row["SucceededRuns"] == 1 and row["CompletedAtMs"] is not None
    assert await repo.set_terminal_status(conn, f"missing-{unique}", status="FAILED") is False


async def test_soft_delete_and_restore_report_whether_the_row_existed(conn, unique: str) -> None:
    launch = await _launch(conn, unique)
    assert await repo.set_launch_deleted(conn, launch, deleted=True) is True
    assert await repo.set_launch_deleted(conn, launch, deleted=False) is True
    assert await repo.set_launch_deleted(conn, f"missing-{unique}", deleted=True) is False
    assert await repo.set_run_deleted(conn, 2_147_000_000, deleted=True) is False
