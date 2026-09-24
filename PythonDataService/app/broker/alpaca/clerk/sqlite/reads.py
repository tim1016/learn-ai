"""Typed read snapshots over the SQLite Clerk spine.

Pure ``SELECT`` + row-to-dataclass mapping, no write path, no lock, no fold
concerns — split out of ``repository.py`` to keep that module under the
file-size ceiling as new read surfaces accumulate slice over slice.
``ClerkSqliteRepository`` methods delegate here; callers outside this package
still never see a cursor (PRD §9.2) — they go through the repository, which
happens to forward to this module for these queries.
"""

from __future__ import annotations

import json
import sqlite3
from typing import NamedTuple

from app.broker.alpaca.clerk.sqlite.execution_coverage import FILL_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.models import (
    BotConfigResource,
    CommandResource,
    ControlMetaSnapshot,
    DecisionReceiptResource,
    EffectOperationResource,
    ExternalOrderResource,
    ManualOrderCancellationResource,
    ManualOrderLegResource,
    ManualOrderTicketResource,
    OrderResource,
)

_COMMAND_COLUMNS: tuple[str, ...] = (
    "command_id",
    "idempotency_key",
    "payload_hash",
    "kind",
    "strategy_instance_id",
    "run_id",
    "action",
    "intended_end_state",
    "state",
    "effect_operation_id",
    "receipt_id",
    "created_at_ms",
    "updated_at_ms",
)

_DECISION_RECEIPT_COLUMNS: tuple[str, ...] = (
    "strategy_instance_id",
    "seq",
    "outcome",
    "symbol",
    "intent_id",
    "order_ref",
    "observed_at_ms",
    "facts_json",
)

_EXTERNAL_ORDER_COLUMNS: tuple[str, ...] = (
    "external_order_id",
    "broker_order_id",
    "client_order_id",
    "symbol",
    "side",
    "qty",
    "order_type",
    "limit_price",
    "stop_price",
    "filled_avg_price",
    "observed_at_ms",
    "acknowledged_at_ms",
    "ack_operator",
    "evidence_refs_json",
)

_EXTERNAL_ORDER_SELECT = (
    ", ".join(f"eo.{column}" for column in _EXTERNAL_ORDER_COLUMNS)
    + ", (SELECT MIN(ct.sequence) FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED') AS observation_sequence"
    + ", (SELECT MAX(ct.sequence) FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_ACKNOWLEDGED') AS acknowledgement_sequence"
    + ", (SELECT ct.recorded_at_ms FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED' "
    "ORDER BY ct.sequence ASC LIMIT 1) AS observation_recorded_at_ms"
    + ", (SELECT ct.recorded_at_ms FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_ACKNOWLEDGED' "
    "ORDER BY ct.sequence DESC LIMIT 1) AS acknowledgement_recorded_at_ms"
)


def _row_to_command_resource(row: sqlite3.Row) -> CommandResource:
    return CommandResource(**{column: row[column] for column in _COMMAND_COLUMNS})


def control_meta_snapshot(conn: sqlite3.Connection) -> ControlMetaSnapshot:
    row = conn.execute(
        "SELECT schema_version, account_id, db_identity_token, authority_generation, "
        "control_revision, created_at_ms, last_open_at_ms FROM control_meta WHERE id = 1"
    ).fetchone()
    return ControlMetaSnapshot(**dict(row))


def strategy_instances(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms "
        "FROM strategy_instances ORDER BY created_at_ms ASC"
    ).fetchall()
    return [dict(row) for row in rows]


#: Alpaca order states in which the broker may still act on an order. One
#: definition, imported by the recovery policy rather than restated there, so
#: "this bot still has a working order" cannot mean two different things
#: depending on which side asks.
WORKING_BROKER_STATES: frozenset[str] = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
)

#: Alpaca order states in which an owned ENTRY can still fill, so an ENTRY in
#: one of them outliving its run must be cancelled. The operator's
#: ``cancel_verified_working_orders`` and the reconciliation sweep's
#: stopped-run step (:func:`entry_orders_owed_a_cancel`) read this one
#: definition, so the two cancellers cannot disagree about which orders are
#: still live. Deliberately wider than :data:`WORKING_BROKER_STATES`: an
#: auction-bound, suspended or replace-pending order can still fill.
CANCELLABLE_ENTRY_BROKER_STATES: frozenset[str] = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "accepted_for_bidding",
        "pending_cancel",
        "pending_replace",
        "stopped",
        "suspended",
        "calculated",
    }
)

#: Effect-operation states that have not reached a terminal outcome. An effect
#: here can still create broker custody, so a registration carrying one is not
#: inert however flat it currently reads.
NONTERMINAL_EFFECT_STATES: frozenset[str] = frozenset(
    {"reserved", "accepted", "in_progress", "unknown"}
)


def strategy_instances_with_live_custody(conn: sqlite3.Connection) -> set[str]:
    """Roster ids whose custody can still change, or still needs attention.

    The union of the bot-scoped facts a catalog row's attention flag and its
    authored recovery command are derived from: an unresolved uncertainty
    (which since v12 is also every active hold and every execution-coverage
    conflict, both being ``uncertainties`` rows), a non-zero attributed
    position, an active run, an order in a working broker state, and a
    nonterminal effect operation. Account-scoped uncertainties are excluded on
    purpose -- they reach every row alike through the account facts, so they
    say nothing about one bot.

    The last two arms matter for a retired registration specifically: a
    working order observed after retirement, or an effect accepted in the
    window before its order becomes working, would otherwise leave that bot's
    custody projection and its authored recovery command out of the catalog
    entirely. The order states are the same set ``recovery_policy`` treats as
    working, and the effect states are its nonterminal ones.

    Answering this for the whole roster in one query is what lets the catalog
    skip the per-row projection for a retired registration that is provably
    inert (#1911), without ever going quiet on the retired-but-stranded bot
    #1778 exists for.

    Deliberately a superset: ``attributed_qty <> 0`` admits a quantity the
    float-aware ``position_quantity_is_nonzero`` would call flat, so a
    borderline row takes the fully-projected path. Costing a read is the safe
    direction to be wrong in; going quiet on a bot that needs attention is not.
    """
    working_states = ", ".join("?" for _ in WORKING_BROKER_STATES)
    nonterminal_states = ", ".join("?" for _ in NONTERMINAL_EFFECT_STATES)
    rows = conn.execute(
        "SELECT strategy_instance_id FROM uncertainties "
        "WHERE resolved_at_ms IS NULL AND strategy_instance_id IS NOT NULL "
        "UNION SELECT strategy_instance_id FROM positions "
        "WHERE attributed_qty <> 0 AND strategy_instance_id IS NOT NULL "
        "UNION SELECT strategy_instance_id FROM runs WHERE state = 'ACTIVE' "
        "UNION SELECT e.strategy_instance_id FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"WHERE LOWER(o.broker_state) IN ({working_states}) "
        "AND e.strategy_instance_id IS NOT NULL "
        "UNION SELECT strategy_instance_id FROM effect_operations "
        f"WHERE state IN ({nonterminal_states}) AND strategy_instance_id IS NOT NULL",
        (*sorted(WORKING_BROKER_STATES), *sorted(NONTERMINAL_EFFECT_STATES)),
    ).fetchall()
    return {str(row["strategy_instance_id"]) for row in rows}


