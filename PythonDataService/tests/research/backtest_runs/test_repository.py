"""Backtest runs, trades and parity verdicts on the Python-owned tables (PRD #1929).

Runs against the ephemeral database only (same attestation as the Grid
Search and Recency suites). The semantics under test are the ones the .NET
persistence service, GraphQL queries and parity API established: a
write-then-read round trip with every field intact, LEAN idempotency on the
run id, newest-first history with an engine filter, the five-hundred-trade
report bound, the Recency hard-delete guard, and first-terminal-state-wins
parity verdicts.
"""

from __future__ import annotations

import json

import pytest

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.records import record_from_payload
from tests.research.backtest_runs.payloads import ENTRY_MS, EXIT_MS, engine_payload, lean_payload, trade

pytestmark = pytest.mark.asyncio


async def _insert(conn, payload: dict) -> int:
    outcome = await repo.insert_run(conn, record_from_payload(payload))
    assert outcome.created is True
    return outcome.run_id


async def _recency_run_for(conn, run_id: int, unique: str, *, deleted: bool = False) -> int:
    launch_id = f"launch-{unique}"
    await conn.execute(
        """
        INSERT INTO "RecencyLaunches" ("Id", "ConfigJson", "ExpectedRuns", "SucceededRuns", "FailedRuns", "Status", "CreatedAtMs")
        VALUES ($1, '{}'::jsonb, 1, 1, 0, 'COMPLETED', 1)
        """,
        launch_id,
    )
    return await conn.fetchval(
        """
        INSERT INTO "RecencyRuns" ("RecencyLaunchId", "StrategyKey", "Symbol", "ParamsJson", "ParamsHash", "StudyId", "TotalPnl", "CreatedAtMs", "DeletedAtMs")
        VALUES ($1, 'sma_crossover', $2, '{}'::jsonb, 'h', $3, 0, 1, $4)
        RETURNING "Id"
        """,
        launch_id,
        unique,
        run_id,
        1 if deleted else None,
    )


async def test_an_engine_run_survives_a_write_then_read_round_trip_with_every_field(conn, unique: str) -> None:
    payload = engine_payload(symbol=unique)

    run_id = await _insert(conn, payload)
    run = await repo.get_run(conn, run_id)

    assert run is not None and run.id == run_id
    assert run.source == "engine" and run.engine == "PYTHON" and run.requested_engine == "python"
    assert run.strategy_name == "ema_crossover_signal" and run.symbol == unique.upper()
    assert json.loads(run.parameters_json) == {"symbol": unique.upper(), "gap_bps": 0.0}
    assert run.start_ms == 1736139600000 and run.end_ms == 1736485200000  # ET midnight of the trading dates
    assert run.fill_mode == "signal_bar_close" and run.timespan == "minute" and run.duration_ms == 1234
    assert run.executed_at_ms > 0
    assert (run.total_trades, run.winning_trades, run.losing_trades, run.win_rate) == (1, 1, 0, 1.0)
    assert (run.total_pnl, run.initial_cash, run.final_equity, run.total_fees) == (20.0, 100_000.0, 100_020.0, 0.0)
    assert (run.max_drawdown, run.sharpe_ratio, run.sortino_ratio, run.profit_factor) == (0.0257, 1.43, 2.59, 2.0)
    assert run.commission_per_order == 0.0 and run.brokerage_policy == "algorithm_default"
    assert json.loads(run.data_policy_json) == json.loads(payload["data_policy_json"])
    assert json.loads(run.lean_statistics_json) == payload["lean_statistics"]
    assert run.lean_analysis_json is None
    assert json.loads(run.run_verdict_json) == json.loads(payload["run_verdict_json"])
    assert (run.verdict_version, run.verdict_grade, run.verdict_signal) == (2, "A", "Paper-trade")
    assert json.loads(run.equity_curve_json) == json.loads(payload["equity_curve_json"])
    assert json.loads(run.validation_analytics_json) == json.loads(payload["validation_analytics_json"])
    assert json.loads(run.insight_summary_json) == {"total": 1}
    assert json.loads(run.metric_documentation_json) == json.loads(payload["metric_documentation_json"])
    assert run.parity_group_id is None and run.notes is None
    assert run.trades_truncated is False and run.parity_verdicts == ()
    [persisted] = run.trades
    assert (persisted.trade_number, persisted.entry_ms, persisted.exit_ms) == (1, ENTRY_MS, EXIT_MS)
    assert (persisted.entry_price, persisted.exit_price, persisted.quantity, persisted.pnl) == (
        710.0,
        712.0,
        10.0,
        20.0,
    )
    assert persisted.signal_reason == "ema cross" and persisted.is_synthetic_exit is False


