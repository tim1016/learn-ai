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
    EnterAcceptedFacts,
    OrderFillObservedFacts,
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
        strategy_instance_id=payload["strategy_instance_id"],
        run_id=payload["run_id"],
        action=facts.action,
        intended_end_state=facts.intended_end_state,
        state="accepted",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', 'ACCOUNT_CLERK', ?, ?, NULL)",
        (
            payload["effect_operation_id"],
            payload["authority_generation"],
            facts.effect_idempotency_key,
            payload["command_id"],
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
        "UPDATE commands SET effect_operation_id = ? WHERE command_id = ?",
        (payload["effect_operation_id"], payload["command_id"]),
    )


def _fold_order_submit_acked(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """§3c: idempotent by construction. The current row is already committed
    to ``custody_transitions`` before this fold runs, so ``MAX(source_event_at_ms)``
    over every ``ORDER_SUBMIT_ACKED`` row for this order (including this one)
    tells whether this observation is the newest seen — an older, out-of-order
    delivery is still logged for audit but does not regress ``orders.broker_state``.

    ``source_event_at_ms`` is nullable (``BrokerOrder.updated_at_ms`` is
    optional — Alpaca can omit it): a ``None`` observation can never prove
    itself the newest, so it is always treated as not-newer once a real
    timestamp has already been recorded, rather than raising on the ``None
    >= int`` comparison.
    """
    order_ref = payload["order_ref"]
    latest = conn.execute(
        "SELECT MAX(source_event_at_ms) AS latest FROM custody_transitions "
        "WHERE order_ref = ? AND transition_kind = 'ORDER_SUBMIT_ACKED'",
        (order_ref,),
    ).fetchone()["latest"]
    source_event_at_ms = payload["source_event_at_ms"]
    if latest is None or (source_event_at_ms is not None and source_event_at_ms >= latest):
        conn.execute(
            "UPDATE orders SET broker_order_id = ?, broker_state = ?, submitted_at_ms = ?, "
            "updated_at_ms = ? WHERE order_ref = ?",
            (
                payload["broker_order_id"],
                payload["broker_state"],
                payload["clerk_observed_at_ms"],
                payload["recorded_at_ms"],
                order_ref,
            ),
        )
    conn.execute(
        "UPDATE effect_operations SET state = 'in_progress', updated_at_ms = ? "
        "WHERE effect_operation_id = ?",
        (payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    conn.execute(
        "UPDATE commands SET state = 'in_progress', updated_at_ms = ? WHERE command_id = ?",
        (payload["recorded_at_ms"], payload["command_id"]),
    )


def _fold_effect_terminal(
    conn: sqlite3.Connection, payload: dict[str, Any], *, terminal_state: str
) -> None:
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


def _fold_order_submit_failed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Definitive submit failure (vendor 4xx/409/auth/rate-limit) or absence
    proven past the R4 uncertainty grace window — both fold to the same
    terminal ``failed`` outcome with a receipt."""
    _fold_effect_terminal(conn, payload, terminal_state="failed")


def _fold_order_submit_uncertain(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """A lost/timed-out broker response — never a terminal outcome (R4):
    retains Account Clerk custody at ``unknown`` until resolution proves
    otherwise. No receipt: ``unknown`` is not a proof of any outcome."""
    conn.execute(
        "UPDATE effect_operations SET state = 'unknown', updated_at_ms = ? "
        "WHERE effect_operation_id = ?",
        (payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    conn.execute(
        "UPDATE commands SET state = 'unknown', updated_at_ms = ? WHERE command_id = ?",
        (payload["recorded_at_ms"], payload["command_id"]),
    )


#: Numerical-rigor tolerance for the fill-quantity delta gate below — see
#: ``docs/references/clerk-fill-quantity-tolerance.md`` for the citation and
#: reasoning (`.claude/rules/numerical-rigor.md`).
FILL_QTY_EPSILON = 1e-9


def _fold_order_fill_observed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Namespace-attributed exposure fold: ``positions`` sums only this
    order's owned fills, keyed by ``strategy_instance_id`` — never derived by
    netting the raw broker account position (schema.py comment, preserving
    ``exposure.py`` semantics).

    The evidence carries Alpaca's REST-reported *cumulative*
    ``filled_quantity``/``filled_avg_price`` for the order, not a
    per-execution id (no execution id is available outside the
    trade_updates websocket stream, #1378+ territory) — so this fold derives
    the delta qty, delta price, and a dedup identity itself:

    - ``delta_qty = cumulative_filled_quantity - SUM(prior recorded fills'
      qty)``, so a sequence of partial fills (e.g. 2 then 5) sums to the
      right total (5), not their cumulative values (7). Compared against
      ``FILL_QTY_EPSILON``, not bare ``<= 0`` — quantities are floats
      (fractional shares), so a re-observation of the same cumulative state
      can differ from the recorded sum by float64 residue rather than
      exactly zero (see the tolerance doc).
    - ``delta_price`` is *not* ``facts.avg_price`` copied verbatim:
      Alpaca's ``filled_avg_price`` is the volume-weighted average over the
      *whole* order, so the new delta's own price is
      ``(cumulative_qty * avg_price - prior_qty * prior_avg_price) /
      delta_qty`` — derived from ``SUM(qty * price)`` over this order's
      already-recorded fills, not the raw cumulative average.
    - ``fill_id`` is built from ``cumulative_filled_quantity`` rounded to
      the same fixed precision as the epsilon gate, not the float's raw
      ``str()`` repr — two observations of a mathematically-identical
      cumulative state must dedup even if their float representations
      differ by residue.

    A stale/regressed observation (``delta_qty`` at or below the tolerance)
    folds no fill row at all — out-of-order evidence never double-counts or
    reverses exposure.

    ``is_correction`` is always recorded ``False`` this slice:
    :func:`~app.broker.alpaca.clerk.sqlite.enter.fold_order_evidence` never
    passes ``True``, and a downward correction (a negative ``delta_qty``)
    returns above before any row is written at all. There is no correction
    path yet — a broker-issued downward correction permanently overstates
    attributed exposure until one is built (#1378+ territory).
    """
    facts = OrderFillObservedFacts.from_facts_json(payload["facts_json"])
    order_ref = payload["order_ref"]
    fill_id = f"{order_ref}:{facts.cumulative_filled_quantity:.9f}"
    already_recorded = conn.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone()
    if already_recorded is not None:
        return
    prior = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS qty, COALESCE(SUM(qty * price), 0) AS cost "
        "FROM fills WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    delta_qty = facts.cumulative_filled_quantity - prior["qty"]
    if delta_qty < FILL_QTY_EPSILON:
        return
    delta_cost = (facts.avg_price * facts.cumulative_filled_quantity) - prior["cost"]
    delta_price = delta_cost / delta_qty
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )
    signed_delta = delta_qty if facts.side == "BUY" else -delta_qty
    conn.execute(
        "INSERT INTO positions (strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(strategy_instance_id, symbol) DO UPDATE SET "
        "attributed_qty = attributed_qty + excluded.attributed_qty, "
        "updated_at_ms = excluded.updated_at_ms",
        (payload["strategy_instance_id"], facts.symbol, signed_delta, payload["recorded_at_ms"]),
    )


DEFAULT_FOLD_REGISTRY = FoldRegistry()
DEFAULT_FOLD_REGISTRY.register(
    "STRATEGY_INSTANCE_REGISTERED", _fold_strategy_instance_registered
)
DEFAULT_FOLD_REGISTRY.register("RUN_STARTED", _fold_run_started)
DEFAULT_FOLD_REGISTRY.register("RUN_STOPPED", _fold_run_stopped)
DEFAULT_FOLD_REGISTRY.register("COMMAND_REJECTED", _fold_command_rejected)
DEFAULT_FOLD_REGISTRY.register("ENTER_ACCEPTED", _fold_enter_accepted)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_ACKED", _fold_order_submit_acked)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_FAILED", _fold_order_submit_failed)
DEFAULT_FOLD_REGISTRY.register("ORDER_SUBMIT_UNCERTAIN", _fold_order_submit_uncertain)
DEFAULT_FOLD_REGISTRY.register("ORDER_FILL_OBSERVED", _fold_order_fill_observed)
