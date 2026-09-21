"""SQLite DDL fragments for schema v14: exact simulated execution evidence (#2178).

The main schema module owns the ordered full contract and the migration
registry.  These fragments keep the v14 fills-table replacement — the
``simulated_execution`` evidence vocabulary and the durable-evidence re-tag
of persisted Shadow fills — reviewable as one cohesive unit, the same
separation ``custody_schema_contract.py`` gives the v9 subject boundary.

``SHADOW_SIMULATED_EXECUTION_RETAG_SQL`` is deliberately one shared
statement rather than migration-private logic: the mirror rebuild replays
the immutable transition stream, and a pre-v14 Shadow fill's
``ORDER_FILL_OBSERVED`` facts carry no execution identity, so the replay
re-materializes it as cumulative recovery.  The rebuild therefore re-applies
the same durable-evidence decision after folding (``rebuild.py``), and the
migration and the rebuild can never disagree about which rows are simulated.
"""

from __future__ import annotations

_FILLS_V14_TABLE_DDL = """\
CREATE TABLE fills (
    fill_id                  TEXT PRIMARY KEY,       -- Alpaca execution id (idempotent identity, §9.4)
    order_ref                TEXT NOT NULL REFERENCES orders(order_ref),
    qty                      REAL NOT NULL,
    price                    REAL NOT NULL,
    side                     TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    is_correction             INTEGER NOT NULL DEFAULT 0,  -- 1 = broker-issued correction, not erasure of prior fact
    execution_id             TEXT,                   -- Alpaca execution id; null only for cumulative recovery
    evidence_source          TEXT NOT NULL DEFAULT 'cumulative_recovery'
                              CHECK (evidence_source IN ('websocket','activity_recovery','cumulative_recovery','simulated_execution')),
    event_kind               TEXT NOT NULL DEFAULT 'fill'
                              CHECK (event_kind IN ('fill','correction')),
    superseded_execution_ref TEXT,                   -- correction target; original execution remains auditable
    fee                      REAL,
    fee_fidelity             TEXT NOT NULL DEFAULT 'not_reported'
                              CHECK (fee_fidelity IN ('reported','not_reported')),
    source_event_at_ms       INTEGER,                 -- Alpaca's fill timestamp, when supplied
    clerk_observed_at_ms     INTEGER NOT NULL,
    recorded_at_ms           INTEGER NOT NULL,
    recorded_transition_sequence INTEGER NOT NULL REFERENCES custody_transitions(sequence)
);"""

#: Re-tag only rows whose own durable evidence proves they were synthesized by
#: the Shadow world: the order's ``broker_order_id`` is the synthesized
#: ``shadow-order:<client_order_id>`` identity and the order owns exactly one
#: cumulative fill (a no-submit adapter fills an order exactly once, so a
#: multi-row order is ambiguous and stays untouched).  Real Paper/Live
#: cumulative recovery rows are untouched by construction, and the re-derived
#: ``shadow-execution:`` identity is the Shadow world's own namespace, never a
#: fabricated broker receipt.  The Dry-Run world's legacy ``sim-order:`` rows
#: are deliberately not re-tagged; they convert lazily through the
#: auto-supersession proof when their order is next observed.
SHADOW_SIMULATED_EXECUTION_RETAG_SQL = (
    "UPDATE fills SET execution_id = 'shadow-execution:' || fills.order_ref, "
    "evidence_source = 'simulated_execution' "
    "WHERE fills.evidence_source = 'cumulative_recovery' "
    "AND fills.execution_id IS NULL "
    "AND (SELECT COUNT(*) FROM fills other WHERE other.order_ref = fills.order_ref "
    "AND other.evidence_source = 'cumulative_recovery') = 1 "
    "AND EXISTS (SELECT 1 FROM orders o WHERE o.order_ref = fills.order_ref "
    "AND o.client_order_id = o.order_ref "
    "AND o.broker_order_id = 'shadow-order:' || o.order_ref)"
)

#: v13 -> v14: a deterministic no-submit adapter's authoritative fills keep
#: their exact simulated execution identity instead of the generic cumulative
#: recovery classification (#2178).  SQLite cannot ALTER a CHECK constraint,
#: so the fills table is replaced with one whose evidence_source vocabulary
#: admits 'simulated_execution' (the v11 -> v12 holds replacement is the
#: precedent) before the re-tag runs inside the same transaction.
SCHEMA_V14_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ux_fills_execution_id",
    "ALTER TABLE fills RENAME TO fills_v13_legacy",
    _FILLS_V14_TABLE_DDL,
    "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
    "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
    "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
    "SELECT fill_id, order_ref, qty, price, side, is_correction, execution_id, "
    "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
    "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence "
    "FROM fills_v13_legacy",
    SHADOW_SIMULATED_EXECUTION_RETAG_SQL,
    "DROP TABLE fills_v13_legacy",
    "CREATE UNIQUE INDEX ux_fills_execution_id ON fills(execution_id) "
    "WHERE execution_id IS NOT NULL",
)
