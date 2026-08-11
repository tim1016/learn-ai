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
CREATE TABLE custody_transitions (id INTEGER PRIMARY KEY);
CREATE TABLE mirror_fence (id INTEGER PRIMARY KEY);
"""


def _empty_v6_authority(conn: sqlite3.Connection) -> None:
    conn.executescript(_V6_AUTHORITY_DDL)
    conn.execute("INSERT INTO control_meta (id, schema_version) VALUES (1, 6)")
    conn.commit()


def test_schema_ddl_matches_pinned_contracts_doc() -> None:
    pinned = schema.load_pinned_ddl(REPO_ROOT)
    assert pinned == schema.SCHEMA_DDL


def test_schema_creates_all_eighteen_pinned_tables() -> None:
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
        "reconciliations",
        "receipts",
        "custody_transitions",
        "mirror_fence",
    }


def test_v7_execution_provenance_and_bounded_receipts_schema() -> None:
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
    } <= index_names


def test_empty_v6_authority_migrates_atomically_to_complete_v7_schema() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    _empty_v6_authority(conn)

    schema.migrate_schema(conn, from_version=6)

    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 7
    assert {
        row[1]
        for row in conn.execute("PRAGMA table_info(fills)")
    } >= {
        "execution_id",
        "evidence_source",
        "event_kind",
        "superseded_execution_ref",
        "fee",
        "fee_fidelity",
    }
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"external_orders", "bot_config", "decision_receipts"} <= tables
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "ux_fills_execution_id",
        "ux_external_orders_broker_order_id",
        "ix_decision_receipts_strategy_observed_at",
    } <= indexes


def test_data_bearing_v6_authority_fails_closed_without_schema_mutation() -> None:
    import pytest

    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    _empty_v6_authority(conn)
    conn.execute("INSERT INTO custody_transitions (id) VALUES (1)")
    conn.commit()
    fills_before = list(conn.execute("PRAGMA table_info(fills)"))

    with pytest.raises(ValueError, match="requires an empty authority"):
        schema.migrate_schema(conn, from_version=6)

    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == 6
    assert list(conn.execute("PRAGMA table_info(fills)")) == fills_before
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'decision_receipts'"
    ).fetchone() is None


def test_v6_to_v7_migration_rolls_back_partial_ddl_on_failure() -> None:
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
        "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, "
        "started_at_ms, stopped_at_ms) VALUES ('run-1', 'spy', 'lifecycle-1', 'ACTIVE', 1, NULL)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, "
            "started_at_ms, stopped_at_ms) VALUES ('run-2', 'spy', 'lifecycle-2', 'ACTIVE', 2, NULL)"
        )

    command_values = (
        "1, 'command-key', 'payload', 'strategy_decision', 'spy', NULL, 'ENTER', "
        "NULL, 'accepted', NULL, NULL, 1, 1"
    )
    conn.execute(
        "INSERT INTO commands (command_id, authority_generation, idempotency_key, payload_hash, "
        "kind, strategy_instance_id, run_id, action, intended_end_state, state, "
        "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        f"VALUES ('command-1', {command_values})"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO commands (command_id, authority_generation, idempotency_key, payload_hash, "
            "kind, strategy_instance_id, run_id, action, intended_end_state, state, "
            "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
            f"VALUES ('command-2', {command_values})"
        )

    effect_values = (
        "1, 'effect-key', 'command-1', 'spy', NULL, 'ENTER', 'accepted', "
        "'ACCOUNT_CLERK', 1, 1, NULL, NULL, NULL, NULL, NULL"
    )
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, idempotency_key, "
        "command_id, strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms, terminal_receipt_id, claim_owner, claim_token, claimed_at_ms, claim_expires_at_ms) "
        f"VALUES ('effect-1', {effect_values})"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO effect_operations (effect_operation_id, authority_generation, idempotency_key, "
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
