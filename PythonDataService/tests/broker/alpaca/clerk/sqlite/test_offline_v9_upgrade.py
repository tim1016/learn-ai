"""Production-shaped v8-to-v9 ceremony coverage for custody-subject schema v9."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import offline_v9_upgrade
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.facts import (
    CustodySubjectRegisteredFacts,
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
    ManualTicketLegReservedFacts,
    ManualTicketReservedFacts,
    ManualTicketStateFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.offline_v9_upgrade import (
    OfflineV9UpgradeRefused,
    rollback_v9_upgrade_offline,
    upgrade_v8_authority_offline,
)
from app.broker.alpaca.clerk.sqlite.recovery import ProcessStopProof
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.paths import safe_path_component
from app.broker.contract.models import BrokerOrderLeg

ACCOUNT_ID = "PA-V9-UPGRADE"
STRATEGY_ID = "spy-upgrade"


class _Clock:
    def __init__(self) -> None:
        self.value = 1_800_000_000_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _v8_schema() -> str:
    fixture = Path(__file__).parents[4] / "fixtures" / "sqlite-clerk-v8-schema.sql"
    return fixture.read_text(encoding="utf-8")


def _copy_as_production_v8(*, v9_path: Path, v8_path: Path) -> None:
    """Materialize a real v8 physical layout from a production-like v8 data set."""
    source = sqlite3.connect(v9_path)
    source.row_factory = sqlite3.Row
    destination = sqlite3.connect(v8_path)
    try:
        destination.executescript(_v8_schema())
        destination.execute("PRAGMA foreign_keys = OFF")
        destination.execute("ATTACH DATABASE ? AS v9", (str(v9_path),))
        tables = [
            row[0]
            for row in destination.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
            )
        ]
        for table in tables:
            destination_columns = [
                row[1] for row in destination.execute(f"PRAGMA table_info({table})")
            ]
            source_columns = {
                row[1] for row in source.execute(f"PRAGMA table_info({table})")
            }
            columns = [column for column in destination_columns if column in source_columns]
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            select_columns = quoted_columns
            if table in {"holds", "uncertainties"}:
                select_columns = select_columns.replace(
                    '"scope"',
                    "CASE scope WHEN 'CUSTODY_SUBJECT' THEN 'BOT' ELSE scope END",
                )
            destination.execute(
                f'INSERT INTO "{table}" ({quoted_columns}) '
                f'SELECT {select_columns} FROM v9."{table}"'
            )
        destination.execute("UPDATE control_meta SET schema_version = 8 WHERE id = 1")
        destination.commit()
    finally:
        destination.close()
        source.close()


def _seed_production_v8(tmp_path: Path) -> tuple[_Clock, Path]:
    """Create v8 data with an open order, position, uncertainty, fill, correction, and hash chain."""
    clock = _Clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(
        strategy_instance_id=STRATEGY_ID,
        symbol="SPY",
        config_hash="upgrade-config",
    )
    started = submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=STRATEGY_ID,
        lifecycle_run_id="upgrade-run",
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=STRATEGY_ID,
        decision_id="upgrade-entry",
        lifecycle_run_id="upgrade-run",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=3),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    exact = ExecutionSliceFilledFacts(
        execution_id="exec-upgrade-1",
        symbol="SPY",
        side="BUY",
        slice_qty=3.0,
        slice_price=100.0,
        source_event_at_ms=clock(),
        evidence_source="websocket",
        fee=None,
        fee_fidelity="not_reported",
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=STRATEGY_ID,
            run_id=started.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=exact.source_event_at_ms,
            clerk_observed_at_ms=clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=exact.to_facts_json(),
        )
    )
    correction = ExecutionCorrectedFacts(
        execution_id="exec-upgrade-2",
        superseded_execution_ref=exact.execution_id,
        symbol="SPY",
        side="BUY",
        corrected_qty=2.0,
        corrected_price=101.0,
        why="production-shaped correction coverage",
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=STRATEGY_ID,
            run_id=started.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="EXECUTION_CORRECTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=clock(),
            clerk_observed_at_ms=clock(),
            summary_code="EXECUTION_CORRECTED",
            facts_json=correction.to_facts_json(),
        )
    )
    assert raise_uncertainty(
        repo,
        strategy_instance_id=STRATEGY_ID,
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="Upgrade fixture uncertainty",
        explanation="The source keeps a subject-scoped uncertainty through migration.",
        operator_impact="New exposure remains blocked.",
        next_step="Reconcile the exact broker order.",
        evidence_refs=(accepted.order_ref,),
        cause_facts={"order_ref": accepted.order_ref},
    )
    repo.close()

    account_dir = tmp_path / "accounts" / "alpaca" / safe_path_component(ACCOUNT_ID, "account_id")
    v9_path = account_dir / "clerk.db"
    v8_path = tmp_path / "production-v8.db"
    _copy_as_production_v8(v9_path=v9_path, v8_path=v8_path)
    for suffix in ("-wal", "-shm"):
        (account_dir / f"clerk.db{suffix}").unlink(missing_ok=True)
    os.replace(v8_path, v9_path)
    return clock, v9_path


def _stop_proof(clock: _Clock) -> ProcessStopProof:
    return ProcessStopProof(
        account_id=ACCOUNT_ID,
        observed_at_ms=clock(),
        proof_reference="tests/offline-v9-upgrade-process-stop.json",
    )


def _transition_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM custody_transitions").fetchone()[0]
    finally:
        conn.close()


def test_offline_upgrade_replays_a_production_shaped_v8_authority_and_is_idempotent(
    tmp_path: Path,
) -> None:
    clock, db_path = _seed_production_v8(tmp_path)
    before_count = _transition_count(db_path)
    source = sqlite3.connect(db_path)
    try:
        assert source.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 8
        assert source.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'custody_subjects'"
        ).fetchone() is None
        assert source.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert source.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert source.execute("SELECT COUNT(*) FROM uncertainties").fetchone()[0] == 1
        assert source.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    finally:
        source.close()

    receipt = upgrade_v8_authority_offline(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        process_stop_proof=_stop_proof(clock),
        clock=clock,
    )

    assert receipt.applied is True
    assert receipt.transition_count == before_count
    assert _transition_count(db_path) == before_count
    upgraded = sqlite3.connect(db_path)
    try:
        assert upgraded.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 9
        assert upgraded.execute(
            "SELECT kind, strategy_instance_id FROM custody_subjects WHERE subject_id = ?",
            (f"bot:{STRATEGY_ID}",),
        ).fetchone() == ("BOT", STRATEGY_ID)
        assert upgraded.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert upgraded.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert upgraded.execute("SELECT COUNT(*) FROM uncertainties").fetchone()[0] == 1
        assert upgraded.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    finally:
        upgraded.close()

    reopened = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    try:
        assert reopened.attributed_positions_for_strategy(STRATEGY_ID) == {"SPY": 2.0}
    finally:
        reopened.close()

    retry = upgrade_v8_authority_offline(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        process_stop_proof=_stop_proof(clock),
        clock=clock,
    )
    assert retry.applied is False
    assert retry.receipt_reference == receipt.receipt_reference
    assert _transition_count(db_path) == before_count


def test_failed_stage_proof_keeps_v8_selected_and_records_a_failure_receipt(tmp_path: Path) -> None:
    clock, db_path = _seed_production_v8(tmp_path)

    def fail_stage(_stage_path: Path) -> None:
        raise RuntimeError("injected stage-proof failure")

    with pytest.raises(RuntimeError, match="stage-proof"):
        upgrade_v8_authority_offline(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            process_stop_proof=_stop_proof(clock),
            clock=clock,
            before_swap=fail_stage,
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 8
    finally:
        conn.close()
    failures = tuple(
        (
            tmp_path
            / "accounts"
            / "alpaca"
            / safe_path_component(ACCOUNT_ID, "account_id")
            / "offline-v9-upgrades"
        ).glob(
            "*.failed.json"
        )
    )
    assert len(failures) == 1
    assert "injected stage-proof failure" in failures[0].read_text(encoding="utf-8")


def test_source_verification_failure_records_a_v8_failure_receipt_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, db_path = _seed_production_v8(tmp_path)

    def fail_verification(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected source verification failure")

    monkeypatch.setattr(offline_v9_upgrade, "verify_database", fail_verification)
    with pytest.raises(RuntimeError, match="source verification"):
        upgrade_v8_authority_offline(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            process_stop_proof=_stop_proof(clock),
            clock=clock,
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 8
    finally:
        conn.close()
    failure = next(
        (
            tmp_path
            / "accounts"
            / "alpaca"
            / safe_path_component(ACCOUNT_ID, "account_id")
            / "offline-v9-upgrades"
        ).glob("*.failed.json")
    )
    assert "injected source verification failure" in failure.read_text(encoding="utf-8")


def test_post_swap_interruption_is_finalized_from_the_durable_prepare_receipt(
    tmp_path: Path,
) -> None:
    clock, db_path = _seed_production_v8(tmp_path)

    def interrupt_after_publication(_published_path: Path) -> None:
        raise RuntimeError("injected post-swap interruption")

    with pytest.raises(RuntimeError, match="post-swap"):
        upgrade_v8_authority_offline(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            process_stop_proof=_stop_proof(clock),
            clock=clock,
            after_swap=interrupt_after_publication,
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 9
    finally:
        conn.close()
    upgrade_root = (
        tmp_path
        / "accounts"
        / "alpaca"
        / safe_path_component(ACCOUNT_ID, "account_id")
        / "offline-v9-upgrades"
    )
    assert len(tuple(upgrade_root.glob("*.prepared.json"))) == 1
    assert not tuple(upgrade_root.glob("*.failed.json"))

    finalized = upgrade_v8_authority_offline(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        process_stop_proof=_stop_proof(clock),
        clock=clock,
    )

    assert finalized.applied is False
    assert len(tuple(upgrade_root.glob("*-v8-to-v9.json"))) == 1


def test_receipt_bound_rollback_restores_only_the_upgrade_backup(tmp_path: Path) -> None:
    clock, db_path = _seed_production_v8(tmp_path)
    upgrade_v8_authority_offline(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        process_stop_proof=_stop_proof(clock),
        clock=clock,
    )

    receipt = rollback_v9_upgrade_offline(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        process_stop_proof=_stop_proof(clock),
        clock=clock,
    )

    assert receipt.account_id == ACCOUNT_ID
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 8
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'custody_subjects'"
        ).fetchone() is None
    finally:
        conn.close()


def test_manual_subject_and_ticket_facts_are_replayable_without_a_bot_or_run(tmp_path: Path) -> None:
    clock = _Clock()
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    subject_id = manual_operator_subject_id("operator-a")
    repo.append_transition(
        TransitionInput(
            transition_kind="CUSTODY_SUBJECT_REGISTERED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=clock(),
            summary_code="CUSTODY_SUBJECT_REGISTERED",
            facts_json=CustodySubjectRegisteredFacts(
                subject_id=subject_id,
                kind="MANUAL_OPERATOR",
                strategy_instance_id=None,
                operator_id="operator-a",
            ).to_facts_json(),
        )
    )
    repo.append_transition(
        TransitionInput(
            transition_kind="MANUAL_TICKET_RESERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="reserved",
            clerk_observed_at_ms=clock(),
            summary_code="MANUAL_TICKET_RESERVED",
            facts_json=ManualTicketReservedFacts(
                ticket_id="ticket-a",
                subject_id=subject_id,
                operator_id="operator-a",
                instruction_hash="request-hash-a",
                legs=(
                    ManualTicketLegReservedFacts("leg-a", "leg-hash-a"),
                    ManualTicketLegReservedFacts("leg-b", "leg-hash-b"),
                ),
            ).to_facts_json(),
        )
    )
    # A replay of the exact reservation is a no-op. This asserts the persisted
    # SQLite rows are compared by value, not by their sqlite3.Row wrapper.
    repo.append_transition(
        TransitionInput(
            transition_kind="MANUAL_TICKET_RESERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="reserved",
            clerk_observed_at_ms=clock(),
            summary_code="MANUAL_TICKET_RESERVED",
            facts_json=ManualTicketReservedFacts(
                ticket_id="ticket-a",
                subject_id=subject_id,
                operator_id="operator-a",
                instruction_hash="request-hash-a",
                legs=(
                    ManualTicketLegReservedFacts("leg-a", "leg-hash-a"),
                    ManualTicketLegReservedFacts("leg-b", "leg-hash-b"),
                ),
            ).to_facts_json(),
        )
    )
    with pytest.raises(ValueError, match="manual ticket legs conflict"):
        repo.append_transition(
            TransitionInput(
                transition_kind="MANUAL_TICKET_RESERVED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="reserved",
                clerk_observed_at_ms=clock(),
                summary_code="MANUAL_TICKET_RESERVED",
                facts_json=ManualTicketReservedFacts(
                    ticket_id="ticket-a",
                    subject_id=subject_id,
                    operator_id="operator-a",
                    instruction_hash="request-hash-a",
                    legs=(ManualTicketLegReservedFacts("leg-a", "leg-hash-a"),),
                ).to_facts_json(),
            )
        )
    repo.append_transition(
        TransitionInput(
            transition_kind="MANUAL_TICKET_PAUSED_UNKNOWN",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="unknown",
            clerk_observed_at_ms=clock(),
            summary_code="MANUAL_TICKET_PAUSED_UNKNOWN",
            facts_json=ManualTicketStateFacts(
                ticket_id="ticket-a",
                subject_id=subject_id,
                state="PAUSED_UNKNOWN",
            ).to_facts_json(),
        )
    )
    repo.append_transition(
        TransitionInput(
            transition_kind="MANUAL_TICKET_CANCELED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=clock(),
            summary_code="MANUAL_TICKET_CANCELED",
            facts_json=ManualTicketStateFacts(
                ticket_id="ticket-a",
                subject_id=subject_id,
                state="CANCELED",
            ).to_facts_json(),
        )
    )
    try:
        transition_rows = repo.custody_transitions()
        assert all(
            row["strategy_instance_id"] is None and row["run_id"] is None
            for row in transition_rows
        )
        ticket = repo._conn.execute(
            "SELECT subject_id, operator_id, instruction_hash, state FROM manual_order_tickets "
            "WHERE ticket_id = 'ticket-a'"
        ).fetchone()
        assert tuple(ticket) == (subject_id, "operator-a", "request-hash-a", "CANCELED")
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM manual_order_legs WHERE ticket_id = 'ticket-a'"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            repo._conn.execute(
                "UPDATE manual_order_tickets SET instruction_hash = 'changed' WHERE ticket_id = 'ticket-a'"
            )
    finally:
        repo.close()

    account_dir = tmp_path / "accounts" / "alpaca" / safe_path_component(ACCOUNT_ID, "account_id")
    for filename in ("clerk.db", "clerk.db-wal", "clerk.db-shm"):
        (account_dir / filename).unlink(missing_ok=True)
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    try:
        rebuilt_ticket = rebuilt._conn.execute(
            "SELECT subject_id, operator_id, instruction_hash, state FROM manual_order_tickets "
            "WHERE ticket_id = 'ticket-a'"
        ).fetchone()
        assert tuple(rebuilt_ticket) == (subject_id, "operator-a", "request-hash-a", "CANCELED")
        assert rebuilt._conn.execute(
            "SELECT COUNT(*) FROM manual_order_legs WHERE ticket_id = 'ticket-a'"
        ).fetchone()[0] == 2
    finally:
        rebuilt.close()


def test_offline_upgrade_refuses_stale_process_stop_proof_without_mutating_v8(tmp_path: Path) -> None:
    clock, db_path = _seed_production_v8(tmp_path)
    stale = ProcessStopProof(
        account_id=ACCOUNT_ID,
        observed_at_ms=clock.value - 60_001,
        proof_reference="tests/stale-stop-proof.json",
    )
    with pytest.raises(OfflineV9UpgradeRefused, match="stale"):
        upgrade_v8_authority_offline(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            process_stop_proof=stale,
            clock=clock,
        )
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 8
    finally:
        conn.close()


def test_offline_upgrade_refuses_an_active_execution_lease_without_mutating_v8(tmp_path: Path) -> None:
    clock, db_path = _seed_production_v8(tmp_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE control_meta SET execution_lease_owner = 'live-worker', "
            "execution_lease_expires_at_ms = ? WHERE id = 1",
            (clock.value + 1_000,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(OfflineV9UpgradeRefused, match="lease is active"):
        upgrade_v8_authority_offline(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            process_stop_proof=_stop_proof(clock),
            clock=clock,
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 8
    finally:
        conn.close()