def market_data_symbols(conn: sqlite3.Connection) -> tuple[str, ...]:
    """One projection of deployments and custody using the canonical live sets.

    Strategy identity supplies bot symbols without decoding historical order
    legs or fills. Only manual orders need their immutable acceptance symbol.
    Retired strategies remain watched while they still have custody.
    """
    working = ",".join("?" for _ in WORKING_BROKER_STATES)
    pending = ",".join("?" for _ in NONTERMINAL_EFFECT_STATES)
    rows = conn.execute(
        "WITH live_effects AS ("
        "SELECT effect_operation_id, strategy_instance_id FROM effect_operations "
        f"WHERE state IN ({pending}) UNION "
        "SELECT e.effect_operation_id, e.strategy_instance_id FROM effect_operations e "
        "JOIN orders o ON o.effect_operation_id = e.effect_operation_id "
        f"WHERE lower(o.broker_state) IN ({working})), demand AS ("
        "SELECT symbol, 1 AS priority FROM strategy_instances WHERE retired_at_ms IS NULL "
        "UNION ALL SELECT symbol, 0 FROM positions WHERE attributed_qty != 0 "
        "UNION ALL SELECT i.symbol, 0 FROM strategy_instances i "
        "JOIN live_effects e ON e.strategy_instance_id = i.strategy_instance_id "
        "UNION ALL SELECT json_extract(t.facts_json, '$.leg.symbol') AS symbol, 0 "
        "FROM live_effects e JOIN custody_transitions t "
        "ON t.effect_operation_id = e.effect_operation_id "
        "WHERE e.strategy_instance_id IS NULL AND t.transition_kind = 'MANUAL_ORDER_ACCEPTED') "
        "SELECT symbol FROM demand GROUP BY symbol ORDER BY min(priority), symbol",
        (*sorted(NONTERMINAL_EFFECT_STATES), *sorted(WORKING_BROKER_STATES)),
    ).fetchall()
    return tuple(row["symbol"] for row in rows)


def strategy_instance(conn: sqlite3.Connection, strategy_instance_id: str) -> dict | None:
    row = conn.execute(
        "SELECT strategy_instance_id, symbol, config_hash, created_at_ms, retired_at_ms "
        "FROM strategy_instances WHERE strategy_instance_id = ?",
        (strategy_instance_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def bot_config(conn: sqlite3.Connection, strategy_instance_id: str) -> BotConfigResource | None:
    row = conn.execute(
        "SELECT strategy_instance_id, strategy_key, display_name, config_json, config_hash, created_at_ms "
        "FROM bot_config WHERE strategy_instance_id = ?",
        (strategy_instance_id,),
    ).fetchone()
    return BotConfigResource(**dict(row)) if row is not None else None


def decision_receipt_tail(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    limit: int,
) -> list[DecisionReceiptResource]:
    rows = conn.execute(
        f"SELECT {', '.join(_DECISION_RECEIPT_COLUMNS)} FROM decision_receipts "
        "WHERE strategy_instance_id = ? ORDER BY seq DESC LIMIT ?",
        (strategy_instance_id, limit),
    ).fetchall()
    return [DecisionReceiptResource(**dict(row)) for row in reversed(rows)]


def decision_receipts_by_transaction(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    transaction_ref: str,
    limit: int,
) -> list[DecisionReceiptResource]:
    rows = conn.execute(
        f"SELECT {', '.join(_DECISION_RECEIPT_COLUMNS)} FROM decision_receipts "
        "WHERE strategy_instance_id = ? AND (intent_id = ? OR order_ref = ?) "
        "ORDER BY seq DESC LIMIT ?",
        (strategy_instance_id, transaction_ref, transaction_ref, limit),
    ).fetchall()
    return [DecisionReceiptResource(**dict(row)) for row in reversed(rows)]


def _external_order_resource(row: sqlite3.Row) -> ExternalOrderResource:
    values = {column: row[column] for column in _EXTERNAL_ORDER_COLUMNS}
    evidence_refs = json.loads(values.pop("evidence_refs_json"))
    if not isinstance(evidence_refs, list) or not all(isinstance(item, str) for item in evidence_refs):
        raise ValueError("external order evidence_refs_json must be a string list")
    return ExternalOrderResource(
        **values,
        evidence_refs=tuple(evidence_refs),
        observation_sequence=row["observation_sequence"],
        acknowledgement_sequence=row["acknowledgement_sequence"],
        observation_recorded_at_ms=row["observation_recorded_at_ms"],
        acknowledgement_recorded_at_ms=row["acknowledgement_recorded_at_ms"],
    )


def external_order(
    conn: sqlite3.Connection,
    external_order_id: str,
) -> ExternalOrderResource | None:
    row = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo WHERE external_order_id = ?",
        (external_order_id,),
    ).fetchone()
    return _external_order_resource(row) if row is not None else None


def external_order_by_broker_order_id(
    conn: sqlite3.Connection,
    broker_order_id: str,
) -> ExternalOrderResource | None:
    row = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo WHERE broker_order_id = ?",
        (broker_order_id,),
    ).fetchone()
    return _external_order_resource(row) if row is not None else None


def external_orders(conn: sqlite3.Connection) -> list[ExternalOrderResource]:
    rows = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo "
        "ORDER BY eo.observed_at_ms DESC, eo.external_order_id DESC"
    ).fetchall()
    return [_external_order_resource(row) for row in rows]


def external_order_page(
    conn: sqlite3.Connection,
    *,
    observation_sequence_before: int | None,
    external_order_id_before: str | None,
    lifecycle_state: str | None,
    limit: int,
) -> list[ExternalOrderResource]:
    if (observation_sequence_before is None) != (external_order_id_before is None):
        raise ValueError("external-order cursor must include both keyset fields")
    lifecycle_predicate = {
        None: "",
        "review_required": "eo.acknowledged_at_ms IS NULL",
        "reviewed": "eo.acknowledged_at_ms IS NOT NULL",
    }.get(lifecycle_state)
    if lifecycle_predicate is None:
        raise ValueError("external-order lifecycle_state is invalid")
    params: tuple[object, ...]
    where_clauses: list[str] = []
    observation_sequence = (
        "(SELECT MIN(ct.sequence) FROM custody_transitions ct "
        "WHERE ct.broker_order_id = eo.broker_order_id "
        "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED')"
    )
    if observation_sequence_before is None:
        params = (limit,)
    else:
        where_clauses.append(
            f"({observation_sequence} < ? OR ({observation_sequence} = ? AND eo.external_order_id < ?))"
        )
        params = (
            observation_sequence_before,
            observation_sequence_before,
            external_order_id_before,
            limit,
        )
    if lifecycle_predicate:
        where_clauses.append(lifecycle_predicate)
    where = f"WHERE {' AND '.join(where_clauses)} " if where_clauses else ""
    rows = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo {where}"
        f"ORDER BY {observation_sequence} DESC, eo.external_order_id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_external_order_resource(row) for row in rows]


