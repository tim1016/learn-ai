"""Guiding-philosophy #5 parity: schema.py's DDL must match the pinned doc.

If this test fails, either the doc changed without updating ``schema.py`` (or
vice versa) — the pinned-contracts document is the reference, this module is
the canonical implementation, and this test is what keeps them from
drifting.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.broker.alpaca.clerk.sqlite import schema

REPO_ROOT = Path(__file__).resolve().parents[6]

_V6_AUTHORITY_DDL = """
CREATE TABLE control_meta (id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL);
CREATE TABLE strategy_instances (strategy_instance_id TEXT PRIMARY KEY);
CREATE TABLE runs (id INTEGER PRIMARY KEY);
CREATE TABLE commands (id INTEGER PRIMARY KEY);
CREATE TABLE effect_operations (id INTEGER PRIMARY KEY);
CREATE TABLE orders (order_ref TEXT PRIMARY KEY);
CREATE TABLE operation_order_links (id INTEGER PRIMARY KEY);
CREATE TABLE fills (
    fill_id TEXT PRIMARY KEY,
    order_ref TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    side TEXT NOT NULL,
    is_correction INTEGER NOT NULL DEFAULT 0,
    source_event_at_ms INTEGER,
    clerk_observed_at_ms INTEGER NOT NULL,
    recorded_at_ms INTEGER NOT NULL
);
CREATE TABLE positions (id INTEGER PRIMARY KEY);
CREATE TABLE holds (id INTEGER PRIMARY KEY);
CREATE TABLE uncertainties (id INTEGER PRIMARY KEY);
CREATE TABLE reconciliations (id INTEGER PRIMARY KEY);
CREATE TABLE receipts (id INTEGER PRIMARY KEY);
CREATE TABLE custody_transitions (
    sequence INTEGER PRIMARY KEY,
    order_ref TEXT,
    facts_json TEXT
);
CREATE TABLE mirror_fence (id INTEGER PRIMARY KEY);
"""


def _empty_v6_authority(conn: sqlite3.Connection) -> None:
    conn.executescript(_V6_AUTHORITY_DDL)
    conn.execute("INSERT INTO control_meta (id, schema_version) VALUES (1, 6)")
    conn.commit()


def test_schema_ddl_matches_pinned_contracts_doc() -> None:
    pinned = schema.load_pinned_ddl(REPO_ROOT)
    assert pinned == schema.SCHEMA_DDL


def test_schema_creates_all_twenty_two_pinned_tables() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
        )
    }
    assert tables == {
        "control_meta",
        "strategy_instances",
        "runs",
        "custody_subjects",
        "commands",
        "effect_operations",
        "orders",
        "operation_order_links",
        "fills",
        "external_orders",
        "bot_config",
        "decision_receipts",
        "positions",
        "holds",
        "uncertainties",
        "manual_order_tickets",
        "manual_order_legs",
        "manual_order_cancellations",
        "reconciliations",
        "receipts",
        "custody_transitions",
        "mirror_fence",
    }


def test_v9_execution_provenance_and_custody_subject_schema() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)

    fills_columns = {
        row[1]: row
        for row in conn.execute("PRAGMA table_info(fills)")
    }
    assert set(fills_columns) >= {
        "execution_id",
        "evidence_source",
        "event_kind",
        "superseded_execution_ref",
        "fee",
        "fee_fidelity",
        "recorded_transition_sequence",
    }
    assert fills_columns["execution_id"][3] == 0
    assert fills_columns["evidence_source"][4] == "'cumulative_recovery'"
    assert fills_columns["event_kind"][4] == "'fill'"
    assert fills_columns["fee_fidelity"][4] == "'not_reported'"

    index_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "ux_fills_execution_id",
        "ux_external_orders_broker_order_id",
        "ix_decision_receipts_strategy_observed_at",
        "ux_manual_order_legs_command",
        "ux_manual_order_legs_effect",
        "ux_manual_order_legs_order",
        "ix_manual_order_cancellations_effect",
    } <= index_names

    command_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(commands)")}
    effect_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(effect_operations)")}
    assert command_columns["subject_id"][3] == 1
    assert effect_columns["subject_id"][3] == 1
    assert command_columns["strategy_instance_id"][3] == 0
    assert effect_columns["strategy_instance_id"][3] == 0


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
            "UPDATE custody_subjects SET operator_id = 'operator-b' "
            "WHERE subject_id = 'manual-operator:operator-a'"
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
    with pytest.raises(sqlite3.IntegrityError, match="hold subject"):
        conn.execute(
            "INSERT INTO holds (hold_id, scope, subject_id, strategy_instance_id, reason_code, state, opened_at_ms) "
            "VALUES ('cross-hold', 'CUSTODY_SUBJECT', 'manual-operator:operator-a', 'spy', 'X', 'ACTIVE', 1)"
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
        conn.execute(
            "UPDATE manual_order_tickets SET operator_id = 'operator-b' "
            "WHERE ticket_id = 'manual-ticket'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_tickets are append-only"):
        conn.execute("DELETE FROM manual_order_tickets WHERE ticket_id = 'manual-ticket'")
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_legs identity is immutable"):
        conn.execute(
            "UPDATE manual_order_legs SET leg_id = 'renamed-leg' "
            "WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="manual_order_legs are append-only"):
        conn.execute(
            "DELETE FROM manual_order_legs WHERE ticket_id = 'manual-ticket' AND leg_id = 'leg-a'"
        )
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
            "UPDATE manual_order_cancellations SET cancel_request_id = 'other-request' "
            "WHERE order_ref = 'manual-order'"
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


def test_empty_v6_authority_stops_at_the_required_offline_v8_to_v9_ceremony() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    _empty_v6_authority(conn)

    import pytest

    with pytest.raises(schema.OfflineSchemaUpgradeRequired, match="offline v8-to-v9"):
        schema.migrate_schema(conn, from_version=6)

    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 6
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'decision_receipts'"
    ).fetchone() is None


def test_data_bearing_v6_authority_fails_closed_without_schema_mutation() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    _empty_v6_authority(conn)
    conn.execute("INSERT INTO custody_transitions (sequence) VALUES (1)")
    conn.commit()
    fills_before = list(conn.execute("PRAGMA table_info(fills)"))

    with pytest.raises(ValueError, match="requires an empty authority"):
        schema.migrate_schema(conn, from_version=6)

    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 6
    assert list(conn.execute("PRAGMA table_info(fills)")) == fills_before
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'decision_receipts'"
    ).fetchone() is None


def test_v6_to_v8_migration_rolls_back_partial_ddl_on_failure() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    _empty_v6_authority(conn)
    conn.execute("CREATE TABLE external_orders (id INTEGER PRIMARY KEY)")
    conn.commit()
    fills_before = list(conn.execute("PRAGMA table_info(fills)"))

    with pytest.raises(sqlite3.OperationalError, match="external_orders"):
        schema.migrate_schema(conn, from_version=6)

    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 6
    assert list(conn.execute("PRAGMA table_info(fills)")) == fills_before


def test_partial_unique_indexes_allow_only_one_active_safety_cause() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    conn.execute(
        "INSERT INTO holds (hold_id, scope, strategy_instance_id, reason_code, state, "
        "opened_at_ms) VALUES ('h1', 'ACCOUNT_CLERK', NULL, 'FOREIGN', 'ACTIVE', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO holds (hold_id, scope, strategy_instance_id, reason_code, state, "
            "opened_at_ms) VALUES ('h2', 'ACCOUNT_CLERK', NULL, 'FOREIGN', 'ACTIVE', 2)"
        )
    conn.execute("UPDATE holds SET state = 'RESOLVED', resolved_at_ms = 3 WHERE hold_id = 'h1'")
    conn.execute(
        "INSERT INTO holds (hold_id, scope, strategy_instance_id, reason_code, state, "
        "opened_at_ms) VALUES ('h2', 'ACCOUNT_CLERK', NULL, 'FOREIGN', 'ACTIVE', 4)"
    )

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
