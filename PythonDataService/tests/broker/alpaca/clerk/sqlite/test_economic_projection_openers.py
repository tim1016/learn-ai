"""Read-only openers the twin reconciliation reads a foreign authority with (ADR 0059 D2)."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionUnavailable,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from app.services.alpaca_shadow_reconciliation import EconomicFillSource, read_twin_fills
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_day_end_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

_ACCOUNT_ID = "PA-TWIN-OPENERS"
_SID = "twin-openers"
_OTHER_SID = "twin-openers-other"
_DAY = date(2026, 8, 10)


def _repository(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_786_368_000_000),
    )
    repo.register_strategy_instance(
        strategy_instance_id=_SID,
        symbol="SPY",
        config_hash="twin-openers-config",
    )
    return repo


def _accept_enter(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    lifecycle_run_id: str,
    symbol: str,
    quantity: float,
) -> EnterSubmission:
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=lifecycle_run_id,
    )
    accepted = accept_enter(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        decision_id=f"{strategy_instance_id}-enter",
        lifecycle_run_id=lifecycle_run_id,
        leg=BrokerOrderLeg(symbol=symbol, side="buy", quantity=quantity),
    )
    assert accepted.effect_operation_id is not None and accepted.order_ref is not None
    return accepted


def _append_slice(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    strategy_instance_id: str,
    symbol: str,
    execution_id: str,
    quantity: float,
    price: float,
    filled_at_ms: int,
) -> None:
    """One exact websocket execution slice — what a filled ENTER leaves in the authority."""
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol=symbol,
        side="BUY",
        slice_qty=quantity,
        slice_price=price,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=filled_at_ms,
    )
    transition = TransitionInput(
        strategy_instance_id=strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=filled_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_SLICE_FILLED",
        facts_json=facts.to_facts_json(),
    )
    assert (
        repo.append_execution_slice_if_absent(
            execution_id=execution_id,
            order_ref=accepted.order_ref or "",
            build_transition=lambda: transition,
            build_coverage_conflict=lambda: (_ for _ in ()).throw(
                AssertionError("an exact fixture slice cannot need cumulative recovery")
            ),
        )
        == "appended"
    )


def test_from_database_path_reads_a_running_authority_and_refuses_a_foreign_file(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-1",
    )

    # The repository still holds its execution lease on this database.
    reader = SqliteEconomicProjectionReader.from_database_path(repo.db_path)
    try:
        assert [run.run_id for run in reader.runs_for_strategy(_SID)] == [f"{_SID}:l-1"]
        # A *write* while the foreign reader is open — only that proves the
        # reader never took the lease the repository is still holding.
        repo.clock.advance(1_000)
        submit_stop_run(
            repo,
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            lifecycle_run_id="l-1",
        )
        assert [run.state for run in reader.runs_for_strategy(_SID)] == ["STOPPED"]
    finally:
        reader.close()
        repo.close()

    stranger = tmp_path / "not-a-clerk.db"
    connection = sqlite3.connect(stranger)
    try:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(EconomicProjectionUnavailable, match="not a readable clerk database"):
        SqliteEconomicProjectionReader.from_database_path(stranger)


def test_runs_for_strategy_returns_every_run_oldest_first(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-1",
    )
    repo.clock.advance(1_000)
    submit_stop_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-1",
    )
    repo.clock.advance(1_000)
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        lifecycle_run_id="l-2",
    )

    reader = SqliteEconomicProjectionReader.from_database_path(repo.db_path)
    try:
        runs = reader.runs_for_strategy(_SID)
    finally:
        reader.close()
        repo.close()

    assert [(run.run_id, run.state) for run in runs] == [
        (f"{_SID}:l-1", "STOPPED"),
        (f"{_SID}:l-2", "ACTIVE"),
    ]
    assert runs[0].started_at_ms < runs[1].started_at_ms
    assert runs[0].stopped_at_ms is not None and runs[1].stopped_at_ms is None


def test_economic_fill_source_reads_one_instances_half_open_et_day(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id=_OTHER_SID,
        symbol="MSFT",
        config_hash="twin-openers-other-config",
    )
    mine = _accept_enter(
        repo, strategy_instance_id=_SID, lifecycle_run_id="l-1", symbol="SPY", quantity=30
    )
    theirs = _accept_enter(
        repo, strategy_instance_id=_OTHER_SID, lifecycle_run_id="l-1", symbol="MSFT", quantity=10
    )
    _append_slice(
        repo,
        mine,
        strategy_instance_id=_SID,
        symbol="SPY",
        execution_id="exec-mid-day",
        quantity=10.0,
        price=123.45,
        filled_at_ms=et_minute_of_day_ms(_DAY, 600),
    )
    _append_slice(
        repo,
        mine,
        strategy_instance_id=_SID,
        symbol="SPY",
        execution_id="exec-last-millisecond",
        quantity=10.0,
        price=124.50,
        filled_at_ms=et_day_end_ms(_DAY) - 1,
    )
    _append_slice(
        repo,
        mine,
        strategy_instance_id=_SID,
        symbol="SPY",
        execution_id="exec-next-day",
        quantity=10.0,
        price=125.50,
        filled_at_ms=et_day_end_ms(_DAY),
    )
    _append_slice(
        repo,
        theirs,
        strategy_instance_id=_OTHER_SID,
        symbol="MSFT",
        execution_id="exec-other-instance",
        quantity=10.0,
        price=200.25,
        filled_at_ms=et_minute_of_day_ms(_DAY, 601),
    )

    source = EconomicFillSource.from_database_path(repo.db_path)
    try:
        runs = source.runs_for_strategy(_SID)
        fills = read_twin_fills(
            source,
            strategy_instance_id=_SID,
            session_open_ms=et_minute_of_day_ms(_DAY, 570),
        )
    finally:
        source.close()
        repo.close()

    assert [run.run_id for run in runs] == [f"{_SID}:l-1"]

    # The ET day is half-open: its final millisecond is in, the next day's
    # first is out, and the other instance's fill never belonged to this one.
    assert [fill.filled_at_ms for fill in fills] == [
        et_minute_of_day_ms(_DAY, 600),
        et_day_end_ms(_DAY) - 1,
    ]
    first = fills[0]
    assert first.symbol == "SPY"
    assert first.side == "buy" and type(first.side) is str
    assert isinstance(first.quantity, Decimal) and first.quantity == Decimal("10")
    assert isinstance(first.fill_price, Decimal) and first.fill_price == Decimal("123.45")
    assert str(first.fill_price) == "123.45"  # the float's shortest round trip, not its binary tail
    assert first.order_ref == mine.order_ref