def external_orders_observed_since(conn: sqlite3.Connection, *, since_ms: int) -> int:
    """How many foreign orders were observed at or after ``since_ms``.

    The day-P&L fact reads this to decide whether it can vouch for the day at
    all: an order the Clerk did not place has no journaled fills, so its P&L
    is not in the FIFO and the day's number would be quietly wrong.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM external_orders WHERE observed_at_ms >= ?",
            (since_ms,),
        ).fetchone()[0]
    )


UNFOLDABLE_BROKER_ORDER_ACKNOWLEDGED_SUMMARY_CODE = "UNFOLDABLE_BROKER_ORDER_ACKNOWLEDGED"
# Evidence-ref prefixes on the acknowledgement's resolution transition. The
# prefixes keep the order id and the operator unambiguous in one ref list.
UNFOLDABLE_ACK_ORDER_REF_PREFIX = "broker_order:"
UNFOLDABLE_ACK_OPERATOR_REF_PREFIX = "operator:"


class UnfoldableBrokerOrderReview(NamedTuple):
    """One operator review of an unfoldable broker order, and what it covered.

    ``broker_state`` is the order's state inside the episode the review
    resolved: a later observation in a *different* state is new broker
    evidence the review never saw, so it is fenced again (#2363 review).
    """

    operator: str
    acknowledged_at_ms: int
    broker_state: str


def unfoldable_broker_order_acknowledgements(
    conn: sqlite3.Connection,
) -> dict[str, UnfoldableBrokerOrderReview]:
    """The latest operator review of each unfoldable broker order (#2363).

    The hash-chained resolution transition is the durable record: an
    unfoldable order has no ``external_orders`` row to carry the review. The
    read probes ``custody_transitions`` through the v16 partial
    ``summary_code`` index and joins each resolution to the episode it closed
    by primary key, so a resting order re-seen by every sweep never scans the
    append-only journal.
    """
    reviewed: dict[str, UnfoldableBrokerOrderReview] = {}
    for row in conn.execute(
        "SELECT t.facts_json AS resolution_json, t.recorded_at_ms, u.facts_json AS episode_json "
        "FROM custody_transitions t JOIN uncertainties u "
        "ON u.uncertainty_id = json_extract(t.facts_json, '$.uncertainty_id') "
        "WHERE t.transition_kind = 'UNCERTAINTY_RESOLVED' AND t.summary_code = ? "
        "ORDER BY t.sequence",
        (UNFOLDABLE_BROKER_ORDER_ACKNOWLEDGED_SUMMARY_CODE,),
    ):
        refs = json.loads(row["resolution_json"])["evidence_refs"]
        operator = next(
            ref.removeprefix(UNFOLDABLE_ACK_OPERATOR_REF_PREFIX)
            for ref in refs
            if ref.startswith(UNFOLDABLE_ACK_OPERATOR_REF_PREFIX)
        )
        states = {
            order["broker_order_id"]: order["broker_state"]
            for order in json.loads(row["episode_json"])["cause_facts"]["orders"]
        }
        for ref in refs:
            if ref.startswith(UNFOLDABLE_ACK_ORDER_REF_PREFIX):
                broker_order_id = ref.removeprefix(UNFOLDABLE_ACK_ORDER_REF_PREFIX)
                reviewed[broker_order_id] = UnfoldableBrokerOrderReview(
                    operator=operator,
                    acknowledged_at_ms=row["recorded_at_ms"],
                    broker_state=states[broker_order_id],
                )
    return reviewed


def unfoldable_broker_orders_active_since(
    conn: sqlite3.Connection, *, reason_code: str, since_ms: int
) -> int:
    """How many distinct unfoldable broker orders showed activity at or after ``since_ms``.

    The day-P&L fact's companion to :func:`external_orders_observed_since`:
    an order the Clerk could not record has no journaled fills either.
    Activity is the first observation or any later change of the order's
    broker state, so an order first seen yesterday that fills today counts
    today. Every episode row is read, resolved or not, so an acknowledged
    (released) order still counts for the day it was active; each row holds
    its episode's final cause, and the latest activity only ever grows. The
    v16 ``reason_code`` index keeps this off a full ``uncertainties`` scan.
    """
    latest_activity: dict[str, int] = {}
    for row in conn.execute(
        "SELECT facts_json FROM uncertainties WHERE reason_code = ?",
        (reason_code,),
    ):
        for order in json.loads(row["facts_json"])["cause_facts"]["orders"]:
            broker_order_id = order["broker_order_id"]
            latest_activity[broker_order_id] = max(
                order["last_activity_at_ms"],
                latest_activity.get(broker_order_id, order["last_activity_at_ms"]),
            )
    return sum(1 for active_at_ms in latest_activity.values() if active_at_ms >= since_ms)


def command(conn: sqlite3.Connection, command_id: str) -> CommandResource | None:
    row = conn.execute(
        f"SELECT {', '.join(_COMMAND_COLUMNS)} FROM commands WHERE command_id = ?",
        (command_id,),
    ).fetchone()
    return _row_to_command_resource(row) if row is not None else None


def command_by_idempotency_key(
    conn: sqlite3.Connection, *, authority_generation: int, idempotency_key: str
) -> CommandResource | None:
    row = conn.execute(
        f"SELECT {', '.join(_COMMAND_COLUMNS)} FROM commands WHERE authority_generation = ? AND idempotency_key = ?",
        (authority_generation, idempotency_key),
    ).fetchone()
    return _row_to_command_resource(row) if row is not None else None


def effect_operation(conn: sqlite3.Connection, effect_operation_id: str) -> EffectOperationResource | None:
    row = conn.execute(
        "SELECT effect_operation_id, authority_generation, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms, terminal_receipt_id FROM effect_operations "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def order(conn: sqlite3.Connection, order_ref: str) -> OrderResource | None:
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return OrderResource(**dict(row)) if row is not None else None


def order_for_effect_operation(conn: sqlite3.Connection, effect_operation_id: str) -> OrderResource | None:
    """The order originally created by a single-order effect (ENTER)."""
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return OrderResource(**dict(row)) if row is not None else None


def manual_order_ticket(conn: sqlite3.Connection, ticket_id: str) -> ManualOrderTicketResource | None:
    """Return one manual ticket and its bounded, durable leg resources."""
    ticket = conn.execute(
        "SELECT ticket_id, subject_id, operator_id, instruction_hash, state, created_at_ms, updated_at_ms "
        "FROM manual_order_tickets WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    if ticket is None:
        return None
    legs = conn.execute(
        "SELECT leg.ticket_id, leg.leg_id, leg.sequence_index, leg.subject_id, leg.instruction_hash, "
        "leg.command_id, leg.effect_operation_id, leg.order_ref, leg.state, leg.created_at_ms, "
        "leg.updated_at_ms, COALESCE(json_extract(reserved_leg.value, '$.instruction'), "
        "json_extract(acceptance.facts_json, '$.leg')) AS instruction_json "
        "FROM manual_order_legs leg "
        "LEFT JOIN custody_transitions reservation ON reservation.sequence = ("
        "SELECT MIN(candidate.sequence) FROM custody_transitions candidate "
        "WHERE candidate.transition_kind = 'MANUAL_TICKET_RESERVED' "
        "AND json_extract(candidate.facts_json, '$.ticket_id') = leg.ticket_id) "
        "LEFT JOIN json_each(reservation.facts_json, '$.legs') reserved_leg "
        "ON json_extract(reserved_leg.value, '$.leg_id') = leg.leg_id "
        "LEFT JOIN custody_transitions acceptance ON acceptance.sequence = ("
        "SELECT MIN(candidate.sequence) FROM custody_transitions candidate "
        "WHERE candidate.transition_kind = 'MANUAL_ORDER_ACCEPTED' "
        "AND candidate.effect_operation_id = leg.effect_operation_id) "
        "WHERE leg.ticket_id = ? ORDER BY leg.sequence_index ASC",
        (ticket_id,),
    ).fetchall()
    resources = []
    for leg in legs:
        values = dict(leg)
        instruction_json = values.pop("instruction_json")
        values["instruction"] = json.loads(instruction_json) if instruction_json is not None else None
        resources.append(ManualOrderLegResource(**values))
    return ManualOrderTicketResource(
        **dict(ticket),
        legs=tuple(resources),
    )


def manual_order_cancellation(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> ManualOrderCancellationResource | None:
    """Return the one durable cancellation owned by a manual order reference."""
    row = conn.execute(
        "SELECT order_ref, subject_id, cancel_request_id, command_id, effect_operation_id, "
        "state, created_at_ms, updated_at_ms FROM manual_order_cancellations WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return ManualOrderCancellationResource(**dict(row)) if row is not None else None


def manual_order_leg_for_order_ref(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
) -> ManualOrderLegResource | None:
    """Return the immutable ticket leg that owns one manual order reference."""
    row = conn.execute(
        "SELECT ticket_id, leg_id, sequence_index, subject_id, instruction_hash, command_id, effect_operation_id, "
        "order_ref, state, created_at_ms, updated_at_ms FROM manual_order_legs WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    if row is None:
        return None
    values = dict(row)
    values["instruction"] = None
    return ManualOrderLegResource(**values)


def manual_order_cancellation_for_effect(
    conn: sqlite3.Connection,
    *,
    effect_operation_id: str,
) -> ManualOrderCancellationResource | None:
    """Resolve a recovery effect back to its immutable manual target order."""
    row = conn.execute(
        "SELECT order_ref, subject_id, cancel_request_id, command_id, effect_operation_id, "
        "state, created_at_ms, updated_at_ms FROM manual_order_cancellations "
        "WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return ManualOrderCancellationResource(**dict(row)) if row is not None else None


def custody_subject(conn: sqlite3.Connection, subject_id: str) -> dict | None:
    row = conn.execute(
        "SELECT subject_id, kind, strategy_instance_id, operator_id, created_at_ms "
        "FROM custody_subjects WHERE subject_id = ?",
        (subject_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def orders_for_effect_operation(conn: sqlite3.Connection, effect_operation_id: str) -> list[OrderResource]:
    """Every order linked to this operation, without changing order origin."""
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, o.role, "
        "o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN operation_order_links l USING (order_ref) "
        "WHERE l.effect_operation_id = ? ORDER BY l.linked_at_ms ASC, o.order_ref ASC",
        (effect_operation_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def terminal_entry_orders_with_fills(
    conn: sqlite3.Connection, *, order_ref: str | None = None
) -> list[tuple[str, str]]:
    """``(order_ref, strategy_instance_id)`` of every ENTRY order carrying a fill
    row while its ENTER is ``failed``/``rejected`` (#2348).

    No fold terminalizes an ENTER that has an effective fill, so each row is a
    candidate contradiction; the caller nets superseded fills. ``order_ref``
    narrows the scan to one order (the trade_updates sink's early check).
    """
    rows = conn.execute(
        "SELECT o.order_ref, e.strategy_instance_id FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.role = 'ENTRY' AND e.kind = 'ENTER' AND e.state IN ('failed', 'rejected') "
        "AND e.strategy_instance_id IS NOT NULL "
        "AND (? IS NULL OR o.order_ref = ?) "
        "AND EXISTS (SELECT 1 FROM fills f WHERE f.order_ref = o.order_ref) "
        "ORDER BY o.order_ref ASC",
        (order_ref, order_ref),
    ).fetchall()
    return [(row["order_ref"], row["strategy_instance_id"]) for row in rows]


def terminal_entry_orders_unproven_at_broker(
    conn: sqlite3.Connection,
    *,
    symbols: frozenset[str],
    exclude_client_order_ids: frozenset[str],
    rested_since_ms: int,
    limit: int,
) -> list[str]:
    """ENTRY orders folded ``failed``/``rejected`` that no broker state ended (#2348).

    The sweep's exact-lookup worklist for a late fill the open-order snapshot
    cannot show: an ENTER voided on absence (#2342) or refused on a
    duplicate-id reply (#2304) never recorded a broker-terminal state, so the
    order may be live -- or already ``filled`` and closed -- at the broker.
    One the broker itself ended is proven and excluded. Narrowed to
    ``symbols`` (matched on the immutable ``ENTER_ACCEPTED`` leg).

    A lookup that finds nothing leaves ``broker_state`` NULL, so without a
    rotation the same never-landed voids would take every slot on every pass
    and starve an older void that did land. So each order's latest per-order
    reconciliation attempt (a Clerk-clock ``reconciliations`` row, recorded by
    every exact lookup) orders the list, never-looked-up first; an order
    last looked up after ``rested_since_ms`` is excluded until it has rested.
    Orders already in the open-order snapshot are dropped before ``limit``,
    so they cannot spend a slot either.
    """
    if not symbols:
        return []
    symbol_marks = ", ".join("?" for _ in symbols)
    excluded = sorted(exclude_client_order_ids)
    exclude_clause = (
        f"AND o.client_order_id NOT IN ({', '.join('?' for _ in excluded)}) " if excluded else ""
    )
    rows = conn.execute(
        "SELECT order_ref FROM ("
        "SELECT o.order_ref, e.updated_at_ms, "
        "(SELECT MAX(r.attempted_at_ms) FROM reconciliations r "
        "WHERE r.effect_operation_id = o.effect_operation_id "
        "AND r.order_ref = o.order_ref) AS last_lookup_ms "
        "FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "JOIN custody_transitions t ON t.order_ref = o.order_ref "
        "AND t.transition_kind = 'ENTER_ACCEPTED' "
        "WHERE o.role = 'ENTRY' AND e.kind = 'ENTER' AND e.state IN ('failed', 'rejected') "
        "AND e.strategy_instance_id IS NOT NULL "
        "AND (o.broker_state IS NULL OR LOWER(o.broker_state) NOT IN "
        "('filled','canceled','expired','rejected','replaced')) "
        f"AND UPPER(json_extract(t.facts_json, '$.leg.symbol')) IN ({symbol_marks}) "
        f"{exclude_clause}"
        ") WHERE last_lookup_ms IS NULL OR last_lookup_ms <= ? "
        "ORDER BY last_lookup_ms IS NOT NULL, last_lookup_ms ASC, "
        "updated_at_ms DESC, order_ref DESC LIMIT ?",
        (*sorted(symbols), *excluded, rested_since_ms, limit),
    ).fetchall()
    return [row["order_ref"] for row in rows]


def all_order_refs(conn: sqlite3.Connection) -> frozenset[str]:
    """Every immutable broker identity captured by this authority."""
    rows = conn.execute("SELECT order_ref FROM orders").fetchall()
    return frozenset(row["order_ref"] for row in rows)


def entry_orders_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> list[OrderResource]:
    """Every entry order whose immutable origin belongs to one strategy."""
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.role = 'ENTRY' AND e.strategy_instance_id = ? "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (strategy_instance_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def entry_orders_owed_a_cancel(conn: sqlite3.Connection) -> list[OrderResource]:
    """Working strategy ENTRYs whose run is not ACTIVE and that no EXIT owns.

    The reconciliation sweep's stopped-run worklist (#2362), account-wide in
    one read:

    * the order may still fill (:data:`CANCELLABLE_ENTRY_BROKER_STATES`);
    * its effect belongs to a strategy -- manual-custody orders belong to no
      run;
    * the effect's run is not ``ACTIVE`` (at most one run per strategy is);
    * no nonterminal EXIT links the order -- that EXIT's machine owns its
      cancel, the predicate of :func:`active_exit_for_order`.
    """
    states = ", ".join("?" for _ in CANCELLABLE_ENTRY_BROKER_STATES)
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.role = 'ENTRY' AND e.strategy_instance_id IS NOT NULL "
        f"AND LOWER(o.broker_state) IN ({states}) "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.run_id = e.run_id "
        "AND r.strategy_instance_id = e.strategy_instance_id AND r.state = 'ACTIVE') "
        "AND NOT EXISTS (SELECT 1 FROM operation_order_links l "
        "JOIN effect_operations x ON x.effect_operation_id = l.effect_operation_id "
        "WHERE l.order_ref = o.order_ref AND x.kind = 'EXIT' "
        "AND x.state NOT IN ('succeeded','failed','rejected')) "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        tuple(sorted(CANCELLABLE_ENTRY_BROKER_STATES)),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def orders_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> list[OrderResource]:
    """Every order (ENTRY and REDUCING alike) belonging to one strategy.

    Unlike :func:`entry_orders_for_strategy`, this is not role-filtered — the
    custody-proof surface needs a live EXIT's REDUCING child counted as
    working/unresolved exposure too, not just its cancelled ENTRY siblings.
    """
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.strategy_instance_id = ? "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (strategy_instance_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def active_exit_for_order(conn: sqlite3.Connection, order_ref: str) -> EffectOperationResource | None:
    """The nonterminal EXIT currently linked to an entry, if any."""
    row = conn.execute(
        "SELECT e.effect_operation_id, e.authority_generation, e.idempotency_key, e.command_id, "
        "e.strategy_instance_id, e.run_id, e.kind, e.state, e.custody_owner, e.created_at_ms, "
        "e.updated_at_ms, e.terminal_receipt_id FROM effect_operations e "
        "JOIN operation_order_links l ON l.effect_operation_id = e.effect_operation_id "
        "WHERE l.order_ref = ? AND e.kind = 'EXIT' "
        "AND e.state NOT IN ('succeeded','failed','rejected') "
        "ORDER BY e.created_at_ms DESC LIMIT 1",
        (order_ref,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def active_exit_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> EffectOperationResource | None:
    """The strategy's live EXIT fence against concurrently admitted ENTERs."""
    row = conn.execute(
        "SELECT effect_operation_id, authority_generation, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, "
        "updated_at_ms, terminal_receipt_id FROM effect_operations "
        "WHERE strategy_instance_id = ? AND kind = 'EXIT' "
        "AND state NOT IN ('succeeded','failed','rejected') "
        "ORDER BY created_at_ms DESC LIMIT 1",
        (strategy_instance_id,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def reconcilable_effect_operations(
    conn: sqlite3.Connection, *, subject_id: str | None = None
) -> list[EffectOperationResource]:
    """Distinct nonterminal broker-facing operations requiring fresh evidence.

    ``subject_id`` narrows the read to one custody subject (#1793). The filter
    is total: ``effect_operations.subject_id`` is NOT NULL with a foreign key
    into ``custody_subjects``, so every operation belongs to exactly one
    subject and no unattributable remainder exists. ``None`` keeps the
    account-wide read, which is what a boot-time account summary wants.

    A terminal order stays on the worklist while its effective fills fall
    short of the broker's own cumulative ``filled_quantity`` (#2305): a lost
    ``trade_updates`` slice -- dropped at capture, or executed before the
    first ``listen`` -- is re-derived by the sweep's exact lookup, whose
    cumulative fold then closes the shortfall and drops it off the list.
    """
    subject_clause = "AND e.subject_id = ? " if subject_id is not None else ""
    params: tuple[object, ...] = (subject_id,) if subject_id is not None else ()
    rows = conn.execute(
        "SELECT DISTINCT e.effect_operation_id, e.authority_generation, e.idempotency_key, "
        "e.command_id, e.strategy_instance_id, e.run_id, e.kind, e.state, e.custody_owner, "
        "e.created_at_ms, e.updated_at_ms, e.terminal_receipt_id "
        "FROM effect_operations e LEFT JOIN operation_order_links l "
        "ON l.effect_operation_id = e.effect_operation_id LEFT JOIN orders o "
        "ON o.order_ref = l.order_ref WHERE e.kind IN ('ENTER','EXIT','MANUAL_ORDER','CANCEL') "
        "AND e.state NOT IN ('succeeded','failed','rejected') "
        + subject_clause +
        "AND (e.state IN ('accepted','unknown') OR e.kind IN ('EXIT','CANCEL') "
        "OR o.broker_state IS NULL OR lower(o.broker_state) NOT IN "
        "('filled','canceled','expired','rejected','replaced') "
        "OR (lower(o.broker_state) = 'filled' AND NOT EXISTS ("
        "SELECT 1 FROM fills f WHERE f.order_ref = o.order_ref "
        f"AND {_EFFECTIVE_FILL_PREDICATE})) "
        f"OR {_fills_short_of_broker_cumulative_sql('o.order_ref')}) "
        "ORDER BY e.created_at_ms ASC",
        (*params, FILL_QTY_EPSILON),
    ).fetchall()
    return [EffectOperationResource(**dict(row)) for row in rows]


def position(conn: sqlite3.Connection, strategy_instance_id: str, symbol: str) -> float:
    row = conn.execute(
        "SELECT attributed_qty FROM positions WHERE strategy_instance_id = ? AND symbol = ?",
        (strategy_instance_id, symbol.upper()),
    ).fetchone()
    return row["attributed_qty"] if row is not None else 0.0


def fills_for_order(conn: sqlite3.Connection, order_ref: str) -> list[dict]:
    rows = conn.execute(
        "SELECT fill_id, order_ref, qty, price, side, is_correction, execution_id, evidence_source, "
        "event_kind, superseded_execution_ref, fee, fee_fidelity, source_event_at_ms, "
        "clerk_observed_at_ms, recorded_at_ms FROM fills WHERE order_ref = ? "
        "ORDER BY source_event_at_ms ASC, recorded_transition_sequence ASC, fill_id ASC",
        (order_ref,),
    ).fetchall()
    return [dict(row) for row in rows]


def execution_exists(conn: sqlite3.Connection, execution_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM fills WHERE execution_id = ? LIMIT 1", (execution_id,)).fetchone()
    return row is not None


def cumulative_recovery_fill_exists_for_order(conn: sqlite3.Connection, order_ref: str) -> bool:
    """Whether aggregate recovery has already covered part of one order.

    Recovery evidence has no execution identity, so a later exact websocket
    slice cannot be safely merged into the same order without proving its
    coverage relationship to the aggregate total.
    """
    row = conn.execute(
        "SELECT 1 FROM fills WHERE order_ref = ? AND evidence_source = 'cumulative_recovery' LIMIT 1",
        (order_ref,),
    ).fetchone()
    return row is not None


def cumulative_recovery_fill_ids_for_order(conn: sqlite3.Connection, order_ref: str) -> list[str]:
    """Return the immutable fold identities covered by aggregate recovery."""
    rows = conn.execute(
        "SELECT fill_id FROM fills WHERE order_ref = ? AND evidence_source = 'cumulative_recovery' ORDER BY fill_id",
        (order_ref,),
    ).fetchall()
    return [str(row["fill_id"]) for row in rows]


def correction_uncertainty_exists(conn: sqlite3.Connection, execution_id: str) -> bool:
    """Whether an invalid correction already durably raised its uncertainty.

    Invalid corrections intentionally do not create a ``fills`` execution
    row, so their broker execution identity lives in the typed uncertainty
    cause. Parsing the small closed transition subset avoids relying on an
    optional SQLite JSON extension while preserving crash/restart dedup.
    """
    rows = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE transition_kind = 'UNCERTAINTY_RAISED'"
    ).fetchall()
    for row in rows:
        facts = json.loads(row["facts_json"])
        if facts.get("cause_facts", {}).get("execution_id") == execution_id:
            return True
    return False


def effective_execution_slice(conn: sqlite3.Connection, execution_id: str) -> dict | None:
    """Return one currently effective execution plus its immutable custody owner.

    Corrections are append-only replacement rows. A row is effective only
    while no later row names its execution identity as ``superseded``.
    """
    row = conn.execute(
        "SELECT f.execution_id, f.order_ref, f.qty, f.price, f.side, f.evidence_source, f.fee, "
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
        (execution_id,),
    ).fetchone()
    return dict(row) if row is not None else None


#: A fill row is effective while no correction names its ``execution_id`` as
#: superseded (PRD #1441 S1.2). Expects the row aliased ``f``.
_EFFECTIVE_FILL_PREDICATE = (
    "NOT EXISTS (SELECT 1 FROM fills successor "
    "WHERE successor.superseded_execution_ref = f.execution_id)"
)


def _effective_fill_totals_for_order(
    conn: sqlite3.Connection,
    order_ref: str,
    *,
    evidence_sources: tuple[str, ...] | None = None,
) -> tuple[float, float]:
    """Canonical effective-fill aggregation with an optional evidence filter."""
    source_predicate = ""
    parameters: tuple[object, ...] = (order_ref,)
    if evidence_sources is not None:
        placeholders = ", ".join("?" for _ in evidence_sources)
        source_predicate = f"AND f.evidence_source IN ({placeholders}) "
        parameters += evidence_sources
    row = conn.execute(
        "SELECT COALESCE(SUM(f.qty), 0) AS qty, COALESCE(SUM(f.qty * f.price), 0) AS cost "
        "FROM fills f WHERE f.order_ref = ? " + source_predicate + "AND " + _EFFECTIVE_FILL_PREDICATE,
        parameters,
    ).fetchone()
    return float(row["qty"]), float(row["cost"])


def _latest_reported_filled_quantity_sql(order_ref_sql: str) -> str:
    """SQL scalar: the cumulative filled quantity the order's LATEST acknowledgement reported.

    The one reading of ``ORDER_SUBMIT_ACKED.facts_json.reported_filled_quantity``
    (#2305). It is the latest acknowledgement by sequence, not the largest:
    a later exact REST lookup that reports less than an earlier websocket
    frame is the broker's current word, and it must be able to close the
    gap -- a maximum would keep the order short for ever. ``NULL`` when the
    latest acknowledgement reported no fill (and for every pre-#2305 row).
    """
    return (
        "(SELECT CAST(json_extract(t.facts_json, '$.reported_filled_quantity') AS REAL) "
        "FROM custody_transitions t WHERE t.order_ref = " + order_ref_sql + " "
        "AND t.transition_kind = 'ORDER_SUBMIT_ACKED' ORDER BY t.sequence DESC LIMIT 1)"
    )


def _fills_short_of_broker_cumulative_sql(order_ref_sql: str) -> str:
    """SQL predicate: the latest broker-reported cumulative exceeds the effective fills.

    ``NULL`` (never short) when the latest acknowledgement reported no fill.
    ``order_ref_sql`` appears twice; binds :data:`FILL_QTY_EPSILON` last. The
    fill sum is covered by ``ix_fills_order_ref`` and the effective-fill
    predicate by ``ix_fills_superseded_execution_ref`` (schema v15).
    """
    return (
        "(" + _latest_reported_filled_quantity_sql(order_ref_sql) + " - "
        "(SELECT COALESCE(SUM(f.qty), 0) FROM fills f WHERE f.order_ref = " + order_ref_sql + " "
        "AND " + _EFFECTIVE_FILL_PREDICATE + ") >= ?)"
    )


def latest_reported_filled_quantity(conn: sqlite3.Connection, order_ref: str) -> float | None:
    """The cumulative filled quantity the order's latest acknowledgement reported, if any."""
    row = conn.execute(
        "SELECT " + _latest_reported_filled_quantity_sql("?") + " AS reported", (order_ref,)
    ).fetchone()
    return float(row["reported"]) if row["reported"] is not None else None


def latest_acknowledgement_in_states(
    conn: sqlite3.Connection,
    order_ref: str,
    broker_states: frozenset[str],
) -> tuple[str, float | None] | None:
    """The state and reported cumulative of the order's latest acknowledgement in ``broker_states``.

    Both values come from the SAME acknowledgement. The ``orders`` fold keeps
    a terminal state once it has seen one while a later stale REST fold can
    still append a working-state acknowledgement with an older cumulative,
    so pairing the order row's state with the latest acknowledgement's total
    could claim a final total the broker never reported (#2346).
    """
    rows = conn.execute(
        "SELECT broker_state, CAST(json_extract(facts_json, '$.reported_filled_quantity') AS REAL) "
        "AS reported FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind = 'ORDER_SUBMIT_ACKED' ORDER BY sequence DESC",
        (order_ref,),
    )
    for row in rows:
        state = row["broker_state"]
        if isinstance(state, str) and state.lower() in broker_states:
            return state, (float(row["reported"]) if row["reported"] is not None else None)
    return None


def order_fills_short_of_broker_cumulative(conn: sqlite3.Connection, order_ref: str) -> bool:
    """Whether the order's effective fills fall short of the broker's latest cumulative (#2305).

    The one definition shared by the reconciliation worklist
    (:func:`reconcilable_effect_operations`) and the EXIT machine's
    reducing-order refresh. Bounded by construction: one exact REST lookup
    appends an acknowledgement carrying the broker's current cumulative and
    folds fills up to it, so after any successful lookup the order is short
    only if the broker itself still reports more than it has recorded.
    """
    row = conn.execute(
        "SELECT " + _fills_short_of_broker_cumulative_sql("?") + " AS short",
        (order_ref, order_ref, FILL_QTY_EPSILON),
    ).fetchone()
    return bool(row["short"])


def effective_fill_totals_for_order(conn: sqlite3.Connection, order_ref: str) -> tuple[float, float]:
    """Return the quantity and cost of the order's current effective leaves.

    Formula: ``effective_qty = SUM(qty)`` and ``effective_cost = SUM(qty * price)``
    for fills with no successor naming their ``execution_id`` as superseded.
    Reference: PRD #1441 S1.2 execution corrections.
    Canonical implementation: this query, reused by cumulative recovery.
    Validated against: ``test_cumulative_recovery_fill_is_explicitly_tagged``.
    """
    return _effective_fill_totals_for_order(conn, order_ref)


def effective_exact_fill_totals_for_order(conn: sqlite3.Connection, order_ref: str) -> tuple[float, float]:
    """Return current exact-execution quantity and cost for one order.

    Aggregate REST recovery is deliberately excluded: it can establish
    exposure and an acknowledgement, but only a broker-issued execution ID
    may complete a manual ticket.
    """
    return _effective_fill_totals_for_order(
        conn,
        order_ref,
        evidence_sources=("websocket", "activity_recovery"),
    )


def uncertain_orders(conn: sqlite3.Connection) -> list[OrderResource]:
    """Every order whose effect operation is still ``unknown`` (#1378) — the
    reconciliation sweep's own worklist. Joins rather than filtering
    ``orders`` directly: "uncertain" is a property of the effect operation
    (R4), not of a column on ``orders`` itself."""
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.state = 'unknown' ORDER BY o.updated_at_ms ASC"
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


def attributed_positions_by_symbol(conn: sqlite3.Connection) -> dict[str, float]:
    """Account-wide Clerk-attributed exposure per symbol (#1378) — the sum of
    every bot's namespace-attributed ``positions`` row, for comparison
    against the broker's own account-wide position snapshot. Never nets
    against the raw broker position; this is our side of that comparison."""
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, SUM(attributed_qty) AS qty FROM positions GROUP BY UPPER(symbol)"
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


def attributed_positions_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, SUM(attributed_qty) AS qty FROM positions "
        "WHERE strategy_instance_id = ? GROUP BY UPPER(symbol)",
        (strategy_instance_id,),
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


def attributed_positions_for_subject(conn: sqlite3.Connection, subject_id: str) -> dict[str, float]:
    """Return one custody subject's position projection without bot aliases."""
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, SUM(attributed_qty) AS qty FROM positions "
        "WHERE subject_id = ? GROUP BY UPPER(symbol)",
        (subject_id,),
    ).fetchall()
    return {row["symbol"]: row["qty"] for row in rows}


def manual_reduction_available_quantity(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    symbol: str,
) -> float:
    """Return manual-owned long quantity not already reserved for a sell.

    Formula: ``max(0, folded_manual_long - pending_manual_sell_qty)`` where
    each pending sell quantity is its requested quantity less its current
    effective filled quantity.
    Reference: docs/prds/2026-08-13-sqlite-clerk-manual-orders.md §8.
    Canonical implementation: this file.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::
      test_manual_sell_reserves_only_its_subject_long_position.

    The position projection includes exact fills and corrections.  Each
    accepted, working, or unknown manual sell remains reserved until its own
    effect becomes terminal, so concurrent confirmations cannot oversell the
    custody subject while a broker response is pending.
    """
    normalized_symbol = symbol.upper()
    position = conn.execute(
        "SELECT attributed_qty FROM positions WHERE subject_id = ? AND symbol = ?",
        (subject_id, normalized_symbol),
    ).fetchone()
    long_quantity = max(0.0, float(position["attributed_qty"]) if position is not None else 0.0)
    reservations = conn.execute(
        "SELECT leg.order_ref, CAST(json_extract(acceptance.facts_json, '$.leg.quantity') AS REAL) "
        "AS requested_qty FROM effect_operations effect "
        "JOIN manual_order_legs leg ON leg.effect_operation_id = effect.effect_operation_id "
        "JOIN custody_transitions acceptance "
        "ON acceptance.sequence = ("
        "SELECT MIN(candidate.sequence) FROM custody_transitions candidate "
        "WHERE candidate.effect_operation_id = effect.effect_operation_id "
        "AND candidate.transition_kind = 'MANUAL_ORDER_ACCEPTED') "
        "WHERE effect.kind = 'MANUAL_ORDER' AND effect.subject_id = ? "
        "AND effect.state NOT IN ('succeeded', 'failed', 'rejected') "
        "AND UPPER(json_extract(acceptance.facts_json, '$.leg.symbol')) = ? "
        "AND UPPER(json_extract(acceptance.facts_json, '$.leg.side')) = 'SELL'",
        (subject_id, normalized_symbol),
    ).fetchall()
    reserved_quantity = sum(
        max(
            0.0,
            float(row["requested_qty"]) - effective_fill_totals_for_order(conn, str(row["order_ref"]))[0],
        )
        for row in reservations
    )
    return max(0.0, long_quantity - reserved_quantity)


def has_nonterminal_manual_order(conn: sqlite3.Connection) -> bool:
    """Whether any manual order leaves account-wide new exposure unsafe."""
    row = conn.execute(
        "SELECT 1 FROM effect_operations WHERE kind = 'MANUAL_ORDER' "
        "AND state NOT IN ('succeeded', 'failed', 'rejected') LIMIT 1"
    ).fetchone()
    return row is not None


def has_nonterminal_manual_order_outside_ticket(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
) -> bool:
    """Whether another ticket leaves new manual exposure unsafe.

    Ordered continuation may follow a broker-acknowledged leg in its *own*
    ticket, but it must never bypass an unresolved manual order from another
    ticket. This remains a repository read, not a UI inference.
    """
    row = conn.execute(
        "SELECT 1 FROM effect_operations effect "
        "JOIN manual_order_legs leg ON leg.effect_operation_id = effect.effect_operation_id "
        "WHERE effect.kind = 'MANUAL_ORDER' "
        "AND effect.state NOT IN ('succeeded', 'failed', 'rejected') "
        "AND leg.ticket_id != ? LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return row is not None


def active_hold(conn: sqlite3.Connection, *, scope: str, reason_code: str) -> dict | None:
    """The current ``ACTIVE`` hold for this ``(scope, reason_code)``, if any —
    reconciliation's idempotency check before raising a new one (#1378)."""
    row = conn.execute(
        "SELECT hold_id, scope, strategy_instance_id, reason_code, state, opened_at_ms, "
        "resolved_at_ms, evidence_refs_json FROM holds "
        "WHERE scope = ? AND reason_code = ? AND state = 'ACTIVE' "
        "ORDER BY opened_at_ms DESC LIMIT 1",
        (scope, reason_code),
    ).fetchone()
    return dict(row) if row is not None else None


_UNCERTAINTY_COLUMNS = (
    "uncertainty_id, scope, severity, blocks_new_exposure, allows_reduction, custody_owner, "
    "strategy_instance_id, reason_code, headline, explanation, operator_impact, next_step, "
    "observed_at_ms, resolved_at_ms, evidence_refs_json, facts_schema_version, facts_json"
)


def active_uncertainty(
    conn: sqlite3.Connection, *, scope: str, reason_code: str, strategy_instance_id: str | None
) -> dict | None:
    """The current ``ACTIVE`` (``resolved_at_ms IS NULL``) uncertainty for
    this ``(scope, reason_code, strategy_instance_id)``, if any — the
    idempotency check before raising a new one (#1380). ``strategy_instance_id``
    is part of the key (unlike ``active_hold``, which never needs it since
    every hold raised so far is ``ACCOUNT_CLERK``-scoped): two different bots'
    ``CUSTODY_SUBJECT``-scoped uncertainties sharing the same ``reason_code`` must never
    be confused for one another."""
    row = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE scope = ? AND reason_code = ? AND strategy_instance_id IS ? "
        "AND resolved_at_ms IS NULL ORDER BY observed_at_ms DESC LIMIT 1",
        (scope, reason_code, strategy_instance_id),
    ).fetchone()
    return dict(row) if row is not None else None


def uncertainty_history(
    conn: sqlite3.Connection, *, scope: str, reason_code: str, strategy_instance_id: str | None
) -> list[dict]:
    """Every episode, active or resolved, for one ``(scope, reason_code, instance)``.

    Newest episode first. For a detector that must not re-raise a cause the
    latest episode already answered (#2348); the active-only read cannot see
    an episode a legitimate flatten resolved. Episodes of one identity never
    overlap, so the raising transition's sequence (``uncertainty:<seq>``)
    orders them exactly; a clock tie or a lexical id sort would not.
    """
    rows = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE scope = ? AND reason_code = ? AND strategy_instance_id IS ? "
        "ORDER BY CAST(SUBSTR(uncertainty_id, INSTR(uncertainty_id, ':') + 1) AS INTEGER) DESC",
        (scope, reason_code, strategy_instance_id),
    ).fetchall()
    return [dict(row) for row in rows]


def active_uncertainties(conn: sqlite3.Connection) -> list[dict]:
    """Every unresolved uncertainty on the account, oldest observation first.

    The lane's attention set (#2228): one row per raised condition, keyed by
    ``uncertainty_id`` — the stable identity a bell dedupes and deep-links
    from. Deliberately not admission-shaped: the bell lists everything
    needing the operator, not one subject's gating facts.
    """
    rows = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE resolved_at_ms IS NULL "
        "ORDER BY observed_at_ms ASC, uncertainty_id ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def active_uncertainties_for_admission(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str | None = None,
    subject_id: str | None = None,
) -> list[dict]:
    """Return account-wide plus exactly one custody subject's uncertainty."""
    if (strategy_instance_id is None) == (subject_id is None):
        raise ValueError("admission reads require exactly one subject identity")
    rows = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE resolved_at_ms IS NULL AND (scope = 'ACCOUNT_CLERK' "
        "OR (strategy_instance_id IS NOT NULL AND strategy_instance_id = ?) "
        "OR (subject_id IS NOT NULL AND subject_id = ?))",
        (strategy_instance_id, subject_id),
    ).fetchall()
    return [dict(row) for row in rows]


def active_holds_for_admission(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str | None = None,
    subject_id: str | None = None,
) -> list[dict]:
    """Return account-wide plus exactly one custody subject's active holds."""
    if (strategy_instance_id is None) == (subject_id is None):
        raise ValueError("admission reads require exactly one subject identity")
    rows = conn.execute(
        "SELECT hold_id, scope, subject_id, strategy_instance_id, reason_code, state, opened_at_ms, "
        "resolved_at_ms, evidence_refs_json FROM holds "
        "WHERE state = 'ACTIVE' AND (scope = 'ACCOUNT_CLERK' "
        "OR (strategy_instance_id IS NOT NULL AND strategy_instance_id = ?) "
        "OR (subject_id IS NOT NULL AND subject_id = ?))",
        (strategy_instance_id, subject_id),
    ).fetchall()
    return [dict(row) for row in rows]
