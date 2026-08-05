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


_RECEIPT_TERMINAL_STATES = frozenset({"succeeded", "failed"})


def _set_command_terminal(conn: sqlite3.Connection, payload: dict[str, Any], *, state: str) -> None:
    """Shared tail for every command-terminal fold (#1376): link a receipt
    (succeeded/failed only — R3's receipts.terminal_state vocabulary has no
    'rejected'; an admission-time rejection isn't a proof of outcome) and
    set the command's final state.
    """
    command_id = payload["command_id"]
    receipt_id = None
    if state in _RECEIPT_TERMINAL_STATES:
        receipt_id = f"receipt:{command_id}"
        conn.execute(
            "INSERT INTO receipts (receipt_id, command_id, effect_operation_id, terminal_state, "
            "summary_code, proof_reference, recorded_at_ms, facts_json) "
            "VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)",
            (
                receipt_id,
                command_id,
                state,
                payload["summary_code"],
                payload["recorded_at_ms"],
                payload["facts_json"],
            ),
        )
    conn.execute(
        "UPDATE commands SET state = ?, receipt_id = ?, updated_at_ms = ? WHERE command_id = ?",
        (state, receipt_id, payload["recorded_at_ms"], command_id),
    )


def _fold_run_started(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    import json

    facts = json.loads(payload["facts_json"])
    conn.execute(
        "INSERT INTO runs (run_id, strategy_instance_id, lifecycle_run_id, state, "
        "started_at_ms, stopped_at_ms) VALUES (?, ?, ?, 'ACTIVE', ?, NULL)",
        (
            payload["run_id"],
            payload["strategy_instance_id"],
            facts["lifecycle_run_id"],
            payload["recorded_at_ms"],
        ),
    )
    _set_command_terminal(conn, payload, state="succeeded")


def _fold_run_stopped(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE runs SET state = 'STOPPED', stopped_at_ms = ? WHERE run_id = ?",
        (payload["recorded_at_ms"], payload["run_id"]),
    )
    _set_command_terminal(conn, payload, state="succeeded")


def _fold_command_rejected(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    _set_command_terminal(conn, payload, state="rejected")


def _fold_enter_accepted(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Transaction-matrix row "Effect operation acceptance + broker-eligible
    capture (R1)" (#1377): insert the effect operation and its order,
    every fact needed to call the broker already durable and mirror-finalized
    before this transaction's caller may attempt that call.
    """
    import json

    facts = json.loads(payload["facts_json"])
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES (?, ?, ?, ?, ?, NULL, 'ENTER', 'in_progress', 'ACCOUNT_CLERK', ?, ?, NULL)",
        (
            payload["effect_operation_id"],
            payload["authority_generation"],
            facts["effect_idempotency_key"],
            payload["command_id"],
            payload["strategy_instance_id"],
            payload["recorded_at_ms"],
            payload["recorded_at_ms"],
        ),
    )
    conn.execute(
        "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, "
        "role, broker_state, submitted_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, NULL, 'ENTRY', NULL, NULL, ?)",
        (payload["order_ref"], payload["effect_operation_id"], payload["order_ref"], payload["recorded_at_ms"]),
    )
    conn.execute(
        "UPDATE commands SET state = 'in_progress', effect_operation_id = ?, updated_at_ms = ? "
        "WHERE command_id = ?",
        (payload["effect_operation_id"], payload["recorded_at_ms"], payload["command_id"]),
    )


def _fold_order_submit_acked(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """§3c: idempotent by construction. The current row is already committed
    to ``custody_transitions`` before this fold runs, so ``MAX(source_event_at_ms)``
    over every ``ORDER_SUBMIT_ACKED`` row for this order (including this one)
    tells whether this observation is the newest seen — an older, out-of-order
    delivery is still logged for audit but does not regress ``orders.broker_state``.
    """
    order_ref = payload["order_ref"]
    latest = conn.execute(
        "SELECT MAX(source_event_at_ms) AS latest FROM custody_transitions "
        "WHERE order_ref = ? AND transition_kind = 'ORDER_SUBMIT_ACKED'",
        (order_ref,),
    ).fetchone()["latest"]
    if latest is None or payload["source_event_at_ms"] >= latest:
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


def _fold_order_submit_failed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Definitive submit failure (vendor 4xx/409/auth/rate-limit) or absence
    proven past the uncertainty grace window — both fold to the same
    terminal ``failed`` outcome with a receipt."""
    receipt_id = f"receipt:{payload['effect_operation_id']}"
    conn.execute(
        "INSERT INTO receipts (receipt_id, command_id, effect_operation_id, terminal_state, "
        "summary_code, proof_reference, recorded_at_ms, facts_json) "
        "VALUES (?, ?, ?, 'failed', ?, NULL, ?, ?)",
        (
            receipt_id,
            payload["command_id"],
            payload["effect_operation_id"],
            payload["summary_code"],
            payload["recorded_at_ms"],
            payload["facts_json"],
        ),
    )
    conn.execute(
        "UPDATE effect_operations SET state = 'failed', terminal_receipt_id = ?, updated_at_ms = ? "
        "WHERE effect_operation_id = ?",
        (receipt_id, payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    conn.execute(
        "UPDATE commands SET state = 'failed', receipt_id = ?, updated_at_ms = ? WHERE command_id = ?",
        (receipt_id, payload["recorded_at_ms"], payload["command_id"]),
    )


def _fold_order_submit_uncertain(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """A lost/timed-out response — never a terminal outcome (R4): retains
    Account Clerk custody at ``unknown`` until resolution proves otherwise."""
    conn.execute(
        "UPDATE effect_operations SET state = 'unknown', updated_at_ms = ? "
        "WHERE effect_operation_id = ?",
        (payload["recorded_at_ms"], payload["effect_operation_id"]),
    )
    conn.execute(
        "UPDATE commands SET state = 'unknown', updated_at_ms = ? WHERE command_id = ?",
        (payload["recorded_at_ms"], payload["command_id"]),
    )


def _fold_order_fill_observed(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Namespace-attributed exposure fold: ``positions`` sums only this
    order's owned fills, keyed by ``strategy_instance_id`` — never derived by
    netting the raw broker account position (schema.py comment, preserving
    ``exposure.py`` semantics).

    The evidence carries Alpaca's REST-reported *cumulative*
    ``filled_quantity`` for the order, not a per-execution id (no execution
    id is available outside the trade_updates websocket stream, #1378+
    territory) — so this fold derives both the delta and its identity itself:
    ``fill_id = "{order_ref}:{cumulative_filled_quantity}"`` is stable for a
    repeated observation of the same cumulative state (idempotent dedup) and
    distinct once that state legitimately advances. ``delta = cumulative -
    SUM(prior recorded fills' qty)`` is what actually gets credited, so a
    sequence of partial fills (e.g. 2 then 5) sums to the right total (5),
    not their cumulative values (7). A stale/regressed observation (delta <=
    0) folds no fill row at all — out-of-order evidence never double-counts
    or reverses exposure.
    """
    import json

    facts = json.loads(payload["facts_json"])
    order_ref = payload["order_ref"]
    fill_id = f"{order_ref}:{facts['cumulative_filled_quantity']}"
    already_recorded = conn.execute(
        "SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)
    ).fetchone()
    if already_recorded is not None:
        return
    prior_total = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS total FROM fills WHERE order_ref = ?", (order_ref,)
    ).fetchone()["total"]
    delta_qty = facts["cumulative_filled_quantity"] - prior_total
    if delta_qty <= 0:
        return
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fill_id,
            order_ref,
            delta_qty,
            facts["avg_price"],
            facts["side"],
            1 if facts["is_correction"] else 0,
            payload["source_event_at_ms"],
            payload["clerk_observed_at_ms"],
            payload["recorded_at_ms"],
        ),
    )
    signed_delta = delta_qty if facts["side"] == "BUY" else -delta_qty
    conn.execute(
        "INSERT INTO positions (strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(strategy_instance_id, symbol) DO UPDATE SET "
        "attributed_qty = attributed_qty + excluded.attributed_qty, "
        "updated_at_ms = excluded.updated_at_ms",
        (payload["strategy_instance_id"], facts["symbol"], signed_delta, payload["recorded_at_ms"]),
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
