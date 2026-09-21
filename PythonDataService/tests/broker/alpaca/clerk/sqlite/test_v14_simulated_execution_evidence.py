"""Schema v14: Shadow fills keep their exact simulated execution identity (#2178).

A pre-v14 Shadow authority stored every synthesized fill as
``cumulative_recovery`` with a null ``execution_id``, which made a healthy
Shadow bot read as incomplete execution coverage and raised a false
``needs_attention`` flag. The v13 -> v14 migration widens the
``fills.evidence_source`` vocabulary with ``simulated_execution`` and
re-tags only the rows whose own durable evidence proves they came from the
Shadow world: the order's ``broker_order_id`` is the synthesized
``shadow-order:<client_order_id>`` identity and the order owns exactly one
cumulative fill. Real Paper/Live cumulative recovery rows are untouched by
construction, and nothing fabricates a broker receipt — the re-derived
identity is the Shadow world's own ``shadow-execution:`` namespace.
"""

from __future__ import annotations

import sqlite3

from app.broker.alpaca.clerk.sqlite import reads, schema

SHADOW_ACCOUNT_ID = "shadow:9LIVE0001"
PAPER_ACCOUNT_ID = "PA-V14"
_ORDER_REF = "learn-ai/v14-bot/v1:intent-1"


def _v13_authority(account_id: str) -> sqlite3.Connection:
    """A real v13 file: the historical v9 schema plus its registered upgrades."""
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    conn.row_factory = sqlite3.Row
    schema.apply_v9_schema(conn)
    conn.execute(
        "INSERT INTO control_meta "
        "(id, schema_version, broker, account_id, db_identity_token, authority_generation, "
        "control_revision, created_at_ms, last_open_at_ms, reset_provenance_json, "
        "execution_lease_owner, execution_lease_expires_at_ms) "
        "VALUES (1, 9, 'alpaca', ?, 'identity', 1, 0, 1, 1, NULL, NULL, NULL)",
        (account_id,),
    )
    for version in (9, 10, 11, 12):
        for statement in schema.SCHEMA_MIGRATIONS[version]:
            conn.execute(statement)
    conn.execute("UPDATE control_meta SET schema_version = 13 WHERE id = 1")
    conn.commit()
    return conn


