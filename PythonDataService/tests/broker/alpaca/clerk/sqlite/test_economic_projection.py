"""SQLite-native economic projection contract coverage for S2 (#1441-#1447)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionUnavailable,
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from app.lean_sidecar.trading_calendar import SessionWindow, session_window_for_date
from conftest import _clock_at

_ACCOUNT_ID = "PA-S2-ECONOMICS"
_SID = "s2-economic"
_RUN_ID = "run-s2-economic"
_ATOL = 1e-6
_RTOL = 0.0


def _repository(tmp_path: Path) -> tuple[ClerkSqliteRepository, EnterSubmission]:
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_786_368_000_000),
    )
    repo.register_strategy_instance(
        strategy_instance_id=_SID,
        symbol="GOOGL",
        config_hash="s2-economic-config",
    )
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id=_RUN_ID,
    )
    accepted = accept_enter(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        decision_id="s2-economic-enter",
        lifecycle_run_id=_RUN_ID,
        leg=BrokerOrderLeg(symbol="GOOGL", side="buy", quantity=10),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    return repo, accepted


def _slice_transition(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    facts: ExecutionSliceFilledFacts,
) -> TransitionInput:
    return TransitionInput(
        strategy_instance_id=_SID,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=facts.source_event_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_SLICE_FILLED",
        facts_json=facts.to_facts_json(),
    )


def _append_slice(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    side: str,
    quantity: float,
    price: float,
    occurred_at_ms: int,
    fee: float | None = None,
    fee_fidelity: str = "not_reported",
) -> None:
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol="GOOGL",
        side=side,
        slice_qty=quantity,
        slice_price=price,
        fee=fee,
        fee_fidelity=fee_fidelity,
        evidence_source="websocket",
        source_event_at_ms=occurred_at_ms,
    )
    result = repo.append_execution_slice_if_absent(
        execution_id=execution_id,
        order_ref=accepted.order_ref or "",
        build_transition=lambda: _slice_transition(repo, accepted, facts),
        build_coverage_conflict=lambda: (_ for _ in ()).throw(
            AssertionError("exact test fixture cannot have cumulative recovery coverage")
        ),
    )
    assert result == "appended"


def _append_correction(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    superseded_execution_ref: str,
    quantity: float,
    price: float,
    occurred_at_ms: int,
) -> None:
    facts = ExecutionCorrectedFacts(
        execution_id=execution_id,
        superseded_execution_ref=superseded_execution_ref,
        symbol="GOOGL",
        side="BUY",
        corrected_qty=quantity,
        corrected_price=price,
        why="Broker correction fixture",
    )
    correction = TransitionInput(
        strategy_instance_id=_SID,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_CORRECTED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=occurred_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_CORRECTED",
        facts_json=facts.to_facts_json(),
    )
    result = repo.append_execution_correction_or_raise(
        correction=correction,
        build_uncertainty=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
    )
    assert result == "appended"


def _snapshot(
    repo: ClerkSqliteRepository,
    *,
    session: SessionWindow,
    marks: dict[str, MarketMark] | None = None,
):
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        snapshot = reader.bot_economic_snapshot(_SID, session_window=session, marks=marks)
        assert snapshot is not None
        return snapshot
    finally:
        reader.close()


def test_bot_economic_snapshot_uses_effective_execution_slices_and_canonical_fifo(
    tmp_path: Path,
) -> None:
    """GOOGL's two entry slices plus one exit close FIFO lots at $19.00."""
    repo, accepted = _repository(tmp_path)
    session = session_window_for_date(date(2026, 8, 10))
    try:
        _append_slice(
            repo,
            accepted,
            execution_id="exec-buy-1",
            side="BUY",
            quantity=2.0,
            price=100.0,
            occurred_at_ms=session.open_ms_utc + 1_000,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-buy-2",
            side="BUY",
            quantity=3.0,
            price=102.0,
            occurred_at_ms=session.open_ms_utc + 2_000,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-sell-1",
            side="SELL",
            quantity=5.0,
            price=105.0,
            occurred_at_ms=session.open_ms_utc + 3_000,
        )

        snapshot = _snapshot(repo, session=session)

        assert [fill.event_key for fill in snapshot.recent_fills] == [
            "exec-sell-1",
            "exec-buy-2",
            "exec-buy-1",
        ]
        assert snapshot.fills_today == 3
        assert snapshot.exposure == {}
        assert snapshot.realized_pnl_today == pytest.approx(19.0, abs=_ATOL, rel=_RTOL)
        assert snapshot.open_pnl == pytest.approx(0.0, abs=_ATOL, rel=_RTOL)
        assert snapshot.marks_complete is True
        assert snapshot.fee_fidelity == "not_reported"
        assert snapshot.execution_coverage == "complete"
    finally:
        repo.close()


