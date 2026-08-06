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


def test_schema_ddl_matches_pinned_contracts_doc() -> None:
    pinned = schema.load_pinned_ddl(REPO_ROOT)
    assert pinned == schema.SCHEMA_DDL


def test_schema_creates_all_fifteen_pinned_tables() -> None:
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
        "positions",
        "holds",
        "uncertainties",
        "reconciliations",
        "receipts",
        "custody_transitions",
        "mirror_fence",
    }


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