def _insert_legacy_fill(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
    broker_order_id: str,
    quantity: float,
    price: float,
) -> str:
    """One pre-v14 synthesized fill exactly as ``_fold_order_fill_observed`` wrote it.

    The fixture relaxes the foreign-key chain the production writer maintains
    (effects/commands/transitions) — the migration's re-tagging decision reads
    only ``orders`` and ``fills``, which are inserted in full.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT OR IGNORE INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, "
        "role, broker_state, submitted_at_ms, updated_at_ms) VALUES (?, 'fx-v14', ?, ?, "
        "'ENTRY', 'filled', 1, 1)",
        (order_ref, order_ref, broker_order_id),
    )
    conn.execute(
        "INSERT INTO custody_transitions (prev_hash, row_hash, authority_generation, "
        "transition_kind, custody_owner, execution_authority, operation_state, "
        "clerk_observed_at_ms, recorded_at_ms, summary_code, facts_schema_version, facts_json) "
        "VALUES ('GENESIS', 'row-hash', 1, 'ORDER_FILL_OBSERVED', 'ACCOUNT_CLERK', "
        "'ACCOUNT_CLERK', 'in_progress', 1, 1, 'ORDER_FILL_OBSERVED', 1, '{}')",
    )
    sequence = conn.execute(
        "SELECT sequence FROM custody_transitions ORDER BY sequence DESC LIMIT 1"
    ).fetchone()["sequence"]
    fill_id = f"{order_ref}:{quantity:.9f}"
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, 'BUY', 0, NULL, 'cumulative_recovery', 'fill', NULL, NULL, "
        "'not_reported', 1, 1, 1, ?)",
        (fill_id, order_ref, quantity, price, sequence),
    )
    conn.commit()
    return fill_id


def _fill(conn: sqlite3.Connection, fill_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM fills WHERE fill_id = ?", (fill_id,)).fetchone()
    assert row is not None
    return row


def test_v14_migration_retags_shadow_cumulative_fills_as_exact_simulated_executions() -> None:
    conn = _v13_authority(SHADOW_ACCOUNT_ID)
    try:
        fill_id = _insert_legacy_fill(
            conn,
            order_ref=_ORDER_REF,
            broker_order_id=f"shadow-order:{_ORDER_REF}",
            quantity=10.0,
            price=100.25,
        )

        schema.migrate_schema(conn, from_version=13)

        assert conn.execute("SELECT schema_version FROM control_meta").fetchone()["schema_version"] == (
            schema.SCHEMA_VERSION
        )
        row = _fill(conn, fill_id)
        assert row["execution_id"] == f"shadow-execution:{_ORDER_REF}"
        assert row["evidence_source"] == "simulated_execution"
        # The identity the pre-v14 fold minted stays auditable; only its
        # classification and exact identity are restored.
        assert row["fill_id"] == fill_id
        assert row["qty"] == 10.0
        assert row["price"] == 100.25
        # The execution-coverage gate keys on exactly this classification:
        # with no cumulative row left, a healthy Shadow bot's coverage can
        # read complete on the next projection, with no operator reconcile.
        assert not reads.cumulative_recovery_fill_exists_for_order(conn, _ORDER_REF)
    finally:
        conn.close()


def test_v14_migration_leaves_real_recovery_evidence_cumulative() -> None:
    conn = _v13_authority(PAPER_ACCOUNT_ID)
    try:
        fill_id = _insert_legacy_fill(
            conn,
            order_ref=_ORDER_REF,
            broker_order_id="real-broker-order-7",
            quantity=10.0,
            price=100.25,
        )

        schema.migrate_schema(conn, from_version=13)

        row = _fill(conn, fill_id)
        assert row["execution_id"] is None
        assert row["evidence_source"] == "cumulative_recovery"
    finally:
        conn.close()


def test_v14_migration_refuses_to_retag_without_the_durable_shadow_order_identity() -> None:
    """A shadow-namespaced authority still only re-tags rows whose own order
    proves it was synthesized — a live-broker order id is never re-read as a
    simulated execution, and an ambiguous multi-row order stays untouched."""
    conn = _v13_authority(SHADOW_ACCOUNT_ID)
    try:
        unproven = _insert_legacy_fill(
            conn,
            order_ref=_ORDER_REF,
            broker_order_id="live-broker-order-9",
            quantity=10.0,
            price=100.25,
        )
        second_ref = "learn-ai/v14-bot/v1:intent-2"
        first = _insert_legacy_fill(
            conn,
            order_ref=second_ref,
            broker_order_id=f"shadow-order:{second_ref}",
            quantity=4.0,
            price=100.0,
        )
        partial = _insert_legacy_fill(
            conn,
            order_ref=second_ref,
            broker_order_id=f"shadow-order:{second_ref}",
            quantity=6.0,
            price=101.0,
        )

        schema.migrate_schema(conn, from_version=13)

        assert _fill(conn, unproven)["evidence_source"] == "cumulative_recovery"
        assert _fill(conn, first)["evidence_source"] == "cumulative_recovery"
        assert _fill(conn, partial)["evidence_source"] == "cumulative_recovery"
    finally:
        conn.close()


def test_a_fresh_authority_admits_simulated_execution_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        schema.configure_connection(conn)
        schema.apply_schema(conn)
        # The vocabulary check only: the fixture row needs no parent rows.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO fills (fill_id, order_ref, qty, price, side, execution_id, "
            "evidence_source, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
            "VALUES ('shadow-execution:x', 'ref-x', 1, 1, 'BUY', 'shadow-execution:x', "
            "'simulated_execution', 1, 1, 0)"
        )
        assert (
            conn.execute(
                "SELECT evidence_source FROM fills WHERE fill_id = 'shadow-execution:x'"
            ).fetchone()[0]
            == "simulated_execution"
        )
    finally:
        conn.close()