def test_snapshot_runs_fifo_over_full_history_not_pre_filtered_session_fills(tmp_path: Path) -> None:
    """A Friday BUY must remain available to price Monday's realized SELL."""
    repo, accepted = _repository(tmp_path)
    prior = session_window_for_date(date(2026, 8, 7))
    today = session_window_for_date(date(2026, 8, 10))
    try:
        _append_slice(
            repo,
            accepted,
            execution_id="exec-prior-buy",
            side="BUY",
            quantity=2.0,
            price=100.0,
            occurred_at_ms=prior.open_ms_utc + 1_000,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-today-sell",
            side="SELL",
            quantity=2.0,
            price=110.0,
            occurred_at_ms=today.open_ms_utc + 1_000,
        )

        snapshot = _snapshot(repo, session=today)

        assert snapshot.fills_today == 1
        assert snapshot.realized_pnl_today == pytest.approx(20.0, abs=_ATOL, rel=_RTOL)
        assert snapshot.open_pnl == pytest.approx(0.0, abs=_ATOL, rel=_RTOL)
    finally:
        repo.close()


def test_late_correction_keeps_root_execution_time_for_fifo_and_session_window(tmp_path: Path) -> None:
    """A post-close correction must not move the earlier sell's closed-lot date."""
    repo, accepted = _repository(tmp_path)
    session = session_window_for_date(date(2026, 8, 10))
    try:
        buy_at = session.open_ms_utc + 1_000
        sell_at = session.open_ms_utc + 2_000
        correction_at = session.close_ms_utc + 1_000
        _append_slice(
            repo,
            accepted,
            execution_id="exec-root-buy",
            side="BUY",
            quantity=2.0,
            price=100.0,
            occurred_at_ms=buy_at,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-sell",
            side="SELL",
            quantity=2.0,
            price=110.0,
            occurred_at_ms=sell_at,
        )
        _append_correction(
            repo,
            accepted,
            execution_id="exec-root-buy-corrected",
            superseded_execution_ref="exec-root-buy",
            quantity=2.0,
            price=101.0,
            occurred_at_ms=correction_at,
        )

        snapshot = _snapshot(repo, session=session)
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            execution_page = reader.account_executions(state="effective")
        finally:
            reader.close()

        assert snapshot.fills_today == 2
        assert snapshot.realized_pnl_today == pytest.approx(18.0, abs=_ATOL, rel=_RTOL)
        corrected = next(
            row for row in execution_page.executions if row.execution_id == "exec-root-buy-corrected"
        )
        assert corrected.filled_at_ms == buy_at
        assert corrected.recorded_at_ms != corrected.filled_at_ms
    finally:
        repo.close()


def test_open_pnl_requires_every_mark_and_carries_mark_observation_time(tmp_path: Path) -> None:
    repo, accepted = _repository(tmp_path)
    session = session_window_for_date(date(2026, 8, 10))
    try:
        _append_slice(
            repo,
            accepted,
            execution_id="exec-open-buy",
            side="BUY",
            quantity=2.0,
            price=100.0,
            occurred_at_ms=session.open_ms_utc + 1_000,
            fee=0.0,
            fee_fidelity="reported",
        )

        missing = _snapshot(repo, session=session)
        marked = _snapshot(
            repo,
            session=session,
            marks={"GOOGL": MarketMark(price=104.0, observed_at_ms=session.open_ms_utc + 4_000)},
        )

        assert missing.open_pnl is None
        assert missing.marks_complete is False
        assert missing.fee_fidelity == "reported"
        assert marked.open_pnl == pytest.approx(8.0, abs=_ATOL, rel=_RTOL)
        assert marked.marks_complete is True
        assert marked.mark_observed_at_ms == {"GOOGL": session.open_ms_utc + 4_000}
    finally:
        repo.close()


def test_market_mark_rejects_non_numeric_or_ambiguous_boundary_values() -> None:
    with pytest.raises(ValueError, match="price"):
        MarketMark(price=True, observed_at_ms=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="observed_at_ms"):
        MarketMark(price=100.0, observed_at_ms=True)  # type: ignore[arg-type]


