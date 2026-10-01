"""``schema.SCHEMA_DDL`` builds the tables, columns, indexes and triggers the
clerk store relies on, and its migrations carry live stores to the current
version intact.

The DDL is the schema's only copy; ADR 0035's binding annex states the
invariants behind it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.broker.alpaca.clerk.sqlite import database_verification, schema
from tests.broker.alpaca.clerk.sqlite.conftest import build_v13_authority


def _authority_built_up_to(conn: sqlite3.Connection, target_version: int) -> None:
    """Build the v13 baseline, then replay the exact registered migration
    chain (``SCHEMA_MIGRATIONS``, via the real ``migrate_schema``) up to
    ``target_version``. ``SCHEMA_VERSION`` is patched for the duration so the
    same production upgrade path used every day is what builds the fixture,
    rather than a second hand-rolled copy of it drifting from the real one."""
    build_v13_authority(conn, account_id="PA1")

    import pytest

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(schema, "SCHEMA_VERSION", target_version)
        schema.migrate_schema(conn, from_version=13)
    finally:
        mp.undo()


def _seed_one_active_run_position_and_working_order(conn: sqlite3.Connection) -> None:
    """Evidence shape a pre-#2550 Paper/Live account has on disk today: one
    ACTIVE run, one open position, one working (unfilled) order."""
    conn.execute(
        "INSERT INTO strategy_instances (strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms) "
        "VALUES ('spy', 'SPY', 'hash', 1, NULL)"
    )
    conn.execute(
        "INSERT INTO custody_subjects (subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES ('bot:spy', 'BOT', 'spy', NULL, 1)"
    )
    conn.execute(
        "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, stopped_at_ms) "
        "VALUES ('run-1', 'spy', 'run-1', 'ACTIVE', 1, NULL)"
    )
    conn.execute(
        "INSERT INTO commands "
        "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, kind, "
        "strategy_instance_id, run_id, action, state, created_at_ms, updated_at_ms) "
        "VALUES ('cmd-1', 1, 'bot:spy', 'cmd-1', 'h', 'strategy_decision', 'spy', 'run-1', "
        "'ENTER', 'accepted', 1, 1)"
    )
    conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
        "VALUES ('eff-1', 1, 'bot:spy', 'eff-1', 'cmd-1', 'spy', 'run-1', 'ENTER', 'accepted', "
        "'ACCOUNT_CLERK', 1, 1)"
    )
    conn.execute(
        "INSERT INTO orders "
        "(order_ref, effect_operation_id, client_order_id, broker_order_id, role, broker_state, "
        "submitted_at_ms, updated_at_ms) "
        "VALUES ('order-1', 'eff-1', 'order-1', NULL, 'ENTRY', 'working', 1, 1)"
    )
    conn.execute(
        "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
        "VALUES ('bot:spy', 'spy', 'SPY', 10, 1)"
    )
    conn.commit()


def _assert_migration_from_version_reaches_current_intact(tmp_path: Path, from_version: int) -> None:
    """Every production Paper/Live custody DB is at v18 (or, mid-rollout,
    already at v19/v20) and runs the new ALTERs on its first open after
    #2550. A reviewer proved by hand that positions/orders/runs survive,
    ``authorization_version`` defaults to 1, and no ``deployment_budgets``
    rows appear; this pins that for v18 through v21."""
    import pytest

    db_path = tmp_path / f"clerk-{from_version}.db"
    conn = sqlite3.connect(db_path)
    schema.configure_connection(conn)
    _authority_built_up_to(conn, from_version)
    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == from_version
    _seed_one_active_run_position_and_working_order(conn)

    schema.migrate_schema(conn, from_version=from_version)

    assert (
        conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0]
        == schema.SCHEMA_VERSION
    )
    assert conn.execute("SELECT authorization_version FROM control_meta WHERE id = 1").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM deployment_budgets").fetchone()[0] == 0
    assert conn.execute("SELECT run_id, state FROM runs WHERE run_id = 'run-1'").fetchone() == (
        "run-1",
        "ACTIVE",
    )
    assert conn.execute(
        "SELECT subject_id, symbol, attributed_qty FROM positions WHERE subject_id = 'bot:spy'"
    ).fetchone() == ("bot:spy", "SPY", 10.0)
    assert conn.execute(
        "SELECT order_ref, broker_state FROM orders WHERE order_ref = 'order-1'"
    ).fetchone() == ("order-1", "working")
    conn.close()

    # Hash-chain/mirror parity: verify_database is the same read-only check
    # backup/restore/cutover run against a live authority file.
    verification = database_verification.verify_database(db_path, expected_account_id="PA1")
    assert verification.schema_version == schema.SCHEMA_VERSION

    # Re-running the migration against the same (now stale) from_version is
    # refused cleanly -- migrate_schema's contract is fail-closed (raise +
    # roll back), not a silent idempotent no-op, once the target columns and
    # tables already exist.
    conn = sqlite3.connect(db_path)
    schema.configure_connection(conn)
    with pytest.raises(sqlite3.OperationalError):
        schema.migrate_schema(conn, from_version=from_version)
    assert (
        conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0]
        == schema.SCHEMA_VERSION
    )
    conn.close()


def test_v18_migration_preserves_evidence_and_defaults_authorization_version(tmp_path: Path) -> None:
    _assert_migration_from_version_reaches_current_intact(tmp_path, 18)


def test_v19_migration_preserves_evidence_and_defaults_authorization_version(tmp_path: Path) -> None:
    _assert_migration_from_version_reaches_current_intact(tmp_path, 19)


def test_v20_migration_preserves_evidence_and_defaults_authorization_version(tmp_path: Path) -> None:
    _assert_migration_from_version_reaches_current_intact(tmp_path, 20)


def test_v21_migration_preserves_evidence_and_defaults_authorization_version(tmp_path: Path) -> None:
    _assert_migration_from_version_reaches_current_intact(tmp_path, 21)


def test_v9_subject_ownership_invariants_reject_counterfeit_and_cross_wired_rows() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    for strategy_instance_id in ("spy", "qqq"):
        conn.execute(
            "INSERT INTO strategy_instances "
            "(strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms) "
            "VALUES (?, ?, 'hash', 1, NULL)",
            (strategy_instance_id, strategy_instance_id.upper()),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO custody_subjects "
            "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
            "VALUES ('counterfeit', 'BOT', 'spy', NULL, 1)"
        )
    conn.execute(
        "INSERT INTO custody_subjects "
        "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES ('bot:spy', 'BOT', 'spy', NULL, 1)"
    )
    conn.execute(
        "INSERT INTO custody_subjects "
        "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES ('bot:qqq', 'BOT', 'qqq', NULL, 1)"
    )
    conn.execute(
        "INSERT INTO custody_subjects "
        "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES ('manual-operator:operator-a', 'MANUAL_OPERATOR', NULL, 'operator-a', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="custody_subjects identity is immutable"):
        conn.execute(
            "UPDATE custody_subjects SET operator_id = 'operator-b' WHERE subject_id = 'manual-operator:operator-a'"
        )

    with pytest.raises(sqlite3.IntegrityError, match="commands subject"):
        conn.execute(
            "INSERT INTO commands "
            "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, kind, "
            "strategy_instance_id, run_id, action, state, created_at_ms, updated_at_ms) "
            "VALUES ('cross-command', 1, 'bot:spy', 'cross-command', 'h', 'strategy_decision', "
            "'qqq', NULL, 'ENTER', 'accepted', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="position subject"):
        conn.execute(
            "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
            "VALUES ('manual-operator:operator-a', 'spy', 'SPY', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="uncertainty subject"):
        conn.execute(
            "INSERT INTO uncertainties "
            "(uncertainty_id, scope, severity, blocks_new_exposure, allows_reduction, subject_id, "
            "strategy_instance_id, reason_code, headline, explanation, operator_impact, next_step, "
            "observed_at_ms, facts_schema_version, facts_json) "
            "VALUES ('cross-uncertainty', 'CUSTODY_SUBJECT', 'warning', 1, 0, "
            "'manual-operator:operator-a', 'spy', 'X', 'h', 'e', 'impact', 'next', 1, 1, '{}')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="manual ticket"):
        conn.execute(
            "INSERT INTO manual_order_tickets "
            "(ticket_id, subject_id, operator_id, instruction_hash, state, created_at_ms, updated_at_ms) "
            "VALUES ('bad-ticket', 'bot:spy', 'operator-a', 'h', 'RESERVED', 1, 1)"
        )

    conn.execute(
        "INSERT INTO commands "
        "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, kind, "
        "strategy_instance_id, run_id, action, state, created_at_ms, updated_at_ms) "
        "VALUES ('bot-command', 1, 'bot:spy', 'bot-command', 'h', 'strategy_decision', "
        "'spy', NULL, 'ENTER', 'accepted', 1, 1)"
    )
    conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
        "VALUES ('bot-effect', 1, 'bot:spy', 'bot-effect', 'bot-command', 'spy', NULL, "
        "'ENTER', 'accepted', 'ACCOUNT_CLERK', 1, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="effect operation"):
        conn.execute(
            "INSERT INTO effect_operations "
            "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
            "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
            "VALUES ('cross-effect', 1, 'bot:qqq', 'cross-effect', 'bot-command', 'qqq', NULL, "
            "'ENTER', 'accepted', 'ACCOUNT_CLERK', 1, 1)"
        )
    conn.execute(
        "INSERT INTO orders "
        "(order_ref, effect_operation_id, client_order_id, role, updated_at_ms) "
        "VALUES ('bot-order', 'bot-effect', 'bot-order', 'ENTRY', 1)"
    )
    conn.execute(
        "INSERT INTO manual_order_tickets "
        "(ticket_id, subject_id, operator_id, instruction_hash, state, created_at_ms, updated_at_ms) "
        "VALUES ('manual-ticket', 'manual-operator:operator-a', 'operator-a', 'h', 'RESERVED', 1, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="manual leg must belong"):
        conn.execute(
            "INSERT INTO manual_order_legs "
            "(ticket_id, leg_id, subject_id, instruction_hash, state, created_at_ms, updated_at_ms) "
            "VALUES ('manual-ticket', 'cross-leg', 'bot:spy', 'leg-h', 'RESERVED', 1, 1)"
        )
    conn.execute(
        "INSERT INTO manual_order_legs "
        "(ticket_id, leg_id, subject_id, instruction_hash, state, created_at_ms, updated_at_ms) "
        "VALUES ('manual-ticket', 'leg-a', 'manual-operator:operator-a', 'leg-h', 'RESERVED', 1, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_tickets identity is immutable"):
        conn.execute("UPDATE manual_order_tickets SET operator_id = 'operator-b' WHERE ticket_id = 'manual-ticket'")
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_tickets are append-only"):
        conn.execute("DELETE FROM manual_order_tickets WHERE ticket_id = 'manual-ticket'")
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_legs identity is immutable"):
        conn.execute(
            "UPDATE manual_order_legs SET leg_id = 'renamed-leg' WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_legs identity is immutable"):
        conn.execute(
            "UPDATE manual_order_legs SET sequence_index = 1 WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_legs are append-only"):
        conn.execute("DELETE FROM manual_order_legs WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'")
    with pytest.raises(sqlite3.IntegrityError, match="manual leg resources"):
        conn.execute(
            "UPDATE manual_order_legs SET command_id = 'bot-command', effect_operation_id = 'bot-effect', "
            "order_ref = 'bot-order' WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'"
        )
    conn.execute(
        "INSERT INTO commands "
        "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, kind, "
        "strategy_instance_id, run_id, action, state, created_at_ms, updated_at_ms) "
        "VALUES ('manual-command', 1, 'manual-operator:operator-a', 'manual-command', 'h', "
        "'manual_order', NULL, NULL, 'SUBMIT_MANUAL_ORDER', 'accepted', 1, 1)"
    )
    conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
        "VALUES ('manual-effect', 1, 'manual-operator:operator-a', 'manual-effect', "
        "'manual-command', NULL, NULL, 'MANUAL_ORDER', 'accepted', 'ACCOUNT_CLERK', 1, 1)"
    )
    conn.execute(
        "INSERT INTO orders "
        "(order_ref, effect_operation_id, client_order_id, role, updated_at_ms) "
        "VALUES ('manual-order', 'manual-effect', 'manual-order', 'MANUAL', 1)"
    )
    conn.execute(
        "UPDATE manual_order_legs SET command_id = 'manual-command', effect_operation_id = "
        "'manual-effect', order_ref = 'manual-order' WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'"
    )
    conn.execute(
        "INSERT INTO commands "
        "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, kind, "
        "strategy_instance_id, run_id, action, state, created_at_ms, updated_at_ms) "
        "VALUES ('manual-cancel-command', 1, 'manual-operator:operator-a', 'manual-cancel', 'h', "
        "'manual_order', NULL, NULL, 'CANCEL_MANUAL_ORDER', 'accepted', 1, 1)"
    )
    conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
        "VALUES ('manual-cancel-effect', 1, 'manual-operator:operator-a', 'manual-cancel-effect', "
        "'manual-cancel-command', NULL, NULL, 'CANCEL', 'accepted', 'ACCOUNT_CLERK', 1, 1)"
    )
    conn.execute(
        "INSERT INTO manual_order_cancellations "
        "(order_ref, subject_id, cancel_request_id, command_id, effect_operation_id, state, "
        "created_at_ms, updated_at_ms) VALUES ('manual-order', 'manual-operator:operator-a', "
        "'cancel-request', 'manual-cancel-command', 'manual-cancel-effect', 'ACCEPTED', 1, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_cancellations identity is immutable"):
        conn.execute(
            "UPDATE manual_order_cancellations SET cancel_request_id = 'other-request' WHERE order_ref = 'manual-order'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_cancellations are append-only"):
        conn.execute("DELETE FROM manual_order_cancellations WHERE order_ref = 'manual-order'")
    with pytest.raises(sqlite3.IntegrityError, match="manual cancellation must own"):
        conn.execute(
            "INSERT INTO manual_order_cancellations "
            "(order_ref, subject_id, cancel_request_id, command_id, effect_operation_id, state, "
            "created_at_ms, updated_at_ms) VALUES ('bot-order', 'bot:spy', 'bot-cancel', "
            "'bot-command', 'bot-effect', 'ACCEPTED', 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="custody_subjects are append-only"):
        conn.execute("DELETE FROM custody_subjects WHERE subject_id = 'bot:qqq'")


def test_partial_unique_indexes_allow_only_one_active_safety_cause() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    # The former ``ux_holds_one_active_cause`` half of this test is gone with
    # the table (ADR 0048 D2). ``ux_uncertainties_one_active_cause`` below is
    # the surviving fence and now covers hold causes too, which is asserted
    # against the compatibility view in
    # ``test_holds_view_shows_one_active_episode_per_hold_cause``.
    conn.execute(
        "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
        "allows_reduction, strategy_instance_id, reason_code, headline, explanation, "
        "operator_impact, next_step, observed_at_ms, facts_schema_version, facts_json) "
        "VALUES ('u1', 'ACCOUNT_CLERK', 'warning', 1, 0, NULL, 'UNKNOWN', 'h', 'e', "
        "'impact', 'step', 1, 1, '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO uncertainties (uncertainty_id, scope, severity, "
            "blocks_new_exposure, allows_reduction, strategy_instance_id, reason_code, "
            "headline, explanation, operator_impact, next_step, observed_at_ms, "
            "facts_schema_version, facts_json) VALUES ('u2', 'ACCOUNT_CLERK', 'warning', "
            "1, 0, NULL, 'UNKNOWN', 'h', 'e', 'impact', 'step', 2, 1, '{}')"
        )
    conn.execute("UPDATE uncertainties SET resolved_at_ms = 3 WHERE uncertainty_id = 'u1'")
    conn.execute(
        "INSERT INTO uncertainties (uncertainty_id, scope, severity, blocks_new_exposure, "
        "allows_reduction, strategy_instance_id, reason_code, headline, explanation, "
        "operator_impact, next_step, observed_at_ms, facts_schema_version, facts_json) "
        "VALUES ('u2', 'ACCOUNT_CLERK', 'warning', 1, 0, NULL, 'UNKNOWN', 'h', 'e', "
        "'impact', 'step', 4, 1, '{}')"
    )


def test_idempotency_indexes_reject_duplicate_rows_at_the_sql_boundary() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    conn.execute(
        "INSERT INTO strategy_instances "
        "(strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms) "
        "VALUES ('spy', 'SPY', 'hash', 1, NULL)"
    )
    conn.execute(
        "INSERT INTO custody_subjects "
        "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES ('bot:spy', 'BOT', 'spy', NULL, 1)"
    )
    conn.execute(
        "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, "
        "started_at_ms, stopped_at_ms) VALUES ('run-1', 'spy', 'lifecycle-1', 'ACTIVE', 1, NULL)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, "
            "started_at_ms, stopped_at_ms) VALUES ('run-2', 'spy', 'lifecycle-2', 'ACTIVE', 2, NULL)"
        )

    command_values = (
        "1, 'bot:spy', 'command-key', 'payload', 'strategy_decision', 'spy', NULL, 'ENTER', "
        "NULL, 'accepted', NULL, NULL, 1, 1"
    )
    conn.execute(
        "INSERT INTO commands (command_id, authority_generation, subject_id, idempotency_key, payload_hash, "
        "kind, strategy_instance_id, run_id, action, intended_end_state, state, "
        "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        f"VALUES ('command-1', {command_values})"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO commands (command_id, authority_generation, subject_id, idempotency_key, payload_hash, "
            "kind, strategy_instance_id, run_id, action, intended_end_state, state, "
            "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
            f"VALUES ('command-2', {command_values})"
        )

    effect_values = (
        "1, 'bot:spy', 'effect-key', 'command-1', 'spy', NULL, 'ENTER', 'accepted', "
        "'ACCOUNT_CLERK', 1, 1, NULL, NULL, NULL, NULL, NULL"
    )
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, subject_id, idempotency_key, "
        "command_id, strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms, terminal_receipt_id, claim_owner, claim_token, claimed_at_ms, claim_expires_at_ms) "
        f"VALUES ('effect-1', {effect_values})"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO effect_operations (effect_operation_id, authority_generation, subject_id, idempotency_key, "
            "command_id, strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
            "updated_at_ms, terminal_receipt_id, claim_owner, claim_token, claimed_at_ms, claim_expires_at_ms) "
            f"VALUES ('effect-2', {effect_values})"
        )

    order_values = "'effect-1', 'client-order-key', NULL, 'ENTRY', NULL, NULL, 1"
    conn.execute(
        "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms) "
        f"VALUES ('order-1', {order_values})"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
            "broker_state, submitted_at_ms, updated_at_ms) "
            f"VALUES ('order-2', {order_values})"
        )


def test_immutability_triggers_block_custody_transitions_mutation() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    conn.execute(
        "INSERT INTO custody_transitions (sequence, prev_hash, row_hash, authority_generation, "
        "strategy_instance_id, run_id, command_id, effect_operation_id, order_ref, "
        "broker_order_id, transition_kind, custody_owner, execution_authority, "
        "operation_state, broker_state, proof_reference, source_event_at_ms, "
        "clerk_observed_at_ms, recorded_at_ms, summary_code, facts_schema_version, facts_json) "
        "VALUES (1, 'GENESIS', 'h', 1, NULL, NULL, NULL, NULL, NULL, NULL, 'K', 'ACCOUNT_CLERK', "
        "'ACCOUNT_CLERK', 'reserved', NULL, NULL, NULL, 1, 1, 'C', 1, '{}')"
    )
    conn.commit()

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE custody_transitions SET operation_state = 'accepted' WHERE sequence = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM custody_transitions WHERE sequence = 1")


def test_mirror_fence_rejects_finalize_phase() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    conn.execute(
        "INSERT INTO custody_transitions (sequence, prev_hash, row_hash, authority_generation, "
        "strategy_instance_id, run_id, command_id, effect_operation_id, order_ref, "
        "broker_order_id, transition_kind, custody_owner, execution_authority, "
        "operation_state, broker_state, proof_reference, source_event_at_ms, "
        "clerk_observed_at_ms, recorded_at_ms, summary_code, facts_schema_version, facts_json) "
        "VALUES (1, 'GENESIS', 'h', 1, NULL, NULL, NULL, NULL, NULL, NULL, 'K', 'ACCOUNT_CLERK', "
        "'ACCOUNT_CLERK', 'reserved', NULL, NULL, NULL, 1, 1, 'C', 1, '{}')"
    )
    conn.commit()

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mirror_fence (sequence, phase, row_hash, authority_generation, recorded_at_ms) "
            "VALUES (1, 'FINALIZE', 'h', 1, 1)"
        )
