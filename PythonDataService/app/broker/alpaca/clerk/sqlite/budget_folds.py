"""Budget projection folds; no economics or external effects run on replay."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.broker.alpaca.clerk.exit_terms import read_exit_terms
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_facts import DeployCommittedFacts
from app.schemas.account_authority import account_authority_agrees


def fold_deploy_committed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    from app.broker.alpaca.clerk.sqlite.folds import _insert_command_row

    facts = DeployCommittedFacts.from_facts_json(payload["facts_json"])
    sid = payload["strategy_instance_id"]
    account = conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0]
    if not account_authority_agrees(account, facts.world):
        raise ValueError("Budget consent belongs to a different custody pool")
    if payload["run_id"] != f"{sid}:{facts.lifecycle_run_id}":
        raise ValueError("Budget consent must own its one lifecycle run")
    if conn.execute("SELECT 1 FROM runs WHERE strategy_instance_id = ?", (sid,)).fetchone():
        raise ValueError("A fresh Deploy requires a new deployment identity")
    registered = conn.execute("SELECT config_hash FROM strategy_instances WHERE strategy_instance_id = ?", (sid,)).fetchone()
    if registered is None or registered[0] != facts.configuration_hash:
        raise ValueError("Budget consent must match the immutable strategy configuration")
    terms = read_exit_terms(conn, sid)
    if terms is None or canonical_sha256(terms.model_dump(mode="json")) != facts.exit_terms_hash:
        raise ValueError("Budget consent must match the immutable exit terms")
    conn.execute(
        "INSERT INTO runs (run_id,strategy_instance_id,lifecycle_run_id,state,started_at_ms) VALUES (?,?,?,'ACTIVE',?)",
        (payload["run_id"], sid, facts.lifecycle_run_id, payload["recorded_at_ms"]),
    )
    _insert_command_row(
        conn, command_id=payload["command_id"], authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key, payload_hash=facts.payload_hash,
        kind="operator_lifecycle", subject_id=f"bot:{sid}", strategy_instance_id=sid,
        run_id=payload["run_id"], action="DEPLOY", intended_end_state="ACTIVE", state="accepted",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    conn.execute(
        "INSERT INTO deployment_budgets (strategy_instance_id,command_id,run_id,world,committed_cents,"
        "configuration_hash,exit_terms_hash,risk_revision,actor,request_fingerprint,committed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, payload["command_id"], payload["run_id"], facts.world, facts.committed_cents,
         facts.configuration_hash, facts.exit_terms_hash, facts.risk_revision, facts.actor, facts.request_fingerprint, payload["recorded_at_ms"]),
    )


def fold_deploy_launched(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    from app.broker.alpaca.clerk.sqlite.folds import _attach_command_receipt

    row = conn.execute(
        "SELECT b.command_id, b.launched_at_ms, b.released_at_ms, r.state FROM deployment_budgets b "
        "JOIN runs r ON r.run_id=b.run_id WHERE b.strategy_instance_id=? AND b.run_id=?",
        (payload["strategy_instance_id"], payload["run_id"]),
    ).fetchone()
    if row is None or row["command_id"] != payload["command_id"] or row["released_at_ms"] is not None or row["state"] != "ACTIVE":
        raise ValueError("A terminal deployment cannot launch again")
    if row["launched_at_ms"] is not None:
        raise ValueError("Deployment launch is recorded once")
    conn.execute("UPDATE deployment_budgets SET launched_at_ms=? WHERE command_id=?", (payload["recorded_at_ms"], payload["command_id"]))
    conn.execute("UPDATE commands SET state='succeeded', updated_at_ms=? WHERE command_id=?", (payload["recorded_at_ms"], payload["command_id"]))
    _attach_command_receipt(conn, command_id=payload["command_id"], terminal_state="succeeded", payload=payload)


def release_stopped_budget(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """RUN_STOPPED is the append-only release fact, including crash recovery.

    It releases the active free-cash claim, never deletes an order/fill/fee.
    Their claims are reprojected from custody, including later corrections.
    """
    if conn.execute("SELECT schema_version FROM control_meta WHERE id=1").fetchone()[0] < 20:
        # The explicitly offline v9 historical replay precedes this feature.
        return
    row = conn.execute("SELECT command_id,launched_at_ms,released_at_ms FROM deployment_budgets WHERE run_id=?", (payload["run_id"],)).fetchone()
    if row is None or row["released_at_ms"] is not None:
        return
    conn.execute("UPDATE deployment_budgets SET released_at_ms=? WHERE command_id=?", (payload["recorded_at_ms"], row["command_id"]))
    if row["launched_at_ms"] is None:
        from app.broker.alpaca.clerk.sqlite.folds import _attach_command_receipt

        conn.execute("UPDATE commands SET state='failed', updated_at_ms=? WHERE command_id=?", (payload["recorded_at_ms"], row["command_id"]))
        _attach_command_receipt(conn, command_id=row["command_id"], terminal_state="failed", payload=payload)