async def test_a_lean_run_is_idempotent_on_its_run_id_and_refuses_a_different_engine(conn, unique: str) -> None:
    first = await repo.insert_run(conn, record_from_payload(lean_payload(f"lean-{unique}", symbol=unique)))
    again = await repo.insert_run(conn, record_from_payload(lean_payload(f"lean-{unique}", symbol=unique)))

    assert first.created is True and again.created is False and again.run_id == first.run_id
    assert (
        await conn.fetchval("SELECT count(*) FROM research_backtest_runs WHERE lean_run_id = $1", f"lean-{unique}") == 1
    )
    with pytest.raises(repo.RunConflictError):
        await repo.insert_run(
            conn, record_from_payload(lean_payload(f"lean-{unique}", symbol=unique, requested_engine="both"))
        )


async def test_every_engine_persist_is_a_new_row(conn, unique: str) -> None:
    first = await _insert(conn, engine_payload(symbol=unique))
    second = await _insert(conn, engine_payload(symbol=unique))

    assert second != first


async def test_history_reads_newest_first_and_filters_by_engine(conn, unique: str) -> None:
    engine_id = await _insert(conn, engine_payload(symbol=unique))
    lean_id = await _insert(conn, lean_payload(f"lean-{unique}", symbol=unique, trades=[trade(synthetic=True)]))

    rows = [row for row in await repo.list_runs(conn, engine=None, limit=500) if row.symbol == unique.upper()]
    assert [row.id for row in rows] == [lean_id, engine_id]
    lean_row, engine_row = rows
    assert (
        lean_row.engine == "LEAN" and lean_row.has_synthetic_exit is True and lean_row.lean_run_id == f"lean-{unique}"
    )
    assert engine_row.engine == "PYTHON" and engine_row.has_synthetic_exit is False
    assert engine_row.start_ms == 1736139600000 and json.loads(engine_row.data_policy_json)["symbol"] == unique

    only_python = [
        row.id for row in await repo.list_runs(conn, engine="PYTHON", limit=500) if row.symbol == unique.upper()
    ]
    only_lean = [row.id for row in await repo.list_runs(conn, engine="LEAN", limit=500) if row.symbol == unique.upper()]
    assert only_python == [engine_id] and only_lean == [lean_id]
    assert len(await repo.list_runs(conn, engine=None, limit=1)) == 1


async def test_the_report_carries_the_newest_five_hundred_trades_in_entry_order_and_says_so(conn, unique: str) -> None:
    trades = [
        trade(number, entry_ms=ENTRY_MS + number * 60_000, exit_ms=ENTRY_MS + number * 60_000 + 30_000)
        for number in range(1, 502)
    ]
    run_id = await _insert(conn, engine_payload(symbol=unique, trades=trades, total_trades=501))

    run = await repo.get_run(conn, run_id)
    everything = await repo.get_run(conn, run_id, trade_limit=None)

    assert run is not None and run.trades_truncated is True
    assert [t.trade_number for t in run.trades] == list(range(2, 502))
    assert everything is not None and everything.trades_truncated is False and len(everything.trades) == 501


async def test_notes_persist_and_read_back(conn, unique: str) -> None:
    run_id = await _insert(conn, engine_payload(symbol=unique))

    assert await repo.update_notes(conn, run_id, "worth a second look") is True
    assert (await repo.get_run(conn, run_id)).notes == "worth a second look"
    assert await repo.update_notes(conn, run_id, None) is True
    assert (await repo.get_run(conn, run_id)).notes is None
    assert await repo.update_notes(conn, -1, "x") is False


async def test_a_run_backing_a_live_recency_run_cannot_be_hard_deleted(conn, unique: str) -> None:
    run_id = await _insert(conn, engine_payload(symbol=unique))
    recency_run_id = await _recency_run_for(conn, run_id, unique)

    assert await repo.delete_run(conn, run_id) == "recency_member"
    assert await repo.get_run(conn, run_id) is not None

    await conn.execute('UPDATE "RecencyRuns" SET "DeletedAtMs" = 1 WHERE "Id" = $1', recency_run_id)
    assert await repo.delete_run(conn, run_id) == "deleted"
    assert await repo.get_run(conn, run_id) is None
    assert await conn.fetchval("SELECT count(*) FROM research_backtest_run_trades WHERE run_id = $1", run_id) == 0
    assert await repo.delete_run(conn, run_id) == "not_found"


