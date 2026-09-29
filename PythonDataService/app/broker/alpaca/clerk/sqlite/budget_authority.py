"""One durable authorization cutover, replayed by the existing custody mirror.

Schema compatibility excludes older writers before they acquire the lease.
Within this compatible runtime, version 1 alone permits legacy Start; version
2 alone permits fresh budget-backed Deploy. Only version 2 admits a new entry
(#2553): an account still on version 1 refuses every ENTER under
``BUDGETS_NOT_SWITCHED_ON`` until its owner switches it in Settings -- never
automatically. The cutover requires every old runner stopped; positions,
orders, fees and reducing recovery remain facts.
"""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

LEGACY_AUTHORIZATION = 1
BUDGET_AUTHORIZATION = 2
# Post-cutover admission refusal that names the budget commitment itself, so
# consumers keying on reason codes do not misclassify a budget fact as an
# envelope-evidence fact. Transient like the envelope set: the refused ENTER
# is retried on the next decision clock until the operator resolves it with a
# fresh Deploy.
BUDGET_COMMITMENT_MISSING = "BUDGET_COMMITMENT_MISSING"
# The account is still on version 1 (#2553, owner decision 2026-09-29): no
# entry is admitted until its owner switches it to budgets in Settings; it is
# never switched automatically. The same code names the account's attention
# item (``lane_summary``). Account-scoped, so transient like the rest -- a bot
# is never halted for it, and its exits keep working.
BUDGETS_NOT_SWITCHED_ON = "BUDGETS_NOT_SWITCHED_ON"
BUDGETS_NOT_SWITCHED_ON_WHY = (
    "This account has not switched to budgets, so no bot on it can open a new position. "
    "Switch this account to budgets in Settings, then deploy each bot again with its own dollar budget."
)
BUDGET_ADMISSION_REASON_CODES: frozenset[str] = frozenset({BUDGET_COMMITMENT_MISSING, BUDGETS_NOT_SWITCHED_ON})
SCHEMA_V21_STATEMENTS = (
    "ALTER TABLE control_meta ADD COLUMN authorization_version INTEGER NOT NULL DEFAULT 1 CHECK(authorization_version IN (1,2))",
    "CREATE TRIGGER trg_budget_authority_monotonic BEFORE UPDATE OF authorization_version ON control_meta "
    "WHEN OLD.authorization_version <> 1 OR NEW.authorization_version <> 2 "
    "BEGIN SELECT RAISE(ABORT, 'budget authorization cannot be reversed'); END",
)
SCHEMA_V21_DDL = "\n".join(f"{statement};" for statement in SCHEMA_V21_STATEMENTS)


def authorization_version(conn: sqlite3.Connection) -> int:
    schema = conn.execute("SELECT schema_version FROM control_meta WHERE id=1").fetchone()[0]
    if schema < 21:
        return LEGACY_AUTHORIZATION  # Explicit offline historical reader.
    return conn.execute("SELECT authorization_version FROM control_meta WHERE id=1").fetchone()[0]


def authority_review_token(repo: ClerkSqliteRepository) -> str:
    """Bind material account/run identity; observation cadence is not a change."""
    with repo._write_lock:
        meta = repo.control_meta_snapshot()
        runs = [tuple(row) for row in repo._conn.execute(
            "SELECT r.run_id,s.config_hash FROM runs r JOIN strategy_instances s USING(strategy_instance_id) "
            "WHERE r.state='ACTIVE' ORDER BY r.run_id"
        )]
        return canonical_sha256({
            "account_id": repo.account_id, "db_identity": meta.db_identity_token,
            "generation": meta.authority_generation, "version": authorization_version(repo._conn), "active_runs": runs,
        })


def fold_budget_authority_cutover(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = json.loads(payload["facts_json"])
    if authorization_version(conn) != LEGACY_AUTHORIZATION or facts.get("from_version") != 1 or facts.get("to_version") != 2:
        raise ValueError("Budget authority cutover must advance exactly once from version 1 to 2")
    account = conn.execute("SELECT account_id FROM control_meta WHERE id=1").fetchone()[0]
    if facts.get("account_id") != account or not facts.get("actor"):
        raise ValueError("Budget authority cutover must preserve its account and actor")
    if conn.execute("SELECT 1 FROM runs WHERE state='ACTIVE' LIMIT 1").fetchone():
        raise ValueError("Every old run must be stopped before budget authority cutover")
    conn.execute("UPDATE control_meta SET authorization_version=2 WHERE id=1")


def commit_budget_authority_cutover(repo: ClerkSqliteRepository, *, actor: str, reviewed_token: str, stop_receipt: str) -> None:
    """Caller owns the lane mutation/intake fence through Stop and this commit."""
    with repo._write_lock:
        if authorization_version(repo._conn) == BUDGET_AUTHORIZATION:
            return
        if not actor or not reviewed_token or not stop_receipt:
            raise BudgetUnavailable("The reviewed upgrade and Stop evidence are required.")
        if repo._conn.execute("SELECT 1 FROM runs WHERE state='ACTIVE' LIMIT 1").fetchone():
            raise BudgetUnavailable("Stop every earlier deployment before switching this account to budgets.")
        repo.append_transition(TransitionInput(
            transition_kind="BUDGET_AUTHORITY_CUTOVER", custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded", clerk_observed_at_ms=repo.clock(), summary_code="BUDGET_AUTHORITY_CUTOVER",
            facts_json=canonicalize({"from_version": 1, "to_version": 2, "account_id": repo.account_id,
                "actor": actor, "reviewed_token": reviewed_token, "stop_receipt": stop_receipt}),
        ))
