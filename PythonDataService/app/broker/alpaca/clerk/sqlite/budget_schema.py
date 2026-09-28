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
    "risk_revision INTEGER NOT NULL, actor TEXT NOT NULL, committed_at_ms INTEGER NOT NULL, "
    "launched_at_ms INTEGER, released_at_ms INTEGER)",
    "CREATE TRIGGER trg_deployment_budget_identity_immutable "
    "BEFORE UPDATE OF strategy_instance_id, command_id, run_id, world, committed_cents, "
    "configuration_hash, exit_terms_hash, risk_revision, actor, committed_at_ms ON deployment_budgets "
    "BEGIN SELECT RAISE(ABORT, 'deployment budget consent is immutable'); END",
    "CREATE TRIGGER trg_deployment_budget_delete_forbidden BEFORE DELETE ON deployment_budgets "
    "BEGIN SELECT RAISE(ABORT, 'deployment budget commitments are append-only'); END",
)
BUDGET_SCHEMA_DDL = "\n".join(f"{statement};" for statement in BUDGET_SCHEMA_STATEMENTS)
