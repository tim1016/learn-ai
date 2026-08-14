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

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    FILL_QTY_EPSILON,
    active_execution_coverage_conflicts,
    execution_coverage_proof,
)
from app.broker.alpaca.clerk.sqlite.external_order_folds import (
    fold_external_order_acknowledged as _fold_external_order_acknowledged,
)
from app.broker.alpaca.clerk.sqlite.external_order_folds import (
    fold_external_order_observed as _fold_external_order_observed,
)
from app.broker.alpaca.clerk.sqlite.facts import (
    AccountHoldRaisedFacts,
    AccountHoldResolvedFacts,
    CommandRejectedFacts,
    EnterAcceptedFacts,
    ExecutionCorrectedFacts,
    ExecutionCoverageQuarantinedFacts,
    ExecutionCoverageResolvedFacts,
    ExecutionSliceFilledFacts,
    ExitAcceptedFacts,
    OrderFillObservedFacts,
    ReconciliationAttemptedFacts,
    RunStartedFacts,
    RunStoppedFacts,
    validate_execution_corrected_facts,
    validate_execution_coverage_quarantined_facts,
    validate_execution_coverage_resolved_facts,
    validate_execution_slice_facts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.manual_ticket_folds import (
    fold_custody_subject_registered,
    fold_manual_order_accepted,
    fold_manual_ticket_reserved,
    fold_manual_ticket_state,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import (
    fold_uncertainty_raised as _fold_uncertainty_raised,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import (
    fold_uncertainty_refreshed as _fold_uncertainty_refreshed,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import (
    fold_uncertainty_resolved as _fold_uncertainty_resolved,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import (
    open_or_refresh_unknown_outcome as _open_or_refresh_unknown_outcome,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_folds import (
    resolve_unknown_outcome_if_proven as _resolve_unknown_outcome_if_proven,
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

    @property
    def registered_kinds(self) -> frozenset[str]:
        """Expose the closed transition vocabulary for projection-copy checks."""
        return frozenset(self._folds)

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
    conn.execute(
        "INSERT INTO bot_config "
        "(strategy_instance_id, strategy_key, display_name, config_json, config_hash, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            payload["strategy_instance_id"],
            facts["strategy_key"],
            facts["display_name"],
            facts["config_json"],
            facts["config_hash"],
            payload["recorded_at_ms"],
        ),
    )
    conn.execute(
        "INSERT INTO custody_subjects "
        "(subject_id, kind, strategy_instance_id, operator_id, created_at_ms) "
        "VALUES (?, 'BOT', ?, NULL, ?)",
        (
            bot_subject_id(payload["strategy_instance_id"]),
            payload["strategy_instance_id"],
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
    subject_id: str,
    strategy_instance_id: str | None,
    run_id: str | None,
    action: str,
    intended_end_state: str | None,
    state: str,
    recorded_at_ms: int,
) -> None:
    """The one INSERT that creates a ``commands`` row — corrective foundation
    slice, Scope B: a command first becomes durable as part of the canonical
    transition whose fold this is, never via a standalone reservation write.

    Always inserts with a null ``effect_operation_id``: the operator-lifecycle
    folds below never populate it at all (no effect operation exists for
    Start/Stop), and a broker-facing command (#1377's ENTER) can't populate it
    at insert time either — ``effect_operations.command_id`` is an immediate,
    non-deferred foreign key, so the effect operation can only be inserted
    *after* this row exists. ``_fold_enter_accepted`` backfills the link with
    a separate ``UPDATE`` once its effect operation is created.
    """
    conn.execute(
        "INSERT INTO commands (command_id, authority_generation, idempotency_key, "
        "payload_hash, kind, subject_id, strategy_instance_id, run_id, action, intended_end_state, "
        "state, effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
        (
            command_id,
            authority_generation,
            idempotency_key,
            payload_hash,
            kind,
            subject_id,
            strategy_instance_id,
            run_id,
            action,
            intended_end_state,
            state,
            recorded_at_ms,
            recorded_at_ms,
        ),
    )


def _insert_receipt_row(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    command_id: str,
    effect_operation_id: str | None,
    terminal_state: str,
    payload: dict[str, Any],
) -> None:
    """The one INSERT that creates a ``receipts`` row — shared by both
    terminal-receipt shapes below (operator-lifecycle's fresh insert and
    ENTER's later transition), so the column list and value order are
    defined exactly once."""
    conn.execute(
        "INSERT INTO receipts (receipt_id, command_id, effect_operation_id, terminal_state, "
        "summary_code, proof_reference, recorded_at_ms, facts_json) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            receipt_id,
            command_id,
            effect_operation_id,
            terminal_state,
            payload["summary_code"],
            payload["recorded_at_ms"],
            payload["facts_json"],
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
    _insert_receipt_row(
        conn,
        receipt_id=receipt_id,
        command_id=command_id,
        effect_operation_id=None,
        terminal_state=terminal_state,
        payload=payload,
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
        subject_id=bot_subject_id(payload["strategy_instance_id"]),
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="succeeded",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    _attach_command_receipt(conn, command_id=payload["command_id"], terminal_state="succeeded", payload=payload)


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
        subject_id=bot_subject_id(payload["strategy_instance_id"]),
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="succeeded",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    _attach_command_receipt(conn, command_id=payload["command_id"], terminal_state="succeeded", payload=payload)


def _fold_command_rejected(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = CommandRejectedFacts.from_facts_json(payload["facts_json"])
    _insert_command_row(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind=facts.kind,
        subject_id=bot_subject_id(payload["strategy_instance_id"]),
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="rejected",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    _attach_command_receipt(conn, command_id=payload["command_id"], terminal_state="rejected", payload=payload)


def _fold_enter_accepted(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Pinned contract §4 "Command/effect admission, broker-eligible"
    (#1377): the fold atomically creates ``effect_operations`` and ``orders``
    (both ``accepted`` — no broker or local work has begun yet, per §5's
    state machine) plus the ``commands`` row that references them, all
    before ``commit_first_transition`` returns. Nothing about a broker call
    is durable yet, so there is nothing here for recovery to duplicate, only
    to resolve (R1).

    Insert order is forced by two *immediate* (non-deferred) foreign keys —
    ``effect_operations.command_id`` and ``orders.effect_operation_id`` are
    both ``NOT NULL REFERENCES``, checked per-statement, not at commit like
    ``custody_transitions``'s deferred FKs. ``commands.effect_operation_id``
    is nullable, so the only way to satisfy both immediate FKs in one
    transaction is: insert the command first (effect link still null),
    insert the effect operation (its command now exists), insert the order
    (its effect operation now exists), then backfill the command's link.
    """
    facts = EnterAcceptedFacts.from_facts_json(payload["facts_json"])
    _insert_command_row(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind=facts.kind,
        subject_id=bot_subject_id(payload["strategy_instance_id"]),
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="accepted",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, subject_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', 'ACCOUNT_CLERK', ?, ?, NULL)",
        (
            payload["effect_operation_id"],
            payload["authority_generation"],
            facts.effect_idempotency_key,
            payload["command_id"],
            bot_subject_id(payload["strategy_instance_id"]),
            payload["strategy_instance_id"],
            payload["run_id"],
            facts.effect_kind,
            payload["recorded_at_ms"],
            payload["recorded_at_ms"],
        ),
    )
    conn.execute(
        "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, "
        "role, broker_state, submitted_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, NULL, 'ENTRY', NULL, NULL, ?)",
        (
            payload["order_ref"],
            payload["effect_operation_id"],
            payload["order_ref"],
            payload["recorded_at_ms"],
        ),
    )
    conn.execute(
        "INSERT INTO operation_order_links (effect_operation_id, order_ref, role, linked_at_ms) "
        "VALUES (?, ?, 'ENTRY', ?)",
        (
            payload["effect_operation_id"],
            payload["order_ref"],
            payload["recorded_at_ms"],
        ),
    )
    conn.execute(
        "UPDATE commands SET effect_operation_id = ? WHERE command_id = ?",
        (payload["effect_operation_id"], payload["command_id"]),
    )


def _fold_exit_accepted(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Create the EXIT and link its captured sibling entries atomically.

    Entry ``orders.effect_operation_id`` values remain immutable ENTER
    provenance. Replayable ``operation_order_links`` carry later EXIT custody,
    so retries rebuild the same operation graph without re-parenting rows.
    """
    facts = ExitAcceptedFacts.from_facts_json(payload["facts_json"])
    _insert_command_row(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind=facts.kind,
        subject_id=bot_subject_id(payload["strategy_instance_id"]),
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="accepted",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, subject_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', 'ACCOUNT_CLERK', ?, ?, NULL)",
        (
            payload["effect_operation_id"],
            payload["authority_generation"],
            facts.effect_idempotency_key,
            payload["command_id"],
            bot_subject_id(payload["strategy_instance_id"]),
            payload["strategy_instance_id"],
            payload["run_id"],
            facts.effect_kind,
            payload["recorded_at_ms"],
            payload["recorded_at_ms"],
        ),
    )
    conn.executemany(
        "INSERT INTO operation_order_links (effect_operation_id, order_ref, role, linked_at_ms) "
        "VALUES (?, ?, 'ENTRY', ?)",
        (
            (payload["effect_operation_id"], order_ref, payload["recorded_at_ms"])
            for order_ref in facts.entry_order_refs
        ),
    )
    conn.execute(
        "UPDATE commands SET effect_operation_id = ? WHERE command_id = ?",
        (payload["effect_operation_id"], payload["command_id"]),
    )


def _fold_exit_reducing_order_created(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Create and link the database-unique reducing identity for one EXIT.

    The instruction remains in typed transition facts; the order row stores
    identity and broker projection while the partial unique link index makes
    a second reducing intent for the same EXIT impossible.
    """
    conn.execute(
        "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, "
        "role, broker_state, submitted_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, NULL, 'REDUCING', NULL, NULL, ?)",
        (
            payload["order_ref"],
            payload["effect_operation_id"],
            payload["order_ref"],
            payload["recorded_at_ms"],
        ),
    )
    conn.execute(
        "INSERT INTO operation_order_links (effect_operation_id, order_ref, role, linked_at_ms) "
        "VALUES (?, ?, 'REDUCING', ?)",
        (
            payload["effect_operation_id"],
            payload["order_ref"],
            payload["recorded_at_ms"],
        ),
    )


_TERMINAL_ORDER_STATES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
_ORDER_STATE_PRECEDENCE = {
    "new": 10,
    "accepted": 10,
    "pending_new": 10,
    "pending_cancel": 20,
    "partially_filled": 30,
    "filled": 100,
    "canceled": 100,
    "expired": 100,
    "rejected": 100,
    "replaced": 100,
}


def order_observation_advances(
    *,
    current_state: str | None,
    prior_source_time: int | None,
    incoming_state: str | None,
    incoming_source_time: int | None,
) -> bool:
    """Apply the canonical timestamp/state ordering to one broker snapshot."""
    current = (current_state or "").lower()
    incoming = (incoming_state or "").lower()
    if current in _TERMINAL_ORDER_STATES:
        return False
    if incoming_source_time is None:
        return incoming in _TERMINAL_ORDER_STATES or not current
    if prior_source_time is None or incoming_source_time > prior_source_time:
        return True
    if incoming_source_time < prior_source_time:
        return False
    return _ORDER_STATE_PRECEDENCE.get(incoming, 0) >= _ORDER_STATE_PRECEDENCE.get(current, 0)


def _ack_advances_order(conn: sqlite3.Connection, payload: dict[str, Any]) -> bool:
    """Whether this observation may advance the materialized order snapshot."""
    row = conn.execute("SELECT broker_state FROM orders WHERE order_ref = ?", (payload["order_ref"],)).fetchone()
    current_state = row["broker_state"]

    current_sequence = _this_transition_sequence(conn)
    prior_source_time = conn.execute(
        "SELECT MAX(source_event_at_ms) AS latest FROM custody_transitions "
        "WHERE order_ref = ? AND transition_kind = 'ORDER_SUBMIT_ACKED' AND sequence < ?",
        (payload["order_ref"], current_sequence),
    ).fetchone()["latest"]
    return order_observation_advances(
        current_state=current_state,
        prior_source_time=prior_source_time,
        incoming_state=payload["broker_state"],
        incoming_source_time=payload["source_event_at_ms"],
    )


def _fold_order_submit_acked(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Audit every snapshot while advancing only the monotone projection.

    Source time wins first, equal timestamps use explicit state precedence,
    and an undated terminal state may advance a nonterminal order. Terminal
    order/effect/command projections never regress on late or duplicate rows.
    """
    order_ref = payload["order_ref"]
    if _ack_advances_order(conn, payload):
        conn.execute(
            "UPDATE orders SET broker_order_id = COALESCE(broker_order_id, ?), "
            "broker_state = ?, submitted_at_ms = COALESCE(submitted_at_ms, ?), "
            "updated_at_ms = ? WHERE order_ref = ?",
            (
                payload["broker_order_id"],
                payload["broker_state"],
                payload["clerk_observed_at_ms"],
                payload["recorded_at_ms"],
                order_ref,
            ),
        )
    unknown_evidence = _resolve_unknown_outcome_if_proven(conn, payload)
    if not unknown_evidence.effect_still_unknown:
        conn.execute(
            "UPDATE effect_operations SET state = 'in_progress', updated_at_ms = ? "
            "WHERE effect_operation_id = ? AND state NOT IN ('succeeded','failed','rejected')",
            (payload["recorded_at_ms"], payload["effect_operation_id"]),
        )
        conn.execute(
            "UPDATE commands SET state = 'in_progress', updated_at_ms = ? WHERE command_id = ? "
            "AND state NOT IN ('succeeded','failed','rejected')",
            (payload["recorded_at_ms"], payload["command_id"]),
        )
        _sync_manual_ticket_effect_state(
            conn,
            payload,
            effect_state="in_progress",
            leg_state="IN_PROGRESS",
            ticket_state="ACTIVE",
        )


def _fold_effect_terminal(conn: sqlite3.Connection, payload: dict[str, Any], *, terminal_state: str) -> None:
    """Shared tail for an effect operation reaching a terminal outcome
    (#1377+): a receipt linking both the effect and its command, and both
    rows' state moved off whatever nonterminal state they were in. Distinct
    from ``_attach_command_receipt`` above, which is the operator-lifecycle
    shape (receipt inserted alongside a *fresh* terminal command row, no
    effect to link) — this one updates an already-existing effect/command
    pair created by an earlier transition (``ENTER_ACCEPTED``).
    """
    effect_operation_id = payload["effect_operation_id"]
    command_id = payload["command_id"]
    current = conn.execute(
        "SELECT state FROM effect_operations WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    if current is not None and current["state"] in ("succeeded", "failed", "rejected"):
        _resolve_unknown_outcome_if_proven(conn, payload)
        return
    receipt_id = f"receipt:{effect_operation_id}"
    _insert_receipt_row(
        conn,
        receipt_id=receipt_id,
        command_id=command_id,
        effect_operation_id=effect_operation_id,
        terminal_state=terminal_state,
        payload=payload,
    )
    conn.execute(
        "UPDATE effect_operations SET state = ?, terminal_receipt_id = ?, updated_at_ms = ? "
        "WHERE effect_operation_id = ?",
        (terminal_state, receipt_id, payload["recorded_at_ms"], effect_operation_id),
    )
    conn.execute(
        "UPDATE commands SET state = ?, receipt_id = ?, updated_at_ms = ? WHERE command_id = ?",
        (terminal_state, receipt_id, payload["recorded_at_ms"], command_id),
    )
    _resolve_unknown_outcome_if_proven(conn, payload)


def _sync_manual_ticket_effect_state(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    effect_state: str,
    leg_state: str,
    ticket_state: str,
) -> None:
    """Advance the manual ticket only when its owned effect reached this state.

    The ticket is a durable projection of the same effect state—not an HTTP
    presentation inference. Non-manual effects leave no matching leg and are
    intentionally unchanged.
    """
    effect = conn.execute(
        "SELECT kind, state FROM effect_operations WHERE effect_operation_id = ?",
        (payload["effect_operation_id"],),
    ).fetchone()
    if effect is None or effect["kind"] != "MANUAL_ORDER" or effect["state"] != effect_state:
        return
    updated = conn.execute(
        "UPDATE manual_order_legs SET state = ?, updated_at_ms = ? "
        "WHERE effect_operation_id = ?",
        (leg_state, payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    if updated.rowcount != 1:
        raise ValueError("manual order effect requires exactly one ticket leg")
    ticket_updated = conn.execute(
        "UPDATE manual_order_tickets SET state = ?, updated_at_ms = ? "
        "WHERE ticket_id = (SELECT ticket_id FROM manual_order_legs "
        "WHERE effect_operation_id = ?)",
        (ticket_state, payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    if ticket_updated.rowcount != 1:
        raise ValueError("manual order effect requires exactly one ticket")


def _fold_manual_order_filled(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Terminal success needs both a filled broker state and exact fill coverage.

    ``order_evidence`` appends this only after it proves that the effective
    execution total covers the immutable manual leg. The fold then makes the
    receipt and bounded ticket projection agree with that final custody fact.
    """
    _fold_effect_terminal(conn, payload, terminal_state="succeeded")
    _sync_manual_ticket_effect_state(
        conn,
        payload,
        effect_state="succeeded",
        leg_state="SUCCEEDED",
        ticket_state="COMPLETED",
    )


def _fold_order_submit_failed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Definitive submit failure (vendor 4xx/409/auth/rate-limit) or absence
    proven past the R4 uncertainty grace window — both fold to the same
    terminal ``failed`` outcome with a receipt."""
    _fold_effect_terminal(conn, payload, terminal_state="failed")
    _sync_manual_ticket_effect_state(
        conn,
        payload,
        effect_state="failed",
        leg_state="FAILED",
        ticket_state="COMPLETED",
    )


def _fold_exit_attributed_flat(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """EXIT's precise terminal-success proof (#1379, pinned contract §6's
    "terminal EXIT receipt"): the attributed exposure for this operation's
    symbol has been independently verified flat (see
    ``exit.resolve_exit``'s verification step) — reuses
    ``_fold_effect_terminal`` unchanged (ENTER never reaches ``succeeded``
    in its own module; this is that shared tail's first ``succeeded``
    caller)."""
    _fold_effect_terminal(conn, payload, terminal_state="succeeded")


def _fold_order_submit_uncertain(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """A lost/timed-out broker response — never a terminal outcome (R4):
    retains Account Clerk custody at ``unknown`` until resolution proves
    otherwise. No receipt: ``unknown`` is not a proof of any outcome."""
    conn.execute(
        "UPDATE effect_operations SET state = 'unknown', updated_at_ms = ? "
        "WHERE effect_operation_id = ? AND state NOT IN ('succeeded','failed','rejected')",
        (payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    conn.execute(
        "UPDATE commands SET state = 'unknown', updated_at_ms = ? WHERE command_id = ? "
        "AND state NOT IN ('succeeded','failed','rejected')",
        (payload["recorded_at_ms"], payload["command_id"]),
    )
    effect = conn.execute(
        "SELECT state FROM effect_operations WHERE effect_operation_id = ?",
        (payload["effect_operation_id"],),
    ).fetchone()
    if effect is not None and effect["state"] == "unknown":
        _sync_manual_ticket_effect_state(
            conn,
            payload,
            effect_state="unknown",
            leg_state="UNKNOWN",
            ticket_state="PAUSED_UNKNOWN",
        )
        _open_or_refresh_unknown_outcome(conn, payload)


#: Numerical-rigor tolerance for "is this position flat/drifted" — same
#: absolute-tolerance rationale as ``FILL_QTY_EPSILON`` above (see
#: ``docs/references/clerk-position-drift-tolerance.md``). Lives here, not in
#: ``reconcile.py`` (its original logical home), so both ``reconcile.py`` and
#: ``exit.py`` can import it without either depending on the other —
#: ``reconcile_uncertain_order`` needs to call ``exit.resolve_exit`` for an
#: EXIT-owned order (#1379), which would otherwise be a circular import.
POSITION_QTY_EPSILON = 1e-9


def position_quantity_is_nonzero(quantity: float) -> bool:
    """Return whether custody must treat ``quantity`` as exposure.

    Formula: ``nonzero(q) = abs(q) >= POSITION_QTY_EPSILON``.
    Reference: ``docs/references/clerk-position-drift-tolerance.md``.
    Canonical implementation: this file.
    Validated against:
      ``tests/broker/alpaca/clerk/sqlite/test_reconcile.py::test_position_quantity_boundary_is_unambiguous``.
    """
    return abs(quantity) >= POSITION_QTY_EPSILON


def _apply_attributed_position_delta(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    symbol: str,
    side: str,
    quantity: float,
) -> None:
    owner = conn.execute(
        "SELECT subject_id, strategy_instance_id FROM effect_operations WHERE effect_operation_id = ?",
        (payload["effect_operation_id"],),
    ).fetchone()
    if owner is None:
        raise ValueError("execution fill requires its durable owning effect")
    signed_delta = quantity if side == "BUY" else -quantity
    conn.execute(
        "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(subject_id, symbol) DO UPDATE SET "
        "attributed_qty = attributed_qty + excluded.attributed_qty, "
        "updated_at_ms = excluded.updated_at_ms",
        (
            owner["subject_id"],
            owner["strategy_instance_id"],
            symbol.upper(),
            signed_delta,
            payload["recorded_at_ms"],
        ),
    )


def _fold_execution_slice_filled(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Fold one idempotent broker execution slice into exposure.

    Formula: attributed_qty' = attributed_qty + sign(side) * slice_qty.
    Reference: docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md
      § Task S1.2 (execution-slice facts + folds).
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_folds_execution.py::test_execution_slice_filled_records_exact_slices.
    """
    facts = ExecutionSliceFilledFacts.from_facts_json(payload["facts_json"])
    validate_execution_slice_facts(facts)
    if payload["source_event_at_ms"] != facts.source_event_at_ms:
        raise ValueError("execution source_event_at_ms must match the typed facts")

    already_recorded = conn.execute(
        "SELECT 1 FROM fills WHERE execution_id = ?", (facts.execution_id,)
    ).fetchone()
    if already_recorded is not None:
        return
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'fill', NULL, ?, ?, ?, ?, ?, ?)",
        (
            facts.execution_id,
            payload["order_ref"],
            facts.slice_qty,
            facts.slice_price,
            facts.side,
            facts.execution_id,
            facts.evidence_source,
            facts.fee,
            facts.fee_fidelity,
            facts.source_event_at_ms,
            payload["clerk_observed_at_ms"],
            payload["recorded_at_ms"],
            _this_transition_sequence(conn),
        ),
    )
    _apply_attributed_position_delta(
        conn,
        payload=payload,
        symbol=facts.symbol,
        side=facts.side,
        quantity=facts.slice_qty,
    )


def _fold_execution_coverage_quarantined(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> None:
    """Keep ambiguous exact evidence in the hash chain, outside economics.

    The immutable transition facts are the quarantine fold. No economic
    current-state row is written here, but the embedded typed uncertainty is
    folded in the same SQLite transaction so a crash cannot leave the account
    with recorded ambiguity and no admission block.
    """
    facts = ExecutionCoverageQuarantinedFacts.from_facts_json(payload["facts_json"])
    exact = validate_execution_coverage_quarantined_facts(facts)
    if facts.order_ref != payload["order_ref"]:
        raise ValueError("quarantined coverage order_ref must match its transition")
    if payload["source_event_at_ms"] != exact.source_event_at_ms:
        raise ValueError("quarantined coverage timestamp must match exact evidence")
    if facts.uncertainty is not None:
        if active_execution_coverage_conflicts(conn, order_ref=facts.order_ref):
            raise ValueError("initial coverage quarantine cannot reopen an active conflict")
        uncertainty_payload = dict(payload)
        uncertainty_payload["transition_kind"] = "UNCERTAINTY_RAISED"
        uncertainty_payload["facts_json"] = facts.uncertainty.to_facts_json()
        _fold_uncertainty_raised(conn, uncertainty_payload)
        return
    active = active_execution_coverage_conflicts(
        conn,
        order_ref=facts.order_ref,
    )
    if not any(
        conflict.conflict_execution_id == facts.conflict_execution_id
        for conflict in active
    ):
        raise ValueError("additional coverage evidence requires its active conflict episode")


def _fold_execution_coverage_resolved(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Replace exactly one cumulative fold with verified exact economics.

    Formula: Δposition = 0 because the replacement is admitted only when
    exact and cumulative quantity/price/side agree within the Clerk's pinned
    execution tolerance.  The raw cumulative and exact observations remain
    custody transitions; ``fills`` is rebuilt from the selected coverage.
    Reference: docs/prds/2026-08-13-sqlite-clerk-manual-orders.md §11.
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_folds_execution.py::test_coverage_resolution_replaces_one_cumulative_fill_once.
    """
    facts = ExecutionCoverageResolvedFacts.from_facts_json(payload["facts_json"])
    exact = validate_execution_coverage_resolved_facts(facts)
    meta = reads.control_meta_snapshot(conn)
    if (
        facts.account_id != meta.account_id
        or facts.authority_generation != meta.authority_generation
        or facts.db_identity_token != meta.db_identity_token
        or facts.expected_control_revision != meta.control_revision
    ):
        raise ValueError("coverage resolution binding does not match the current Clerk authority")
    active = active_execution_coverage_conflicts(
        conn,
        uncertainty_id=facts.uncertainty_id,
    )
    if len(active) != 1 or active[0].order_ref != facts.order_ref:
        raise ValueError("coverage resolution requires its selected active conflict")
    proof = execution_coverage_proof(conn, conflict=active[0])
    cumulative = proof.cumulative
    if (
        not proof.proof_available
        or proof.exact_execution != exact
        or cumulative is None
        or cumulative.fill_id != facts.replaced_cumulative_fill_id
    ):
        raise ValueError("coverage resolution proof does not match immutable evidence")
    if conn.execute("SELECT 1 FROM fills WHERE execution_id = ?", (exact.execution_id,)).fetchone() is not None:
        raise ValueError("coverage resolution exact execution already exists")
    conn.execute("DELETE FROM fills WHERE fill_id = ?", (facts.replaced_cumulative_fill_id,))
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'fill', NULL, ?, ?, ?, ?, ?, ?)",
        (
            exact.execution_id,
            facts.order_ref,
            exact.slice_qty,
            exact.slice_price,
            exact.side,
            exact.execution_id,
            exact.evidence_source,
            exact.fee,
            exact.fee_fidelity,
            exact.source_event_at_ms,
            payload["clerk_observed_at_ms"],
            payload["recorded_at_ms"],
            _this_transition_sequence(conn),
        ),
    )
    conn.execute(
        "UPDATE uncertainties SET resolved_at_ms = ? WHERE uncertainty_id = ? "
        "AND reason_code = 'EXECUTION_COVERAGE_CONFLICT' AND resolved_at_ms IS NULL",
        (payload["recorded_at_ms"], facts.uncertainty_id),
    )
    manual_order = conn.execute(
        "SELECT effect.kind, effect.state, effect.command_id, effect.effect_operation_id, "
        "order_row.broker_state, json_extract(acceptance.facts_json, '$.leg.quantity') "
        "AS requested_quantity FROM orders order_row JOIN effect_operations effect "
        "ON effect.effect_operation_id = order_row.effect_operation_id "
        "JOIN custody_transitions acceptance ON acceptance.sequence = ("
        "SELECT MIN(accepted.sequence) FROM custody_transitions accepted "
        "WHERE accepted.effect_operation_id = effect.effect_operation_id "
        "AND accepted.order_ref = order_row.order_ref "
        "AND accepted.transition_kind = 'MANUAL_ORDER_ACCEPTED') "
        "WHERE order_row.order_ref = ?",
        (facts.order_ref,),
    ).fetchone()
    if (
        manual_order is None
        or manual_order["kind"] != "MANUAL_ORDER"
        or manual_order["state"] in {"succeeded", "failed", "rejected"}
        or (manual_order["broker_state"] or "").lower() != "filled"
    ):
        return
    exact_quantity, _ = reads.effective_exact_fill_totals_for_order(conn, facts.order_ref)
    if abs(exact_quantity - float(manual_order["requested_quantity"])) > FILL_QTY_EPSILON:
        return
    completion_payload = dict(payload)
    completion_payload.update(
        command_id=manual_order["command_id"],
        effect_operation_id=manual_order["effect_operation_id"],
        order_ref=facts.order_ref,
        transition_kind="MANUAL_ORDER_FILLED",
        operation_state="succeeded",
        summary_code="MANUAL_ORDER_FILLED",
    )
    _fold_manual_order_filled(conn, completion_payload)


def _fold_execution_corrected(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Replace one effective execution while retaining its audit history.

    Formula: attributed_qty' = attributed_qty + sign(side) *
      (corrected_qty - superseded_qty).
    Reference: docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md
      § Task S1.2 (execution-slice facts + folds).
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_folds_execution.py::test_execution_correction_replaces_effective_quantity.
    """
    facts = ExecutionCorrectedFacts.from_facts_json(payload["facts_json"])
    validate_execution_corrected_facts(facts)

    already_recorded = conn.execute(
        "SELECT 1 FROM fills WHERE execution_id = ?", (facts.execution_id,)
    ).fetchone()
    if already_recorded is not None:
        return
    owner = conn.execute(
        "SELECT subject_id, strategy_instance_id FROM effect_operations "
        "WHERE effect_operation_id = ?",
        (payload["effect_operation_id"],),
    ).fetchone()
    if owner is None:
        raise ValueError("correction requires its durable owning effect")
    superseded = conn.execute(
        "SELECT f.fill_id, f.order_ref, f.qty, f.price, f.side, f.evidence_source, f.fee, "
        "f.fee_fidelity, e.subject_id, e.strategy_instance_id, "
        "COALESCE(s.symbol, json_extract(manual_acceptance.facts_json, '$.leg.symbol')) AS symbol "
        "FROM fills f JOIN orders o ON o.order_ref = f.order_ref "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "LEFT JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id "
        "LEFT JOIN custody_transitions manual_acceptance ON manual_acceptance.sequence = ("
        "SELECT MIN(acceptance.sequence) FROM custody_transitions acceptance "
        "WHERE acceptance.order_ref = o.order_ref "
        "AND acceptance.effect_operation_id = e.effect_operation_id "
        "AND acceptance.transition_kind = 'MANUAL_ORDER_ACCEPTED') "
        "WHERE f.execution_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM fills successor "
        "WHERE successor.superseded_execution_ref = f.execution_id)",
        (facts.superseded_execution_ref,),
    ).fetchone()
    if superseded is None:
        raise ValueError(
            f"correction target {facts.superseded_execution_ref!r} is missing or no longer effective"
        )
    if superseded["order_ref"] != payload["order_ref"]:
        raise ValueError("correction target belongs to a different order")
    if superseded["subject_id"] != owner["subject_id"]:
        raise ValueError("correction target belongs to a different custody subject")
    if superseded["strategy_instance_id"] != owner["strategy_instance_id"]:
        raise ValueError("correction target belongs to a different strategy instance")
    if not isinstance(superseded["symbol"], str) or not superseded["symbol"]:
        raise ValueError("correction target is missing owned symbol evidence")
    if superseded["symbol"].upper() != facts.symbol.upper():
        raise ValueError("correction symbol does not match the superseded execution")
    if superseded["side"] != facts.side:
        raise ValueError("correction side does not match the superseded execution")

    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'correction', ?, ?, ?, ?, ?, ?, ?)",
        (
            facts.execution_id,
            payload["order_ref"],
            facts.corrected_qty,
            facts.corrected_price,
            facts.side,
            facts.execution_id,
            superseded["evidence_source"],
            facts.superseded_execution_ref,
            superseded["fee"],
            superseded["fee_fidelity"],
            payload["source_event_at_ms"],
            payload["clerk_observed_at_ms"],
            payload["recorded_at_ms"],
            _this_transition_sequence(conn),
        ),
    )
    _apply_attributed_position_delta(
        conn,
        payload=payload,
        symbol=facts.symbol,
        side=facts.side,
        quantity=facts.corrected_qty - superseded["qty"],
    )


def _fold_order_fill_observed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Namespace-attributed exposure fold: ``positions`` sums only this
    order's owned fills, keyed by ``strategy_instance_id`` — never derived by
    netting the raw broker account position (schema.py comment, preserving
    ``exposure.py`` semantics).

    Formula: delta_qty = cumulative_qty - prior_effective_qty; attributed_qty'
      = attributed_qty + sign(side) * delta_qty.
    Reference: docs/references/clerk-fill-quantity-tolerance.md.
    Canonical implementation: this file.
    Validated against: PythonDataService/tests/broker/alpaca/clerk/sqlite/
      test_folds_execution.py::test_cumulative_recovery_fill_is_explicitly_tagged.

    The evidence carries Alpaca's REST-reported *cumulative*
    ``filled_quantity``/``filled_avg_price`` for the order, not a
    per-execution id (no execution id is available outside the
    trade_updates websocket stream, #1378+ territory) — so this fold derives
    the delta qty, delta price, and a dedup identity itself:

    - ``delta_qty = cumulative_filled_quantity - SUM(prior effective fills'
      qty)``, so a sequence of partial fills (e.g. 2 then 5) sums to the
      right total (5), not their cumulative values (7). Superseded execution
      rows remain auditable but are deliberately excluded, so a later
      correction cannot make recovery suppress a genuine new delta. Compared
      against ``FILL_QTY_EPSILON``, not bare ``<= 0`` — quantities are floats
      (fractional shares), so a re-observation of the same cumulative state
      can differ from the effective recorded sum by float64 residue rather
      than exactly zero (see the tolerance doc).
    - ``delta_price`` is *not* ``facts.avg_price`` copied verbatim:
      Alpaca's ``filled_avg_price`` is the volume-weighted average over the
      *whole* order, so the new delta's own price is
      ``(cumulative_qty * avg_price - prior_qty * prior_avg_price) /
      delta_qty`` — derived from ``SUM(qty * price)`` over this order's
      effective fills, not the raw cumulative average.
    - ``fill_id`` is built from ``cumulative_filled_quantity`` rounded to
      the same fixed precision as the epsilon gate, not the float's raw
      ``str()`` repr — two observations of a mathematically-identical
      cumulative state must dedup even if their float representations
      differ by residue.

    A stale/regressed observation (``delta_qty`` at or below the tolerance)
    folds no fill row at all — out-of-order evidence never double-counts or
    reverses exposure.

    This recovery fold never fabricates a correction identity: it writes only
    a newly observed positive cumulative delta. Broker corrections instead
    use ``EXECUTION_CORRECTED``, which names the superseded execution slice.
    Effective totals exclude those superseded rows, allowing a later recovery
    snapshot to contribute only its genuinely unrecorded delta.
    """
    facts = OrderFillObservedFacts.from_facts_json(payload["facts_json"])
    order_ref = payload["order_ref"]
    fill_id = f"{order_ref}:{facts.cumulative_filled_quantity:.9f}"
    already_recorded = conn.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone()
    if already_recorded is not None:
        return
    prior_qty, prior_cost = reads.effective_fill_totals_for_order(conn, order_ref)
    delta_qty = facts.cumulative_filled_quantity - prior_qty
    if delta_qty < FILL_QTY_EPSILON:
        return
    delta_cost = (facts.avg_price * facts.cumulative_filled_quantity) - prior_cost
    delta_price = delta_cost / delta_qty
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, 'cumulative_recovery', 'fill', NULL, NULL, "
        "'not_reported', ?, ?, ?, ?)",
        (
            fill_id,
            order_ref,
            delta_qty,
            delta_price,
            facts.side,
            1 if facts.is_correction else 0,
            payload["source_event_at_ms"],
            payload["clerk_observed_at_ms"],
            payload["recorded_at_ms"],
            _this_transition_sequence(conn),
        ),
    )
    _apply_attributed_position_delta(
        conn,
        payload=payload,
        symbol=facts.symbol,
        side=facts.side,
        quantity=delta_qty,
    )


def _this_transition_sequence(conn: sqlite3.Connection) -> int:
    """The ``custody_transitions.sequence`` of the row this fold is running
    for. Safe to read mid-fold: ``_commit_transition_row`` inserts the
    transition row *before* invoking the fold (§4), and the whole commit runs
    under the repository's write lock plus a live ``BEGIN IMMEDIATE``, so no
    other writer can have advanced ``sequence`` in between. Used to mint a
    globally-unique, replay-deterministic id for a fold's own auxiliary rows
    without a random source (a fold must stay pure for mirror rebuild)."""
    return conn.execute("SELECT MAX(sequence) AS seq FROM custody_transitions").fetchone()["seq"]


def _fold_reconciliation_attempted(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """#1378: durable audit of one reconciliation pass's attempt to resolve a
    single uncertain order — distinct from (and appended alongside, never
    instead of) the ``ORDER_SUBMIT_ACKED``/``ORDER_SUBMIT_FAILED`` transition
    the resolution itself already produced when it found new evidence. This
    fold only ever inserts into ``reconciliations``; it never touches
    ``orders``/``effect_operations``/``positions`` — those projections are
    already correct by the time this transition is appended (see
    ``reconcile.reconcile_uncertain_order``)."""
    facts = ReconciliationAttemptedFacts.from_facts_json(payload["facts_json"])
    reconciliation_id = f"reconciliation:{_this_transition_sequence(conn)}"
    conn.execute(
        "INSERT INTO reconciliations (reconciliation_id, effect_operation_id, order_ref, "
        "trigger, attempted_at_ms, outcome, evidence_refs_json) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (
            reconciliation_id,
            payload["effect_operation_id"],
            payload["order_ref"],
            facts.trigger,
            payload["recorded_at_ms"],
            facts.outcome,
        ),
    )


def _fold_account_hold_raised(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Open one database-unique account hold episode for a typed cause."""
    facts = AccountHoldRaisedFacts.from_facts_json(payload["facts_json"])
    hold_id = f"hold:{_this_transition_sequence(conn)}"
    conn.execute(
        "INSERT INTO holds (hold_id, scope, strategy_instance_id, reason_code, state, "
        "opened_at_ms, resolved_at_ms, evidence_refs_json) "
        "VALUES (?, 'ACCOUNT_CLERK', NULL, ?, 'ACTIVE', ?, NULL, ?)",
        (
            hold_id,
            facts.reason_code,
            payload["recorded_at_ms"],
            canonicalize(facts.evidence_refs),
        ),
    )


def _fold_account_hold_refreshed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = AccountHoldRaisedFacts.from_facts_json(payload["facts_json"])
    conn.execute(
        "UPDATE holds SET evidence_refs_json = ? WHERE scope = 'ACCOUNT_CLERK' "
        "AND reason_code = ? AND state = 'ACTIVE'",
        (canonicalize(facts.evidence_refs), facts.reason_code),
    )


def _fold_account_hold_resolved(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    facts = AccountHoldResolvedFacts.from_facts_json(payload["facts_json"])
    conn.execute(
        "UPDATE holds SET state = 'RESOLVED', resolved_at_ms = ?, evidence_refs_json = ? "
        "WHERE scope = 'ACCOUNT_CLERK' AND reason_code = ? AND state = 'ACTIVE'",
        (payload["recorded_at_ms"], canonicalize(facts.evidence_refs), facts.reason_code),
    )


DEFAULT_FOLD_REGISTRY = FoldRegistry()
DEFAULT_FOLD_REGISTRY.register("STRATEGY_INSTANCE_REGISTERED", _fold_strategy_instance_registered)
DEFAULT_FOLD_REGISTRY.register("RUN_STARTED", _fold_run_started)
DEFAULT_FOLD_REGISTRY.register("RUN_STOPPED", _fold_run_stopped)
DEFAULT_FOLD_REGISTRY.register("COMMAND_REJECTED", _fold_command_rejected)
DEFAULT_FOLD_REGISTRY.register("ENTER_ACCEPTED", _fold_enter_accepted)
DEFAULT_FOLD_REGISTRY.register("EXIT_ACCEPTED", _fold_exit_accepted)
DEFAULT_FOLD_REGISTRY.register("EXIT_REDUCING_ORDER_CREATED", _fold_exit_reducing_order_created)
DEFAULT_FOLD_REGISTRY.register("EXIT_ATTRIBUTED_FLAT", _fold_exit_attributed_flat)
DEFAULT_FOLD_REGISTRY.register("EXIT_NOT_FLAT", _fold_order_submit_failed)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_ACKED", _fold_order_submit_acked)
DEFAULT_FOLD_REGISTRY.register("MANUAL_ORDER_FILLED", _fold_manual_order_filled)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_FAILED", _fold_order_submit_failed)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_UNCERTAIN", _fold_order_submit_uncertain)
# EXIT's cancel-analog (#1379): same generic "effect/command -> unknown, no
# receipt" fold body — no separate function, see _fold_order_submit_uncertain
# and order_evidence.fold_uncertain's docstrings.
DEFAULT_FOLD_REGISTRY.register("ORDER_CANCEL_UNCERTAIN", _fold_order_submit_uncertain)
DEFAULT_FOLD_REGISTRY.register("ORDER_FILL_OBSERVED", _fold_order_fill_observed)
DEFAULT_FOLD_REGISTRY.register("EXECUTION_SLICE_FILLED", _fold_execution_slice_filled)
DEFAULT_FOLD_REGISTRY.register("EXECUTION_COVERAGE_QUARANTINED", _fold_execution_coverage_quarantined)
DEFAULT_FOLD_REGISTRY.register("EXECUTION_COVERAGE_RESOLVED", _fold_execution_coverage_resolved)
DEFAULT_FOLD_REGISTRY.register("CUSTODY_SUBJECT_REGISTERED", fold_custody_subject_registered)
DEFAULT_FOLD_REGISTRY.register("MANUAL_TICKET_RESERVED", fold_manual_ticket_reserved)
DEFAULT_FOLD_REGISTRY.register("MANUAL_ORDER_ACCEPTED", fold_manual_order_accepted)
DEFAULT_FOLD_REGISTRY.register("MANUAL_TICKET_PAUSED_UNKNOWN", fold_manual_ticket_state)
DEFAULT_FOLD_REGISTRY.register("MANUAL_TICKET_COMPLETED", fold_manual_ticket_state)
DEFAULT_FOLD_REGISTRY.register("MANUAL_TICKET_CANCELED", fold_manual_ticket_state)
DEFAULT_FOLD_REGISTRY.register("EXECUTION_CORRECTED", _fold_execution_corrected)
DEFAULT_FOLD_REGISTRY.register("RECONCILIATION_ATTEMPTED", _fold_reconciliation_attempted)
DEFAULT_FOLD_REGISTRY.register("ACCOUNT_HOLD_RAISED", _fold_account_hold_raised)
DEFAULT_FOLD_REGISTRY.register("ACCOUNT_HOLD_REFRESHED", _fold_account_hold_refreshed)
DEFAULT_FOLD_REGISTRY.register("ACCOUNT_HOLD_RESOLVED", _fold_account_hold_resolved)
DEFAULT_FOLD_REGISTRY.register("EXTERNAL_ORDER_OBSERVED", _fold_external_order_observed)
DEFAULT_FOLD_REGISTRY.register("EXTERNAL_ORDER_ACKNOWLEDGED", _fold_external_order_acknowledged)
DEFAULT_FOLD_REGISTRY.register("UNCERTAINTY_RAISED", _fold_uncertainty_raised)
DEFAULT_FOLD_REGISTRY.register("UNCERTAINTY_REFRESHED", _fold_uncertainty_refreshed)
DEFAULT_FOLD_REGISTRY.register("UNCERTAINTY_RESOLVED", _fold_uncertainty_resolved)
# Audit-only EXIT phase markers. Their facts remain in the canonical transition
# stream; materialized order/effect state is advanced by the evidence folds.
DEFAULT_FOLD_REGISTRY.register("ORDER_CANCEL_REQUESTED", lambda _conn, _payload: None)
DEFAULT_FOLD_REGISTRY.register("ENTRY_TERMINAL_CONFIRMED", lambda _conn, _payload: None)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_REQUESTED", lambda _conn, _payload: None)
