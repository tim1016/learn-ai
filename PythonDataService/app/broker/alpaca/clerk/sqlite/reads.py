"""Typed read snapshots over the SQLite Clerk spine.

Pure ``SELECT`` + row-to-dataclass mapping, no write path, no lock, no fold
concerns — split out of ``repository.py`` to keep that module under the
file-size ceiling as new read surfaces accumulate slice over slice.
``ClerkSqliteRepository`` methods delegate here; callers outside this package
still never see a cursor — they go through the repository, which
happens to forward to this module for these queries.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Sequence
from typing import NamedTuple

from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ORDER_CONTRACT_JOINS_SQL,
    ORDER_CONTRACT_SYMBOL_SQL,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    FILL_QTY_EPSILON,
    FINAL_CUMULATIVE_BROKER_STATES,
)
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
from app.broker.alpaca.clerk.sqlite.option_order_legs import MULTI_LEG_PARENT_SQL, NOT_A_LEG_SQL
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    sql_string_list,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_PRICE_CONFLICT_REASON_CODE,
    ExecutionPriceConflictCause,
)

# The one terminal broker-state set, spelled for SQL (``order_projection``).
_TERMINAL_BROKER_STATES_SQL = sql_string_list(ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES)

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
    + ", (SELECT ct.facts_json FROM custody_transitions ct "
    "WHERE ct.broker_order_id = eo.broker_order_id "
    "AND ct.transition_kind = 'EXTERNAL_ORDER_OBSERVED' "
    "ORDER BY ct.sequence DESC LIMIT 1) AS latest_observation_facts"
)


def _manual_chain_order_ref_sql(broker_order_id: str) -> str:
    """The manual legs whose Alpaca replacement chain holds the broker order ``broker_order_id`` names.

    ``broker_order_id`` is an SQL expression: a parameter, or a column of the
    enclosing query. The one statement of chain membership (#2656) that
    :func:`manual_chain_order_ref` and :data:`_OUTSIDE_ORDER_SQL` both read.
    """
    return (
        "SELECT o.order_ref FROM orders o "
        "JOIN manual_order_legs l ON l.order_ref = o.order_ref "
        f"WHERE o.broker_order_id = {broker_order_id} "
        "UNION "
        "SELECT t.order_ref FROM custody_transitions t "
        "WHERE t.order_ref IN (SELECT order_ref FROM manual_order_legs WHERE order_ref IS NOT NULL) "
        "AND t.transition_kind = 'MANUAL_ORDER_REPLACED' "
        f"AND (t.broker_order_id = {broker_order_id} OR json_extract(t.facts_json, '$.replaces') = {broker_order_id})"
    )


#: The bookings of an outside order's fill against the bot that held its option
#: contract (ADR 0076 decision 22). Such a booking is an ``OPTION_POSITION_ENDED``
#: record that names the outside order, on an order bound to the contract. Read
#: from the orders bound to a contract outward (``CROSS JOIN`` keeps that order),
#: so books with no option order read one empty table and nothing else.
_BOOKED_AS_A_BOTS_SALE_SQL = (
    "SELECT t.broker_order_id FROM order_contracts bound "
    "CROSS JOIN custody_transitions t ON t.order_ref = bound.order_ref "
    "WHERE t.transition_kind = 'OPTION_POSITION_ENDED' AND t.broker_order_id IS NOT NULL"
)

#: Whether the ``external_orders`` row ``eo`` is still an outside order. Only
#: this module's reads apply it. Alpaca gives a replacement a client id of its
#: own, so the Clerk can take a replacement for a foreign order before it
#: knows the manual leg's chain holds it (#2656). From the moment the chain holds it, the order is
#: the leg's own and its executions are the leg's (#2787): its row stays as
#: the record of what was observed, but no money read counts it outside the
#: Clerk again, so its fill is counted once. The same holds from the moment an
#: outside order's fill is booked against the bot that held its option contract
#: (ADR 0076 decision 22): the booking is the execution, and it is the bot's.
_OUTSIDE_ORDER_SQL = (
    f"NOT EXISTS ({_manual_chain_order_ref_sql('eo.broker_order_id')}) "
    f"AND NOT EXISTS ({_BOOKED_AS_A_BOTS_SALE_SQL} AND bound.symbol = UPPER(eo.symbol) "
    "AND t.broker_order_id = eo.broker_order_id)"
)


def _row_to_command_resource(row: sqlite3.Row) -> CommandResource:
    return CommandResource(**{column: row[column] for column in _COMMAND_COLUMNS})


def control_meta_snapshot(conn: sqlite3.Connection) -> ControlMetaSnapshot:
    row = conn.execute(
        "SELECT schema_version, account_id, db_identity_token, authority_generation, "
        "control_revision, created_at_ms, last_open_at_ms FROM control_meta WHERE id = 1"
    ).fetchone()
    return ControlMetaSnapshot(**dict(row))


def last_custody_sequence(conn: sqlite3.Connection, strategy_instance_id: str | None = None) -> int:
    """The newest custody transition's sequence, the ledger's or one bot's; 0 when none.

    Per bot it reads the ``(strategy_instance_id, sequence)`` index. Every
    transition that moves a bot's orders, effects, fills or runs carries
    its ``strategy_instance_id``; the untagged ones are account-level
    (uncertainties, fee evidence, budgets, risk limits) or belong to
    manual and outside orders, which are never a bot's custody.
    """
    if strategy_instance_id is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions "
            "WHERE strategy_instance_id = ?",
            (strategy_instance_id,),
        ).fetchone()
    return int(row["sequence"])


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

#: Effect-operation states that ended in failure. The effect's order row and
#: its decision receipt stay stored, so their presence alone never says the
#: effect happened: the terminal state does.
FAILED_EFFECT_STATES: frozenset[str] = frozenset({"failed", "rejected"})


def strategy_instances_with_live_custody(conn: sqlite3.Connection, *, dormant: bool = False) -> set[str]:
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

    ``dormant`` is a store no Clerk reconciles again: a live account's Shadow
    world once the account is live (#2694). No pass there ever ends a filled
    ENTER's effect, so the nonterminal-effect arm would hold a flat bot for
    good. For such a store that one arm is narrowed to the effects still owed
    broker evidence (``reconcilable_effect_operations``), Clear's own test of
    them (``recorded_custody``). Every other arm stands as it is.
    """
    working_states = ", ".join("?" for _ in WORKING_BROKER_STATES)
    nonterminal_states = ", ".join("?" for _ in NONTERMINAL_EFFECT_STATES)
    held = (
        "SELECT strategy_instance_id FROM uncertainties "
        "WHERE resolved_at_ms IS NULL AND strategy_instance_id IS NOT NULL "
        "UNION SELECT strategy_instance_id FROM positions "
        "WHERE attributed_qty <> 0 AND strategy_instance_id IS NOT NULL "
        "UNION SELECT strategy_instance_id FROM runs WHERE state = 'ACTIVE' "
        "UNION SELECT e.strategy_instance_id FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"WHERE LOWER(o.broker_state) IN ({working_states}) "
        "AND e.strategy_instance_id IS NOT NULL "
    )
    if dormant:
        rows = conn.execute(held, tuple(sorted(WORKING_BROKER_STATES))).fetchall()
        owed_evidence = {
            effect.strategy_instance_id
            for effect in reconcilable_effect_operations(conn)
            if effect.strategy_instance_id is not None
        }
        return {str(row["strategy_instance_id"]) for row in rows} | owed_evidence
    rows = conn.execute(
        held + "UNION SELECT strategy_instance_id FROM effect_operations "
        f"WHERE state IN ({nonterminal_states}) AND strategy_instance_id IS NOT NULL",
        (*sorted(WORKING_BROKER_STATES), *sorted(NONTERMINAL_EFFECT_STATES)),
    ).fetchall()
    return {str(row["strategy_instance_id"]) for row in rows}


def market_data_symbols(conn: sqlite3.Connection) -> tuple[str, ...]:
    """One projection of deployments and custody using the canonical live sets.

    Strategy identity supplies bot symbols without decoding historical order
    legs or fills. Only manual orders need their immutable acceptance symbol.
    Retired strategies remain watched while they still have custody.

    A position in an option contract (ADR 0076) is watched through the symbol
    its bot is registered under, the underlying: a contract's own symbol is
    never a stock line to watch.
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
        "UNION ALL SELECT p.symbol, 0 FROM positions p WHERE p.attributed_qty != 0 "
        "AND NOT EXISTS (SELECT 1 FROM option_contracts c WHERE c.symbol = p.symbol) "
        "UNION ALL SELECT i.symbol, 0 FROM positions p "
        "JOIN option_contracts c ON c.symbol = p.symbol "
        "JOIN strategy_instances i ON i.strategy_instance_id = p.strategy_instance_id "
        "WHERE p.attributed_qty != 0 "
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


def option_contracts_to_watch(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The option contracts this account must have quoted, by OCC symbol (ADR 0076).

    A contract a bot holds, and a contract an order is working in at the
    broker. The sibling of :func:`market_data_symbols` for contracts: a
    contract is told by its recorded terms, never by its symbol's spelling,
    so a stock position is never in the answer.
    """
    working = ",".join("?" for _ in WORKING_BROKER_STATES)
    rows = conn.execute(
        "SELECT p.symbol AS symbol FROM positions p JOIN option_contracts c ON c.symbol = p.symbol "
        "WHERE p.attributed_qty != 0 "
        "UNION SELECT oc.symbol FROM order_contracts oc JOIN orders o ON o.order_ref = oc.order_ref "
        f"WHERE lower(o.broker_state) IN ({working}) ORDER BY symbol",
        tuple(sorted(WORKING_BROKER_STATES)),
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


def strategy_ran_before(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    lifecycle_run_id: str,
) -> bool:
    """Whether the instance has a run on record other than ``lifecycle_run_id`` (#2859)."""
    row = conn.execute(
        "SELECT 1 FROM runs WHERE strategy_instance_id = ? AND lifecycle_run_id != ? LIMIT 1",
        (strategy_instance_id, lifecycle_run_id),
    ).fetchone()
    return row is not None


def decision_receipt_tail(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    limit: int,
    before_seq: int | None = None,
) -> list[DecisionReceiptResource]:
    """The newest ``limit`` receipts in ascending sequence order.

    ``before_seq`` reads the newest ``limit`` rows older than that sequence
    instead (#2859). Either way it is one bounded range walk down the primary
    key, starting where the range does.
    """
    older_than = "" if before_seq is None else "AND seq < ? "
    bounds = (limit,) if before_seq is None else (before_seq, limit)
    rows = conn.execute(
        f"SELECT {', '.join(_DECISION_RECEIPT_COLUMNS)} FROM decision_receipts "
        f"WHERE strategy_instance_id = ? {older_than}ORDER BY seq DESC LIMIT ?",
        (strategy_instance_id, *bounds),
    ).fetchall()
    return [DecisionReceiptResource(**dict(row)) for row in reversed(rows)]


def _external_order_resource(row: sqlite3.Row) -> ExternalOrderResource:
    values = {column: row[column] for column in _EXTERNAL_ORDER_COLUMNS}
    evidence_refs = json.loads(values.pop("evidence_refs_json"))
    if not isinstance(evidence_refs, list) or not all(isinstance(item, str) for item in evidence_refs):
        raise ValueError("external order evidence_refs_json must be a string list")
    latest = json.loads(row["latest_observation_facts"]) if row["latest_observation_facts"] else {}
    return ExternalOrderResource(
        **values,
        evidence_refs=tuple(evidence_refs),
        observation_sequence=row["observation_sequence"],
        acknowledgement_sequence=row["acknowledgement_sequence"],
        observation_recorded_at_ms=row["observation_recorded_at_ms"],
        acknowledgement_recorded_at_ms=row["acknowledgement_recorded_at_ms"],
        broker_state=latest.get("broker_state"),
        filled_quantity=latest.get("filled_quantity"),
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
    """Every outside order custody tracks: each ``external_orders`` row a manual chain does not hold (#2787).

    The lookups by identity (:func:`external_order`,
    :func:`external_order_by_broker_order_id`) still answer for every row:
    the folds, an acknowledgement and the account's history read the record
    of what was observed.
    """
    rows = conn.execute(
        f"SELECT {_EXTERNAL_ORDER_SELECT} FROM external_orders eo "
        f"WHERE {_OUTSIDE_ORDER_SQL} "
        "ORDER BY eo.observed_at_ms DESC, eo.external_order_id DESC"
    ).fetchall()
    return [_external_order_resource(row) for row in rows]


def filled_outside_order_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """The broker ids of every outside order (:func:`external_orders`) that reported a fill."""
    rows = conn.execute(
        "SELECT eo.broker_order_id FROM external_orders eo "
        f"WHERE eo.filled_avg_price IS NOT NULL AND ABS(eo.qty) >= 1e-9 AND {_OUTSIDE_ORDER_SQL}"
    ).fetchall()
    return frozenset(row[0] for row in rows)


def outside_orders_booked_as_a_bots_sale(conn: sqlite3.Connection) -> frozenset[str]:
    """The broker ids of the outside orders whose fill is booked against a bot (ADR 0076 decision 22).

    Each is left out of :func:`external_orders`. Its executions in the
    account's activities are the booking's own, on the bot, so no money read
    counts them a second time as an outside order's.
    """
    return frozenset(row[0] for row in conn.execute(_BOOKED_AS_A_BOTS_SALE_SQL))


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

    The day-P&L fact reads it because an order the Clerk could not record
    has no journaled fills.
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


def effect_operation_subject(conn: sqlite3.Connection, effect_operation_id: str) -> str | None:
    """The durable custody subject that owns one effect operation.

    The subject-keyed lookup the #2460 price-conflict helpers use: an
    effect's ``strategy_instance_id`` is deliberately ``None`` for manual
    custody, so the subject is the one identity both bot and manual work
    always carry.
    """
    row = conn.execute(
        "SELECT subject_id FROM effect_operations WHERE effect_operation_id = ?",
        (effect_operation_id,),
    ).fetchone()
    return row["subject_id"] if row is not None else None


def order(conn: sqlite3.Connection, order_ref: str) -> OrderResource | None:
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return OrderResource(**dict(row)) if row is not None else None


def order_for_effect_operation(conn: sqlite3.Connection, effect_operation_id: str) -> OrderResource | None:
    """The order originally created by a single-order effect (ENTER).

    For an entry of several legs that is the parent: the one order that was
    sent. Its legs' rows are under the same effect and are never answered
    here (ADR 0076).
    """
    row = conn.execute(
        "SELECT order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms FROM orders o "
        f"WHERE effect_operation_id = ? AND {NOT_A_LEG_SQL}",
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
    return _failed_operation_orders_unproven_at_broker(
        conn,
        operation_kind="ENTER",
        order_role="ENTRY",
        created_kind="ENTER_ACCEPTED",
        symbol_path="$.leg.symbol",
        also="AND e.strategy_instance_id IS NOT NULL ",
        also_params=(),
        symbols=symbols,
        exclude_client_order_ids=exclude_client_order_ids,
        rested_since_ms=rested_since_ms,
        limit=limit,
    )


def failed_exit_orders_unproven_at_broker(
    conn: sqlite3.Connection,
    *,
    symbols: frozenset[str],
    exclude_client_order_ids: frozenset[str],
    rested_since_ms: int,
    failed_since_ms: int,
    limit: int,
) -> list[str]:
    """REDUCING orders of a failed EXIT that were sent and that no broker state ended (#2854).

    The sweep's exact-lookup worklist for a closing order that may have
    filled where the open-order snapshot cannot show it. An EXIT can fail
    while its order's outcome is not known: an operator's Flatten refused by
    the account check as it would be sent again, or a send made again that
    its session no longer allows. The order had at least one
    ``ORDER_SUBMIT_REQUESTED``, so it may be at the broker -- working, or
    already ``filled`` and closed. One never sent cannot be, and one the
    broker itself ended is proven; both are excluded. Narrowed to ``symbols``
    (matched on the immutable ``EXIT_REDUCING_ORDER_CREATED`` facts).

    An order leaves the list for good once an operator discharged its
    strategy's position in its symbol after the order was created
    (:func:`discharge_accounts_for_fill` states the rule). A lookup answers
    with the order's total, and a total found after such a discharge is
    never credited, so no lookup is spent on it.

    Only an EXIT that failed at or after ``failed_since_ms`` is listed, so an
    old one is not asked about for ever. The rotation is
    :func:`terminal_entry_orders_unproven_at_broker`'s: least recently looked
    up first, an order looked up after ``rested_since_ms`` rests, and orders
    already in the open-order snapshot are dropped before ``limit``.
    """
    return _failed_operation_orders_unproven_at_broker(
        conn,
        operation_kind="EXIT",
        order_role="REDUCING",
        created_kind="EXIT_REDUCING_ORDER_CREATED",
        symbol_path="$.symbol",
        also=(
            "AND e.updated_at_ms >= ? "
            "AND EXISTS (SELECT 1 FROM custody_transitions s WHERE s.order_ref = o.order_ref "
            "AND s.transition_kind = 'ORDER_SUBMIT_REQUESTED') "
            "AND NOT EXISTS (SELECT 1 "
            + _residue_discharges_after(
                strategy="e.strategy_instance_id",
                sequence="t.sequence",
                symbol="json_extract(t.facts_json, '$.symbol')",
            )
            + ") "
        ),
        also_params=(failed_since_ms,),
        symbols=symbols,
        exclude_client_order_ids=exclude_client_order_ids,
        rested_since_ms=rested_since_ms,
        limit=limit,
    )


def _residue_discharges_after(*, strategy: str, sequence: str, symbol: str) -> str:
    """The ``FROM ... WHERE`` of one strategy's discharges in a symbol past a ledger point (#2854).

    The one spelling of "this position was discharged since": every
    ``ATTRIBUTED_RESIDUE_DISCHARGED`` of ``strategy`` in ``symbol`` recorded
    after ``sequence``, as ``d``. Each argument is a SQL expression over the
    caller's own rows, or a ``?`` the caller binds -- in the order strategy,
    sequence, symbol. Read from the transitions themselves, so it holds for a
    discharge made before any caller existed.
    """
    return (
        "FROM custody_transitions d "
        f"WHERE d.strategy_instance_id = {strategy} AND d.sequence > {sequence} "
        "AND d.transition_kind = 'ATTRIBUTED_RESIDUE_DISCHARGED' "
        f"AND UPPER(json_extract(d.facts_json, '$.symbol')) = UPPER({symbol})"
    )


#: The summary code a fill is recorded under when its position credit is
#: withheld (#2854): :func:`discharge_accounts_for_fill` states when.
FILL_CREDIT_WITHHELD_BY_DISCHARGE = "FILL_CREDIT_WITHHELD_RESIDUE_DISCHARGED"

#: The most the broker's clock and the Clerk's are taken to differ (#2854).
#: Inside it the Clerk cannot tell whether an execution came before a
#: discharge's broker read or after, and withholding sends nothing. A fold
#: proves each withheld slice against this value again, so it may only grow:
#: lowered, a ledger that holds a slice withheld inside the old value no
#: longer rebuilds.
DISCHARGE_READ_CLOCK_ALLOWANCE_MS = 5_000


class ResidueDischarge(NamedTuple):
    """One recorded discharge: where it sits in the ledger, and the Clerk's clock as its broker reads returned."""

    sequence: int
    broker_observed_at_ms: int


def residue_discharge_since_order_created(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
    strategy_instance_id: str | None,
    symbol: str,
) -> ResidueDischarge | None:
    """The newest discharge of this strategy's position in ``symbol`` since the order was created (#2854).

    ``None`` when the position was not discharged after the order's first
    transition. A manual order has no strategy, and no discharge is ever its
    own.
    """
    if strategy_instance_id is None:
        return None
    row = conn.execute(
        "SELECT d.sequence, json_extract(d.facts_json, '$.broker_observed_at_ms') AS broker_observed_at_ms "
        + _residue_discharges_after(
            strategy="?",
            sequence="(SELECT MIN(c.sequence) FROM custody_transitions c WHERE c.order_ref = ?)",
            symbol="?",
        )
        + " ORDER BY d.sequence DESC LIMIT 1",
        (strategy_instance_id, order_ref, symbol),
    ).fetchone()
    return ResidueDischarge(**dict(row)) if row is not None else None


def discharge_accounts_for_fill(
    discharge: ResidueDischarge, *, transition_kind: str, source_event_at_ms: int | None
) -> bool:
    """Whether ``discharge`` already counted this fill of an order created before it (#2854).

    ``discharge`` is the newest one of the order's strategy in its symbol
    (:func:`residue_discharge_since_order_created`). It was admitted only
    with nothing working on the symbol, at the broker or in the Clerk, and it
    zeroed the strategy's residue after two broker reads proved the broker's
    position equal to the account's attribution less that residue. Whatever
    the order had executed by those reads is in the position they showed, so
    crediting it afterwards counts it twice.

    An execution made after the reads is not in them: its shares are real,
    and they are the strategy's. An exact execution slice carries the
    broker's time on the execution, so it is credited as any other when that
    time is later than the reads returned by more than
    :data:`DISCHARGE_READ_CLOCK_ALLOWANCE_MS`. Every other slice is counted
    as the discharge's.

    A cumulative total carries the order's last update, not an execution
    time, so it proves nothing about when the shares traded and is always
    counted as the discharge's. That is an accepted gap: an order that fills
    after the discharge and is first seen only as a total stays uncredited
    and stands as a position drift for the operator. Nothing is sent.

    Both times are hashed facts, so the append and the fold's proof of it
    make the same comparison here.
    """
    return not (
        transition_kind == "EXECUTION_SLICE_FILLED"
        and source_event_at_ms is not None
        and source_event_at_ms > discharge.broker_observed_at_ms + DISCHARGE_READ_CLOCK_ALLOWANCE_MS
    )


def fills_written_off_by_residue_discharge(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
    strategy_instance_id: str | None,
    symbol: str,
) -> bool:
    """Whether a discharge wrote off every fill the order has on record (#2854).

    Read from what each fill's own transition recorded, never decided again
    from times: a fill is withheld when it carries
    :data:`FILL_CREDIT_WITHHELD_BY_DISCHARGE`, and credited otherwise. The
    order's fills are written off when none was credited, or when the
    strategy's position in ``symbol`` was discharged after the last one that
    was. A fill credited after the newest discharge is a position the
    strategy holds.

    The first read is the order's own transitions. The second walks the
    strategy's from the last credited fill forward and stops at the first
    discharge, so neither grows with the ledger once the order is written
    off: the reconciliation pass asks this of every such order each time.
    """
    row = conn.execute(
        "SELECT MAX(CASE WHEN summary_code = ? THEN sequence END) AS withheld, "
        "MAX(CASE WHEN summary_code != ? THEN sequence END) AS credited "
        "FROM custody_transitions WHERE order_ref = ? "
        "AND transition_kind IN ('EXECUTION_SLICE_FILLED', 'ORDER_FILL_OBSERVED')",
        (FILL_CREDIT_WITHHELD_BY_DISCHARGE, FILL_CREDIT_WITHHELD_BY_DISCHARGE, order_ref),
    ).fetchone()
    if row["credited"] is None:
        return row["withheld"] is not None
    if strategy_instance_id is None:
        return False
    discharged_since = conn.execute(
        "SELECT 1 "
        + _residue_discharges_after(strategy="?", sequence="?", symbol="?")
        + " ORDER BY d.sequence ASC LIMIT 1",
        (strategy_instance_id, row["credited"], symbol),
    ).fetchone()
    return discharged_since is not None


def _failed_operation_orders_unproven_at_broker(
    conn: sqlite3.Connection,
    *,
    operation_kind: str,
    order_role: str,
    created_kind: str,
    symbol_path: str,
    also: str,
    also_params: tuple[object, ...],
    symbols: frozenset[str],
    exclude_client_order_ids: frozenset[str],
    rested_since_ms: int,
    limit: int,
) -> list[str]:
    """The exact-lookup worklist both kinds of failed operation share (#2348, #2854).

    Orders of role ``order_role`` whose ``operation_kind`` operation is
    ``failed``/``rejected`` and that no broker state ended, in ``symbols``.
    The symbol is read at ``symbol_path`` in the facts of the order's own
    ``created_kind`` transition, which is never rewritten. ``also`` narrows
    the list further, over ``e`` (the operation), ``o`` (the order) and ``t``
    (that transition), with ``also_params`` bound to it. The rotation is the
    one :func:`terminal_entry_orders_unproven_at_broker` describes.

    The read starts from the operations and reaches each order, and then its
    transition, through their indexes. The joins are ``CROSS JOIN`` so that
    SQLite keeps that order and never scans the ledger for the one transition
    kind: this runs on every pass that reads a symbol as drifted. A bound on
    the operation in ``also`` -- the failed EXIT's day -- is walked off
    ``ix_effect_operations_updated_at``.
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
        "FROM effect_operations e "
        "CROSS JOIN orders o ON o.effect_operation_id = e.effect_operation_id "
        "CROSS JOIN custody_transitions t ON t.order_ref = o.order_ref "
        "AND t.transition_kind = ? "
        "WHERE o.role = ? AND e.kind = ? AND e.state IN ('failed', 'rejected') "
        f"{also}"
        "AND (o.broker_state IS NULL OR LOWER(o.broker_state) NOT IN "
        f"({_TERMINAL_BROKER_STATES_SQL})) "
        f"AND UPPER(json_extract(t.facts_json, ?)) IN ({symbol_marks}) "
        f"{exclude_clause}"
        ") WHERE last_lookup_ms IS NULL OR last_lookup_ms <= ? "
        "ORDER BY last_lookup_ms IS NOT NULL, last_lookup_ms ASC, "
        "updated_at_ms DESC, order_ref DESC LIMIT ?",
        (
            created_kind,
            order_role,
            operation_kind,
            *also_params,
            symbol_path,
            *sorted(symbols),
            *excluded,
            rested_since_ms,
            limit,
        ),
    ).fetchall()
    return [row["order_ref"] for row in rows]


def all_order_refs(conn: sqlite3.Connection) -> frozenset[str]:
    """Every immutable broker identity captured by this authority."""
    rows = conn.execute("SELECT order_ref FROM orders").fetchall()
    return frozenset(row["order_ref"] for row in rows)


def manual_chain_order_ref(conn: sqlite3.Connection, broker_order_id: str) -> str | None:
    """The manual leg whose Alpaca replacement chain holds this broker order id (#2656).

    A member is the leg's current head (its ``orders`` row's broker id) or
    either end of any durable ``MANUAL_ORDER_REPLACED`` link, whatever state
    the leg's effect is in -- a late frame about a chain member still belongs
    to its leg. Only manual legs have chains: the head probe goes through
    ``ux_orders_broker_order_id`` and the link probe through the transitions'
    ``order_ref`` index, scoped to the manual legs.
    """
    row = conn.execute(f"{_manual_chain_order_ref_sql('?1')} LIMIT 1", (broker_order_id,)).fetchone()
    return row["order_ref"] if row is not None else None


def manual_chain_member_ids(conn: sqlite3.Connection, order_ref: str) -> frozenset[str]:
    """Every broker order id in one manual leg's Alpaca replacement chain (#2656, #2686).

    The forward twin of :func:`manual_chain_order_ref`, with the same
    members: the leg's current head and either end of each durable
    ``MANUAL_ORDER_REPLACED`` link. Empty for a bot order and for a leg not
    acknowledged yet.
    """
    rows = conn.execute(
        "SELECT o.broker_order_id AS member FROM orders o "
        "JOIN manual_order_legs l ON l.order_ref = o.order_ref "
        "WHERE o.order_ref = ?1 AND o.broker_order_id IS NOT NULL "
        "UNION "
        "SELECT t.broker_order_id FROM custody_transitions t "
        "WHERE t.order_ref = ?1 AND t.transition_kind = 'MANUAL_ORDER_REPLACED' "
        "AND t.order_ref IN (SELECT order_ref FROM manual_order_legs WHERE order_ref IS NOT NULL) "
        "UNION "
        "SELECT json_extract(t.facts_json, '$.replaces') FROM custody_transitions t "
        "WHERE t.order_ref = ?1 AND t.transition_kind = 'MANUAL_ORDER_REPLACED' "
        "AND t.order_ref IN (SELECT order_ref FROM manual_order_legs WHERE order_ref IS NOT NULL)",
        (order_ref,),
    ).fetchall()
    return frozenset(row["member"] for row in rows if row["member"])


def entry_orders_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> list[OrderResource]:
    """Every entry order whose immutable origin belongs to one strategy, least recently updated first.

    Ordered by ``updated_at_ms``, then ``order_ref`` -- not by submission: the
    last is the entry updated last, which every reduction is keyed on
    (``exit.newest_reducible_entry``).
    """
    rows = conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, "
        "o.role, o.broker_state, o.submitted_at_ms, o.updated_at_ms FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.role = 'ENTRY' AND e.strategy_instance_id = ? "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        (strategy_instance_id,),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


_ORDER_FIELDS = (
    "order_ref", "effect_operation_id", "client_order_id", "broker_order_id", "role", "broker_state",
    "submitted_at_ms", "updated_at_ms",
)
_ENTRY_ORDER_COLUMNS = ", ".join(f"o.{name}" for name in _ORDER_FIELDS)

_ENTRY_SHOWN_WORKING = f"LOWER(o.broker_state) IN ({', '.join('?' for _ in CANCELLABLE_ENTRY_BROKER_STATES)})"
"""The broker's last word on the order is a state it can still fill in
(:data:`CANCELLABLE_ENTRY_BROKER_STATES`, bound in sorted order)."""

_STRATEGY_ENTRY_NO_EXIT_OWNS = (
    "o.role = 'ENTRY' AND e.strategy_instance_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM operation_order_links l "
    "JOIN effect_operations x ON x.effect_operation_id = l.effect_operation_id "
    "WHERE l.order_ref = o.order_ref AND x.kind = 'EXIT' "
    "AND x.state NOT IN ('succeeded','failed','rejected'))"
)
"""A strategy's ENTRY that no nonterminal EXIT links (``o`` its order, ``e`` its effect).

With :data:`_ENTRY_SHOWN_WORKING` this is the one definition of "an entry
order that must be cancelled" once its reason has come. Two steps read it and
differ only in the reason: a run that is no longer ACTIVE
(:func:`entry_orders_owed_a_cancel`) and a wait that has run out
(:func:`option_entries_with_a_deadline`).

Manual-custody orders belong to no strategy. An entry a nonterminal EXIT
links is that EXIT's to cancel (the predicate of :func:`active_exit_for_order`),
and a second canceller would race it. **The ENTER's own state is deliberately
not asked**: an ENTER voided as never sent whose order the broker later shows
working (#2342) is as live as any other, and must be cancelled all the same."""


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
    rows = conn.execute(
        f"SELECT {_ENTRY_ORDER_COLUMNS} FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"WHERE {_STRATEGY_ENTRY_NO_EXIT_OWNS} AND {_ENTRY_SHOWN_WORKING} "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.run_id = e.run_id "
        "AND r.strategy_instance_id = e.strategy_instance_id AND r.state = 'ACTIVE') "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC",
        tuple(sorted(CANCELLABLE_ENTRY_BROKER_STATES)),
    ).fetchall()
    return [OrderResource(**dict(row)) for row in rows]


class EntryDeadline(NamedTuple):
    """One option entry the Clerk may still have to cancel, and the two times its acceptance keeps."""

    order: OrderResource
    # When the Clerk cancels what has not filled, and its own clock when it admitted the entry.
    cancel_at_ms: int
    admitted_at_ms: int


_ENTRY_ACCEPTANCE_TIME = "CAST(json_extract(accepted.facts_json, '$.option.{name}') AS INTEGER)"

# Read outward from the orders the books bind to a contract (``CROSS JOIN``
# keeps SQLite to the written order): books that never traded an option answer
# from an empty table, and books that did read one acceptance for each bound
# order, by its index. The custody log itself is never scanned.
# The orders an option entry's deadline is read from: the ones the books bind to one contract, and,
# for an entry of several legs (ADR 0076), the parents the books keep legs for. A parent is the one
# order that was sent, so it is what is listed and cancelled. A leg's row is never listed by either:
# its role is not ENTRY.
_ONE_CONTRACT_ORDERS = "order_contracts bound CROSS JOIN orders o ON o.order_ref = bound.order_ref"
_MULTI_LEG_PARENT_ORDERS = (
    "(SELECT DISTINCT parent_order_ref FROM option_order_legs) parent "
    "CROSS JOIN orders o ON o.order_ref = parent.parent_order_ref"
)


def _books_keep_legs(conn: sqlite3.Connection) -> bool:
    """Whether these books ever took an option order of several legs: one probe of a table that is then empty.

    Asked before a read of the parents, so books that never took one pay a
    few steps for it and not a second query.
    """
    return conn.execute("SELECT 1 FROM option_order_legs LIMIT 1").fetchone() is not None


def _entries_with_a_deadline_sql(orders: str) -> str:
    return (
        f"SELECT {_ENTRY_ORDER_COLUMNS}, {_ENTRY_ACCEPTANCE_TIME.format(name='cancel_at_ms')} AS cancel_at_ms, "
        f"{_ENTRY_ACCEPTANCE_TIME.format(name='admitted_at_ms')} AS admitted_at_ms "
        f"FROM {orders} "
        "CROSS JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "CROSS JOIN custody_transitions accepted ON accepted.order_ref = o.order_ref "
        "AND accepted.transition_kind = 'ENTER_ACCEPTED' "
        f"WHERE {_STRATEGY_ENTRY_NO_EXIT_OWNS} "
        f"AND ({_ENTRY_SHOWN_WORKING} OR (o.broker_state IS NULL "
        f"AND e.state IN ({', '.join('?' for _ in NONTERMINAL_EFFECT_STATES)}))) "
        f"AND {_ENTRY_ACCEPTANCE_TIME.format(name='cancel_at_ms')} IS NOT NULL "
        "ORDER BY cancel_at_ms ASC, o.order_ref ASC"
    )


OPTION_ENTRIES_WITH_A_DEADLINE_SQL = _entries_with_a_deadline_sql(_ONE_CONTRACT_ORDERS)
MULTI_LEG_ENTRIES_WITH_A_DEADLINE_SQL = _entries_with_a_deadline_sql(_MULTI_LEG_PARENT_ORDERS)
OPTION_ENTRIES_WITH_A_DEADLINE_PARAMS = (*sorted(CANCELLABLE_ENTRY_BROKER_STATES), *sorted(NONTERMINAL_EFFECT_STATES))


def option_entries_with_a_deadline(conn: sqlite3.Connection) -> list[EntryDeadline]:
    """Every option entry that may still have to be cancelled at its deadline, the earliest deadline first (#2913).

    The deadline keeper's worklist, in one read. It is
    :func:`entry_orders_owed_a_cancel`'s predicate with a deadline in place
    of a stopped run: a strategy's ENTRY the books show working, that no EXIT
    owns, whatever its ENTER's state. An entry in shares, and an option entry
    from before entries could rest, has no deadline and is never listed.

    One more case is listed that the stopped-run step leaves to the pass's
    own recovery: an entry the broker has not answered about yet, under an
    ENTER that has not ended. Nothing can be cancelled without the broker's
    id for it, so its turn at the deadline is a lookup, which finds the order
    or, past the grace, proves it never arrived.
    """
    rows = conn.execute(OPTION_ENTRIES_WITH_A_DEADLINE_SQL, OPTION_ENTRIES_WITH_A_DEADLINE_PARAMS).fetchall()
    if _books_keep_legs(conn):
        # An entry of several legs: its parent, by the same rule.
        rows += conn.execute(MULTI_LEG_ENTRIES_WITH_A_DEADLINE_SQL, OPTION_ENTRIES_WITH_A_DEADLINE_PARAMS).fetchall()
    return sorted(
        (
            EntryDeadline(
                order=OrderResource(**{name: row[name] for name in _ORDER_FIELDS}),
                cancel_at_ms=row["cancel_at_ms"],
                admitted_at_ms=row["admitted_at_ms"],
            )
            for row in rows
        ),
        key=lambda listed: (listed.cancel_at_ms, listed.order.order_ref),
    )


def working_option_entry_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> OrderResource | None:
    """An option entry of this strategy that the books show working at the broker, or ``None`` (#2913).

    Whatever its ENTER's state: the order of an ENTER voided as never sent,
    which the broker later showed, can still fill. Read from the orders the
    books bind to a contract, so a strategy that trades shares is answered
    from an empty table and its orders are never asked about.
    """
    params = (strategy_instance_id, *sorted(CANCELLABLE_ENTRY_BROKER_STATES))
    where = (
        "CROSS JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        f"WHERE o.role = 'ENTRY' AND e.strategy_instance_id = ? AND {_ENTRY_SHOWN_WORKING} "
        "ORDER BY o.updated_at_ms ASC, o.order_ref ASC LIMIT 1"
    )
    row = conn.execute(
        f"SELECT {_ENTRY_ORDER_COLUMNS} FROM {_ONE_CONTRACT_ORDERS} {where}",
        params,
    ).fetchone()
    # An entry of several legs is bound to no contract: its parent is found from its legs (ADR 0076).
    parent = (
        conn.execute(f"SELECT {_ENTRY_ORDER_COLUMNS} FROM {_MULTI_LEG_PARENT_ORDERS} {where}", params).fetchone()
        if _books_keep_legs(conn)
        else None
    )
    found = min(
        (candidate for candidate in (row, parent) if candidate is not None),
        key=lambda candidate: (candidate["updated_at_ms"], candidate["order_ref"]),
        default=None,
    )
    return OrderResource(**dict(found)) if found is not None else None


class HeldOptionContract(NamedTuple):
    """One option contract a bot holds, and how many: signed, a bought position above zero."""

    strategy_instance_id: str
    symbol: str
    quantity: float


# Read outward from what is held: the few position rows that are not zero, each
# asked of the contracts the books have recorded. Books that never traded an
# option ask an empty table, and no row of the custody log is read.
HELD_OPTION_CONTRACTS_SQL = (
    "SELECT p.strategy_instance_id, contract.symbol AS symbol, p.attributed_qty "
    "FROM positions p "
    "CROSS JOIN option_contracts contract ON contract.symbol = UPPER(p.symbol) "
    "WHERE p.strategy_instance_id IS NOT NULL AND p.attributed_qty != 0 "
    "ORDER BY p.strategy_instance_id ASC, contract.symbol ASC"
)


def held_option_contracts(conn: sqlite3.Connection) -> list[HeldOptionContract]:
    """Every option contract any bot holds, account-wide, in one read (#2913).

    The worklist of the scheduled closes. A close is owed by what is held,
    so it starts from the positions, never from a run or an order's state.
    A position in shares is not listed: its symbol is no recorded contract.
    """
    return [
        HeldOptionContract(row["strategy_instance_id"], row["symbol"], row["attributed_qty"])
        for row in conn.execute(HELD_OPTION_CONTRACTS_SQL).fetchall()
    ]


def strategies_under_active_uncertainty(conn: sqlite3.Connection, reason_code: str) -> list[str]:
    """Every bot with an open episode of ``reason_code``, read by the reason's own index (#2913).

    For a step that owns one bot-scoped notice from its raising to its end
    and must find the ones no longer owed. Books that never raised the
    reason answer from the index alone.
    """
    rows = conn.execute(
        "SELECT strategy_instance_id FROM uncertainties "
        "WHERE reason_code = ? AND resolved_at_ms IS NULL AND strategy_instance_id IS NOT NULL "
        "ORDER BY strategy_instance_id ASC",
        (reason_code,),
    ).fetchall()
    return [row["strategy_instance_id"] for row in rows]


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


_ACTIVE_EXIT_PREDICATE = "e.kind = 'EXIT' AND e.state NOT IN ('succeeded','failed','rejected')"
"""An exit in progress: the one definition behind every "is an EXIT working?" read.

The stuck-EXIT watchdog re-drives only an entry no such EXIT owns
(:func:`active_exit_for_order`), and an ``EXIT_NOT_FLAT`` notice stops
promising its next attempt while the strategy has one
(:func:`strategies_with_active_exit`, #2440 review) — one predicate, so the
two never disagree about whether an exit is in flight."""


def active_exit_for_order(conn: sqlite3.Connection, order_ref: str) -> EffectOperationResource | None:
    """The nonterminal EXIT currently linked to an entry, if any."""
    row = conn.execute(
        "SELECT e.effect_operation_id, e.authority_generation, e.idempotency_key, e.command_id, "
        "e.strategy_instance_id, e.run_id, e.kind, e.state, e.custody_owner, e.created_at_ms, "
        "e.updated_at_ms, e.terminal_receipt_id FROM effect_operations e "
        "JOIN operation_order_links l ON l.effect_operation_id = e.effect_operation_id "
        f"WHERE l.order_ref = ? AND {_ACTIVE_EXIT_PREDICATE} "
        "ORDER BY e.created_at_ms DESC LIMIT 1",
        (order_ref,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def active_exit_for_strategy(conn: sqlite3.Connection, strategy_instance_id: str) -> EffectOperationResource | None:
    """The strategy's live EXIT fence against concurrently admitted ENTERs."""
    row = conn.execute(
        "SELECT e.effect_operation_id, e.authority_generation, e.idempotency_key, e.command_id, "
        "e.strategy_instance_id, e.run_id, e.kind, e.state, e.custody_owner, e.created_at_ms, "
        "e.updated_at_ms, e.terminal_receipt_id FROM effect_operations e "
        f"WHERE e.strategy_instance_id = ? AND {_ACTIVE_EXIT_PREDICATE} "
        "ORDER BY e.created_at_ms DESC LIMIT 1",
        (strategy_instance_id,),
    ).fetchone()
    return EffectOperationResource(**dict(row)) if row is not None else None


def strategies_with_active_exit(
    conn: sqlite3.Connection, strategy_instance_ids: Collection[str]
) -> frozenset[str]:
    """Which of these strategies has an exit in progress — :func:`active_exit_for_strategy`, in one read.

    While a strategy has one, the stuck-EXIT watchdog sends it no re-drive:
    that EXIT owns the whole symbol's reduction (``exit.newest_reducible_entry``)
    and is Clerk work in flight on it (``clerk_work_in_flight``). So
    its ``EXIT_NOT_FLAT`` notice says an exit is working instead of promising
    a time (#2440 review). Asked only about the strategies such a notice
    names, so the usual answer — none — costs no query.
    """
    if not strategy_instance_ids:
        return frozenset()
    ids = sorted(set(strategy_instance_ids))
    rows = conn.execute(
        "SELECT DISTINCT e.strategy_instance_id FROM effect_operations e "
        f"WHERE e.strategy_instance_id IN ({', '.join('?' for _ in ids)}) "
        f"AND {_ACTIVE_EXIT_PREDICATE}",
        ids,
    ).fetchall()
    return frozenset(row["strategy_instance_id"] for row in rows)


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

    A working manual leg stays on it whatever its order's broker state: a
    filled leg ends only on exact executions, which the sweep reads from
    account activity until they cover it (#2686). It is named here by kind,
    like an EXIT and a CANCEL, because a manual order has no
    ``operation_order_links`` row to read a broker state through.

    One order of several legs (ADR 0076) is listed through its parent, the
    one order that was sent. No fill ever lands on a parent, so a filled
    parent is not held to "filled, with a fill on the books": each of its
    legs is (:data:`_A_LEG_NOT_PROVEN_SQL`). A leg's own row is never
    listed: it has no link, and nothing is asked of a broker about it.
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
        "AND (e.state IN ('accepted','unknown') OR e.kind IN ('EXIT','CANCEL','MANUAL_ORDER') "
        "OR o.broker_state IS NULL OR lower(o.broker_state) NOT IN "
        f"({_TERMINAL_BROKER_STATES_SQL}) "
        "OR (lower(o.broker_state) = 'filled' AND NOT EXISTS ("
        "SELECT 1 FROM fills f WHERE f.order_ref = o.order_ref "
        f"AND {_EFFECTIVE_FILL_PREDICATE}) AND NOT {MULTI_LEG_PARENT_SQL}) "
        f"OR {_fills_short_of_broker_cumulative_sql('o.order_ref')} "
        f"OR {_A_LEG_NOT_PROVEN_SQL}) "
        "ORDER BY e.created_at_ms ASC",
        (*params, FILL_QTY_EPSILON, FILL_QTY_EPSILON),
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
        # An order bound to an option contract trades that contract, not its bot's symbol (ADR 0076 decision 13).
        f"COALESCE({ORDER_CONTRACT_SYMBOL_SQL}, s.symbol, "
        "json_extract(manual_acceptance.facts_json, '$.leg.symbol')) AS symbol "
        "FROM fills f JOIN orders o ON o.order_ref = f.order_ref "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "LEFT JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id "
        f"{ORDER_CONTRACT_JOINS_SQL.format(order='o')} "
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


_REPORTED_FILLED_QUANTITY_SQL = "CAST(json_extract(t.facts_json, '$.reported_filled_quantity') AS REAL)"


def _latest_ack_reported_filled_quantity_sql(order_ref_sql: str) -> str:
    """SQL scalar: the cumulative the order's literal LATEST acknowledgement reported.

    Only for the acknowledgement fold's change check (#2305); the shortfall
    reads the governing acknowledgement (#2385). ``NULL`` when that row
    reported no fill.
    """
    return (
        "(SELECT " + _REPORTED_FILLED_QUANTITY_SQL + " "
        "FROM custody_transitions t WHERE t.order_ref = " + order_ref_sql + " "
        "AND t.transition_kind = 'ORDER_SUBMIT_ACKED' ORDER BY t.sequence DESC LIMIT 1)"
    )


#: SQL ``IN`` list of :data:`FINAL_CUMULATIVE_BROKER_STATES` (code-owned literals).
_FINAL_BROKER_STATES_SQL = ", ".join(f"'{state}'" for state in sorted(FINAL_CUMULATIVE_BROKER_STATES))


def _governing_acknowledgement_sql(order_ref_sql: str, columns: str) -> str:
    """SQL query: ``columns`` of the order's governing acknowledgement (#2346, #2385).

    The one definition of which acknowledgement carries the broker's word on
    an order's total, read by the shortfall (#2305) and the order-total
    coverage proof (#2346). An acknowledgement whose OWN state is final
    (:data:`FINAL_CUMULATIVE_BROKER_STATES`) always outranks a working-state
    one: a REST fold records its acknowledgement even when stale
    (``append_stale_ack``), so a pre-cancel answer can land after the
    ``canceled`` frame, and its pre-terminal cumulative would read a short
    order complete (#2385). Only while no acknowledgement is final does the
    latest one by sequence govern.

    Among final acknowledgements the latest by sequence wins -- never the
    largest total, and not the newest broker source time: a lower final
    total from the exact lookup (an execution correction) must close the
    gap, or the order stays short and the EXIT re-appends rows on every
    sweep (#2305 review Major A). Ordering by source time reopens that
    whenever the lookup's ``updated_at`` sorts before the frame's. The cost
    is that a stale final answer folded last governs (#2385 review; pinned
    as a strict xfail). ``columns`` read the row as ``t``.
    """
    return (
        "SELECT " + columns + " FROM custody_transitions t WHERE t.order_ref = " + order_ref_sql + " "
        "AND t.transition_kind = 'ORDER_SUBMIT_ACKED' "
        "ORDER BY (lower(t.broker_state) IN (" + _FINAL_BROKER_STATES_SQL + ")) DESC, "
        "t.sequence DESC LIMIT 1"
    )


def _fills_short_of_broker_cumulative_sql(order_ref_sql: str) -> str:
    """SQL predicate: the governing acknowledgement's cumulative exceeds the effective fills.

    ``NULL`` (never short) when the governing acknowledgement reported no
    fill. ``order_ref_sql`` appears twice; binds :data:`FILL_QTY_EPSILON`
    last. The fill sum is covered by ``ix_fills_order_ref`` and the
    effective-fill predicate by ``ix_fills_superseded_execution_ref``
    (schema v15).
    """
    return (
        "((" + _governing_acknowledgement_sql(order_ref_sql, _REPORTED_FILLED_QUANTITY_SQL) + ") - "
        "(SELECT COALESCE(SUM(f.qty), 0) FROM fills f WHERE f.order_ref = " + order_ref_sql + " "
        "AND " + _EFFECTIVE_FILL_PREDICATE + ") >= ?)"
    )


# Whether a leg of the order of several legs ``o`` still wants fresh evidence (ADR 0076). The parent
# of such an order has no fill of its own: its fills are on its legs' rows. So "filled, and the
# books hold no fill" is asked of each leg, by the rule a single order is held to: a leg the broker
# has not ended, a leg it says filled whose fill the books do not hold, or a leg whose fills fall
# short of the broker's own count. False for every order with no legs. Binds
# :data:`FILL_QTY_EPSILON` last.
_A_LEG_NOT_PROVEN_SQL = (
    "EXISTS (SELECT 1 FROM option_order_legs leg JOIN orders leg_o ON leg_o.order_ref = leg.order_ref "
    "WHERE leg.parent_order_ref = o.order_ref AND ("
    f"leg_o.broker_state IS NULL OR lower(leg_o.broker_state) NOT IN ({_TERMINAL_BROKER_STATES_SQL}) "
    "OR (lower(leg_o.broker_state) = 'filled' AND NOT EXISTS ("
    f"SELECT 1 FROM fills f WHERE f.order_ref = leg_o.order_ref AND {_EFFECTIVE_FILL_PREDICATE})) "
    f"OR {_fills_short_of_broker_cumulative_sql('leg_o.order_ref')}))"
)


def latest_reported_filled_quantity(conn: sqlite3.Connection, order_ref: str) -> float | None:
    """The cumulative filled quantity the order's latest acknowledgement reported, if any."""
    row = conn.execute(
        "SELECT " + _latest_ack_reported_filled_quantity_sql("?") + " AS reported", (order_ref,)
    ).fetchone()
    return float(row["reported"]) if row["reported"] is not None else None


def governing_acknowledgement(
    conn: sqlite3.Connection, order_ref: str
) -> tuple[str | None, float | None] | None:
    """The state and reported cumulative of the order's governing acknowledgement.

    Both come from the SAME row (:func:`_governing_acknowledgement_sql`): the
    ``orders`` fold keeps a terminal state once seen, so pairing it with
    another acknowledgement's total could claim a final total the broker
    never reported (#2346). ``None`` when the order has no acknowledgement.
    """
    row = conn.execute(
        _governing_acknowledgement_sql(
            "?", "t.broker_state AS broker_state, " + _REPORTED_FILLED_QUANTITY_SQL + " AS reported"
        ),
        (order_ref,),
    ).fetchone()
    if row is None:
        return None
    return row["broker_state"], (float(row["reported"]) if row["reported"] is not None else None)


def order_fills_short_of_broker_cumulative(conn: sqlite3.Connection, order_ref: str) -> bool:
    """Whether the order's effective fills fall short of the broker's governing cumulative (#2305).

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
    Validated against: ``test_one_exact_auto_supersedes_many_cumulative_recovery_rows``.
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


def attributed_positions_by_subject(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Every custody subject's attributed quantity per symbol, un-netted (#2344).

    Keyed ``(subject_id, symbol)``, the ``positions`` primary key, so a
    subject long 10 and another short 10 stay two rows of exposure. Use this,
    not ``attributed_positions_by_symbol``, to ask whether custody holds nothing.
    """
    rows = conn.execute("SELECT subject_id, symbol, attributed_qty FROM positions").fetchall()
    return {(row["subject_id"], row["symbol"]): row["attributed_qty"] for row in rows}


def option_contracts_held_by_others(
    conn: sqlite3.Connection, *, strategy_instance_id: str, symbols: Sequence[str]
) -> list[tuple[str, str | None, float]]:
    """What the account holds, outside one bot, in the option contracts ``symbols``: (symbol, holder, signed quantity).

    Every holding that is not zero of every other custody subject: another
    bot's, or the owner's own orders (the holder is then ``None``). What an
    option entry is held against before it is taken, so no leg opens a
    position that would cancel another's at the broker (ADR 0076). One read,
    by the contracts asked about.
    """
    if not symbols:
        return []
    rows = conn.execute(
        "SELECT UPPER(symbol) AS symbol, strategy_instance_id, attributed_qty FROM positions "
        f"WHERE UPPER(symbol) IN ({', '.join('?' for _ in symbols)}) AND attributed_qty != 0 "
        "AND (strategy_instance_id IS NULL OR strategy_instance_id != ?) "
        "ORDER BY symbol, subject_id",
        (*(symbol.upper() for symbol in symbols), strategy_instance_id),
    ).fetchall()
    return [(row["symbol"], row["strategy_instance_id"], row["attributed_qty"]) for row in rows]


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
    Reference: none external.
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


def active_uncertainty_for_subject(
    conn: sqlite3.Connection, *, scope: str, reason_code: str, subject_id: str
) -> dict | None:
    """The active uncertainty for one ``(scope, reason_code, subject_id)``.

    The subject-keyed counterpart of :func:`active_uncertainty` for causes
    whose owning effect may carry no strategy instance (manual custody,
    #2460 review): the episode's durable key is the custody subject, and the
    ``strategy_instance_id`` column is only a bot compatibility projection.
    """
    row = conn.execute(
        f"SELECT {_UNCERTAINTY_COLUMNS} FROM uncertainties "
        "WHERE scope = ? AND reason_code = ? AND subject_id IS ? "
        "AND resolved_at_ms IS NULL ORDER BY observed_at_ms DESC LIMIT 1",
        (scope, reason_code, subject_id),
    ).fetchone()
    return dict(row) if row is not None else None


def active_execution_price_conflicts(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str, ExecutionPriceConflictCause], ...]:
    """Every active ``EXECUTION_PRICE_CONFLICT`` episode as ``(uncertainty_id,
    subject_id, cause)`` (#2460).

    The sweep re-derivation's worklist: it walks recorded evidence only, so
    an episode whose order stopped being re-read (a terminal order's totals
    are never re-folded) still gets its correction-explained exit. Keyed by
    the custody subject -- the episode's durable owner, which manual custody
    shares with bots (#2460 review) -- and the sweep's compare-and-clear
    binds the returned ``uncertainty_id`` and ``cause`` as the expected
    episode identity. Rows whose cause cannot be decoded are skipped -- an
    unreadable cause is not a reason to wedge every other episode's
    re-derivation.
    """
    rows = conn.execute(
        "SELECT uncertainty_id, subject_id, facts_json FROM uncertainties "
        "WHERE reason_code = ? AND resolved_at_ms IS NULL "
        "ORDER BY observed_at_ms ASC, uncertainty_id ASC",
        (EXECUTION_PRICE_CONFLICT_REASON_CODE,),
    ).fetchall()
    episodes: list[tuple[str, str, ExecutionPriceConflictCause]] = []
    for row in rows:
        try:
            raised = json.loads(row["facts_json"])
            cause = ExecutionPriceConflictCause.from_mapping(raised.get("cause_facts"))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if row["subject_id"] is None:
            continue
        episodes.append((row["uncertainty_id"], row["subject_id"], cause))
    return tuple(episodes)


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
