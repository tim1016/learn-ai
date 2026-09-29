"""Immutable budget commitments and their replayed launch/release evidence."""

from __future__ import annotations

BUDGET_SCHEMA_STATEMENTS = (
    "ALTER TABLE envelope_reservations ADD COLUMN exact_reference_price TEXT",
    "ALTER TABLE envelope_reservations ADD COLUMN fee_provision_cents INTEGER NOT NULL DEFAULT 0",
    "CREATE TABLE deployment_budgets ("
    "strategy_instance_id TEXT PRIMARY KEY REFERENCES strategy_instances(strategy_instance_id), "
    "command_id TEXT NOT NULL UNIQUE REFERENCES commands(command_id), "
    "run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id), "
    "world TEXT NOT NULL CHECK(world IN ('real_paper','real_live','shadow','synthetic')), "
    "committed_cents INTEGER NOT NULL CHECK(typeof(committed_cents) = 'integer' AND committed_cents > 0), "
    "configuration_hash TEXT NOT NULL, exit_terms_hash TEXT NOT NULL, "
    "risk_revision INTEGER NOT NULL, actor TEXT NOT NULL, request_fingerprint TEXT NOT NULL, committed_at_ms INTEGER NOT NULL, "
    "launched_at_ms INTEGER, released_at_ms INTEGER)",
    "CREATE TRIGGER trg_deployment_budget_identity_immutable "
    "BEFORE UPDATE OF strategy_instance_id, command_id, run_id, world, committed_cents, "
    "configuration_hash, exit_terms_hash, risk_revision, actor, request_fingerprint, committed_at_ms ON deployment_budgets "
    "BEGIN SELECT RAISE(ABORT, 'deployment budget consent is immutable'); END",
    "CREATE TRIGGER trg_deployment_budget_delete_forbidden BEFORE DELETE ON deployment_budgets "
    "BEGIN SELECT RAISE(ABORT, 'deployment budget commitments are append-only'); END",
)
BUDGET_SCHEMA_DDL = "\n".join(f"{statement};" for statement in BUDGET_SCHEMA_STATEMENTS)

# v21 -> v22 (#2555): what a Stop released and what stayed claimed then, in
# display cents, folded from its RUN_STOPPED facts onto the budget row it
# released, so a money read takes them without searching the journal. Both
# or neither: a Stop that could not value its release recorded none. The
# backfill fills them from the Stops already recorded; a fresh file has none.
SCHEMA_V22_STATEMENTS = (
    "ALTER TABLE deployment_budgets ADD COLUMN released_cents INTEGER "
    "CHECK(released_cents IS NULL OR (typeof(released_cents) = 'integer' AND released_cents >= 0))",
    "ALTER TABLE deployment_budgets ADD COLUMN held_cents INTEGER "
    "CHECK((held_cents IS NULL) = (released_cents IS NULL) "
    "AND (held_cents IS NULL OR (typeof(held_cents) = 'integer' AND held_cents >= 0)))",
    "UPDATE deployment_budgets SET (released_cents, held_cents) = ("
    "SELECT json_extract(t.facts_json, '$.released_cents'), json_extract(t.facts_json, '$.held_cents') "
    "FROM custody_transitions t WHERE t.strategy_instance_id = deployment_budgets.strategy_instance_id "
    "AND t.run_id = deployment_budgets.run_id AND t.transition_kind = 'RUN_STOPPED' ORDER BY t.sequence LIMIT 1) "
    "WHERE released_at_ms IS NOT NULL",
)
SCHEMA_V22_DDL = "\n".join(f"{statement};" for statement in SCHEMA_V22_STATEMENTS)