async def test_deleting_the_lean_side_takes_its_parity_verdict_with_it(conn, unique: str) -> None:
    """A terminal verdict must not outlive the evidence it was judged against (Codex, PR #1969)."""
    group = f"pg-{unique}"
    left = await _insert(conn, engine_payload(symbol=unique, parity_group_id=group))
    right = await _insert(conn, lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group))
    await repo.freeze_parity_verdict(
        conn, parity_group_id=group, left_run_id=left, right_run_id=right, status="agree", verdict_json='{"status":"agree"}'
    )
    assert (await repo.get_parity_verdict(conn, group)).status == "agree"

    assert await repo.delete_run(conn, right) == "deleted"

    assert await repo.get_parity_verdict(conn, group) is None
    assert not (await repo.get_run(conn, left)).parity_verdicts


async def test_a_parity_disposition_is_recorded_once_per_group(conn, unique: str) -> None:
    left = await _insert(conn, engine_payload(symbol=unique, parity_group_id=f"pg-{unique}"))

    first = await repo.create_parity_verdict(
        conn, parity_group_id=f"pg-{unique}", left_run_id=left, status="pending", verdict_json='{"status":"pending"}'
    )
    again = await repo.create_parity_verdict(
        conn, parity_group_id=f"pg-{unique}", left_run_id=left, status="unavailable", verdict_json="{}"
    )

    assert first.status == "pending" and again.id == first.id and again.status == "pending"
    with pytest.raises(ValueError):
        await repo.create_parity_verdict(
            conn, parity_group_id=f"pg-x-{unique}", left_run_id=left, status="agree", verdict_json="{}"
        )
    [row] = await repo.list_parity_verdicts(conn, left)
    assert row.left_run_id == left and row.right_run_id is None and row.verdict_version == repo.PARITY_VERDICT_VERSION


async def test_mark_failed_transitions_only_a_pending_verdict(conn, unique: str) -> None:
    left = await _insert(conn, engine_payload(symbol=unique, parity_group_id=f"pg-{unique}"))
    await repo.create_parity_verdict(
        conn, parity_group_id=f"pg-{unique}", left_run_id=left, status="pending", verdict_json="{}"
    )

    row, transitioned = await repo.mark_parity_failed(
        conn, f"pg-{unique}", status="run_failed", detail="LEAN exited with code 1"
    )
    assert transitioned is True and row is not None and row.status == "run_failed"
    assert json.loads(row.verdict_json)["reason"] == "LEAN exited with code 1"

    row, transitioned = await repo.mark_parity_failed(conn, f"pg-{unique}", status="persist_failed", detail="later")
    assert transitioned is False and row is not None and row.status == "run_failed"  # first terminal state wins
    assert await repo.mark_parity_failed(conn, f"pg-missing-{unique}", status="run_failed", detail="x") == (None, False)
    with pytest.raises(ValueError):
        await repo.mark_parity_failed(conn, f"pg-{unique}", status="agree", detail="x")


async def test_freezing_a_verdict_wins_only_while_pending_and_repairs_a_lost_row(conn, unique: str) -> None:
    group = f"pg-{unique}"
    left = await _insert(conn, engine_payload(symbol=unique, parity_group_id=group))
    right = await _insert(conn, lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group))

    # The pending row was lost: freezing inserts the terminal verdict directly.
    assert (
        await repo.freeze_parity_verdict(
            conn,
            parity_group_id=group,
            left_run_id=left,
            right_run_id=right,
            status="agree",
            verdict_json='{"status":"agree"}',
        )
        is True
    )
    row = await repo.get_parity_verdict(conn, group)
    assert row is not None and row.status == "agree" and row.right_run_id == right
    # Terminal already: never overwritten.
    assert (
        await repo.freeze_parity_verdict(
            conn, parity_group_id=group, left_run_id=left, right_run_id=right, status="diverged", verdict_json="{}"
        )
        is False
    )
    assert (await repo.get_parity_verdict(conn, group)).status == "agree"
    # Both runs see the verdict on their report.
    assert [v.id for v in await repo.list_parity_verdicts(conn, right)] == [row.id]
    assert await repo.find_left_run_id(conn, group) == left