def test_dst_session_boundary_counts_only_fills_inside_canonical_nyse_window(tmp_path: Path) -> None:
    repo, accepted = _repository(tmp_path)
    # The March 2026 Monday follows the US DST transition.  Use only the
    # calendar's ms boundaries; a fixed-offset implementation would drift.
    session = session_window_for_date(date(2026, 3, 9))
    try:
        _append_slice(
            repo,
            accepted,
            execution_id="exec-before-dst-session",
            side="BUY",
            quantity=1.0,
            price=100.0,
            occurred_at_ms=session.open_ms_utc - 1,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-at-dst-open",
            side="BUY",
            quantity=1.0,
            price=101.0,
            occurred_at_ms=session.open_ms_utc,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-before-dst-close",
            side="BUY",
            quantity=1.0,
            price=102.0,
            occurred_at_ms=session.close_ms_utc - 1,
        )

        snapshot = _snapshot(repo, session=session)

        assert snapshot.fills_today == 2
    finally:
        repo.close()


def test_half_day_session_excludes_execution_at_canonical_early_close(tmp_path: Path) -> None:
    repo, accepted = _repository(tmp_path)
    # Thanksgiving Friday is an NYSE early-close session. The calendar, not a
    # literal RTH close, supplies the half-open window used by the projection.
    session = session_window_for_date(date(2026, 11, 27))
    try:
        _append_slice(
            repo,
            accepted,
            execution_id="exec-before-early-close",
            side="BUY",
            quantity=1.0,
            price=100.0,
            occurred_at_ms=session.close_ms_utc - 1,
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-at-early-close",
            side="BUY",
            quantity=1.0,
            price=101.0,
            occurred_at_ms=session.close_ms_utc,
        )

        snapshot = _snapshot(repo, session=session)

        assert snapshot.fills_today == 1
    finally:
        repo.close()


def test_pages_are_keyset_bounded_and_account_rows_preserve_effective_state(tmp_path: Path) -> None:
    repo, accepted = _repository(tmp_path)
    session = session_window_for_date(date(2026, 8, 10))
    try:
        for sequence in range(3):
            _append_slice(
                repo,
                accepted,
                execution_id=f"exec-page-{sequence}",
                side="BUY",
                quantity=1.0,
                price=100.0 + sequence,
                occurred_at_ms=session.open_ms_utc + sequence * 1_000,
            )
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            first = reader.bot_fills(_SID, limit=1)
            assert first.next_cursor is not None
            second = reader.bot_fills(_SID, limit=1, cursor=first.next_cursor)
            execution_page = reader.account_executions(limit=10, origin="strategy", state="effective")
        finally:
            reader.close()

        assert len(first.fills) == 1
        assert len(second.fills) == 1
        assert first.fills[0].event_key != second.fills[0].event_key
        assert {row.state for row in execution_page.executions} == {"effective"}
        assert {row.origin for row in execution_page.executions} == {"strategy"}
    finally:
        repo.close()


def test_mismatched_effective_fills_and_positions_is_explicitly_unavailable(tmp_path: Path) -> None:
    repo, accepted = _repository(tmp_path)
    session = session_window_for_date(date(2026, 8, 10))
    try:
        _append_slice(
            repo,
            accepted,
            execution_id="exec-integrity",
            side="BUY",
            quantity=1.0,
            price=100.0,
            occurred_at_ms=session.open_ms_utc + 1_000,
        )
        # Simulate a corrupted fold without using any legacy data source. The
        # projection must refuse an internally inconsistent SQLite answer.
        repo._conn.execute(
            "UPDATE positions SET attributed_qty = 9 WHERE strategy_instance_id = ? AND symbol = 'GOOGL'",
            (_SID,),
        )
        repo._conn.commit()

        with pytest.raises(EconomicProjectionUnavailable, match="disagree"):
            _snapshot(repo, session=session)
    finally:
        repo.close()


def test_catalog_rollup_returns_one_revision_for_all_requested_bots(tmp_path: Path) -> None:
    repo, accepted = _repository(tmp_path)
    session = session_window_for_date(date(2026, 8, 10))
    second_sid = "s2-economic-two"
    try:
        repo.register_strategy_instance(
            strategy_instance_id=second_sid,
            symbol="AAPL",
            config_hash="s2-economic-config-two",
        )
        _append_slice(
            repo,
            accepted,
            execution_id="exec-catalog",
            side="BUY",
            quantity=1.0,
            price=100.0,
            occurred_at_ms=session.open_ms_utc + 1_000,
        )
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            rollup = reader.catalog_economic_rollup([_SID, second_sid], session_window=session)
        finally:
            reader.close()

        assert set(rollup) == {_SID, second_sid}
        assert {snapshot.control_revision for snapshot in rollup.values()} == {
            repo.control_meta_snapshot().control_revision
        }
    finally:
        repo.close()
