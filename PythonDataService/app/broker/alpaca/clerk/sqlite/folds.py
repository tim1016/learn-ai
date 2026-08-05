"""Fold registry — the pure, replayable current-state derivation PRD Phase 1
calls for ("keep the fold a pure, replayable function").

A fold is keyed by ``transition_kind`` (not passed as an ad-hoc closure at
append time) so the *same* lookup drives both live appends and mirror-rebuild
replay — rebuild only has the recovered ``transition_kind`` + payload to work
from, not whatever closure a live caller happened to pass in.

Slice 2 (#1375) registers exactly one fold, ``STRATEGY_INSTANCE_REGISTERED``,
because bot registration is the one piece of business state this slice
legitimately owns (it needs no command/effect lifecycle to be meaningful).
Slices 3+ extend ``DEFAULT_FOLD_REGISTRY`` with their own transition kinds
rather than growing an if/elif chain here or in the repository.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from app.broker.alpaca.clerk.sqlite.facts import (
    CommandRejectedFacts,
    RunStartedFacts,
    RunStoppedFacts,
)

FoldFn = Callable[[sqlite3.Connection, dict[str, Any]], None]


class UnregisteredTransitionKind(Exception):
    """Fail-closed: an unrecognized transition_kind must not be silently skipped."""


class FoldRegistry:
    """Maps ``transition_kind`` to the pure function that applies its fold."""

    def __init__(self) -> None:
        self._folds: dict[str, FoldFn] = {}

    def register(self, transition_kind: str, fold: FoldFn) -> None:
        if transition_kind in self._folds:
            raise ValueError(f"transition_kind {transition_kind!r} already registered")
        self._folds[transition_kind] = fold

    def apply(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        """Look up and run the fold for ``payload['transition_kind']``.

        Raises :class:`UnregisteredTransitionKind` rather than silently doing
        nothing — an unknown kind during either a live append or a mirror
        rebuild is a program bug (a caller must register before using a new
        kind), not a case to degrade past quietly.
        """
        kind = payload["transition_kind"]
        fold = self._folds.get(kind)
        if fold is None:
            raise UnregisteredTransitionKind(kind)
        fold(conn, payload)


def _fold_strategy_instance_registered(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    import json

    facts = json.loads(payload["facts_json"])
    conn.execute(
        "INSERT INTO strategy_instances "
        "(strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms) "
        "VALUES (?, ?, ?, ?, NULL)",
        (
            payload["strategy_instance_id"],
            facts["symbol"],
            facts["config_hash"],
            payload["recorded_at_ms"],
        ),
    )


def _insert_command_row(
    conn: sqlite3.Connection,
    *,
    command_id: str,
    authority_generation: int,
    idempotency_key: str,
    payload_hash: str,
    kind: str,
    strategy_instance_id: str,
    run_id: str | None,
    action: str,
    intended_end_state: str | None,
    state: str,
    recorded_at_ms: int,
) -> None:
    """The one INSERT that creates a ``commands`` row — corrective foundation
    slice, Scope B: a command first becomes durable as part of the canonical
    transition whose fold this is, never via a standalone reservation write.
    Always inserted already in its terminal state; ``reserved`` is never a
    persisted value under this model.
    """
    conn.execute(
        "INSERT INTO commands (command_id, authority_generation, idempotency_key, "
        "payload_hash, kind, strategy_instance_id, run_id, action, intended_end_state, "
        "state, effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
        (
            command_id,
            authority_generation,
            idempotency_key,
            payload_hash,
            kind,
            strategy_instance_id,
            run_id,
            action,
            intended_end_state,
            state,
            recorded_at_ms,
            recorded_at_ms,
        ),
    )


def _attach_command_receipt(
    conn: sqlite3.Connection, *, command_id: str, terminal_state: str, payload: dict[str, Any]
) -> None:
    """Link the durable terminal receipt (R3's ``receipts.terminal_state``
    vocabulary includes ``rejected`` — an admission-time rejection is itself
    the accepted record of the decision, not merely a state with no proof).
    """
    receipt_id = f"receipt:{command_id}"
    conn.execute(
        "INSERT INTO receipts (receipt_id, command_id, effect_operation_id, terminal_state, "
        "summary_code, proof_reference, recorded_at_ms, facts_json) "
        "VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)",
        (
            receipt_id,
            command_id,
            terminal_state,
            payload["summary_code"],
            payload["recorded_at_ms"],
            payload["facts_json"],
        ),
    )
    conn.execute(
        "UPDATE commands SET receipt_id = ? WHERE command_id = ?",
        (receipt_id, command_id),
    )


def _fold_run_started(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = RunStartedFacts.from_facts_json(payload["facts_json"])
    conn.execute(
        "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, "
        "started_at_ms, stopped_at_ms) VALUES (?, ?, ?, 'ACTIVE', ?, NULL)",
        (
            payload["run_id"],
            payload["strategy_instance_id"],
            facts.lifecycle_run_id,
            payload["recorded_at_ms"],
        ),
    )
    _insert_command_row(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind=facts.kind,
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="succeeded",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    _attach_command_receipt(
        conn, command_id=payload["command_id"], terminal_state="succeeded", payload=payload
    )


def _fold_run_stopped(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = RunStoppedFacts.from_facts_json(payload["facts_json"])
    conn.execute(
        "UPDATE runs SET state = 'STOPPED', stopped_at_ms = ? WHERE run_id = ?",
        (payload["recorded_at_ms"], payload["run_id"]),
    )
    _insert_command_row(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind=facts.kind,
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="succeeded",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    _attach_command_receipt(
        conn, command_id=payload["command_id"], terminal_state="succeeded", payload=payload
    )


def _fold_command_rejected(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = CommandRejectedFacts.from_facts_json(payload["facts_json"])
    _insert_command_row(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind=facts.kind,
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="rejected",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    _attach_command_receipt(
        conn, command_id=payload["command_id"], terminal_state="rejected", payload=payload
    )


DEFAULT_FOLD_REGISTRY = FoldRegistry()
DEFAULT_FOLD_REGISTRY.register(
    "STRATEGY_INSTANCE_REGISTERED", _fold_strategy_instance_registered
)
DEFAULT_FOLD_REGISTRY.register("RUN_STARTED", _fold_run_started)
DEFAULT_FOLD_REGISTRY.register("RUN_STOPPED", _fold_run_stopped)
DEFAULT_FOLD_REGISTRY.register("COMMAND_REJECTED", _fold_command_rejected)
