"""Every bot one custody database holds, run by run (Bot history, #2574).

One read over one query-only snapshot of a custody file: each registered
bot's identity, its runs (start, stop), its effective fills and its orders
counted per run, its budget, and its whole-life result and fees. It is a
projection of facts the Clerk already keeps; it adds no FIFO, no fee rule
and no second store:

* **Transactions** are the canonical effective fills after broker
  corrections (``effective_fill_records``) -- the same population, and so
  the same count, as ``project_bot_results``' ``trade_count``. A fill
  belongs to the run its order's effect operation names
  (``orders -> effect_operations.run_id``).
* **Orders** are the bot's own order rows, by their immutable provenance
  (``orders.effect_operation_id``), counted by the broker's last reported
  state: *sent* once the broker has reported the order at all, then
  *filled*, *cancelled* (cancelled or expired) or *rejected*. An order the
  broker never acknowledged was never sent.
* **Result and fees** are per bot, never per run: ``project_bot_results``
  and the canonical fee reconciler's per-subject total. An old bot with
  several runs (the retired Resume) has one result across all of them.

A fill or order whose effect operation names no run (work done for a bot
after its run ended, such as a later flatten) counts toward the bot, never
toward a run, so a run's counts are exact and the bot's totals stay equal to
its ``trade_count``.

A run was **flattened** when the owner's own flatten -- the Clerk's safe
flatten or the panel's flatten-and-stop, each a reducing EXIT under its own
decision namespace -- was accepted at or after the run's stop and before the
bot's next run began, **and sold**: the broker reports that EXIT's own order
filled and the Clerk holds that order's effective fill (the same fills the
transaction counts read). An accepted flatten that sold nothing -- refused by
the broker, or a limit that expired unsent -- is not a flatten. Nor is a
partial one: a flatten whose order sold part of the position and was then
cancelled or expired, or is still working, leaves the bot holding the rest,
so the run reads as stopped, not flattened -- unless the stuck-EXIT
watchdog's re-drives of that flatten then sold the rest (#2615). A re-drive
carries on its ``EXIT_NOT_FLAT`` episode (``redrive_episode_token``), and the
episode's raise and refreshes in the transition log name every EXIT it was
about -- the flatten, then each re-drive that failed -- so a re-drive that
sold for an owner's flatten, however many tries in, finishes that flatten,
at the flatten's own acceptance.

A custody file no running Clerk has opened since a schema upgrade -- a Live
account's retired Shadow world, a Dry Run from before one -- keeps its old
schema. It is read through the Clerk's own chained migrations applied to a
private in-memory copy (``read_custody_history``), so the file is never
written; one no migration reaches (a newer build's, or one below the v13
floor) is ``CustodySchemaUnreadable`` (#2615).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.budget_projection import (
    BotResult,
    BudgetFees,
    RevisionMemo,
    bot_results_from_fills,
    bots_holding_money,
    read_at_revision,
)
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    OWNER_FLATTEN_DECISION_PREFIXES,
    redrive_episode_token,
    redriven_episode_token,
)
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyRaisedFacts
from app.broker.alpaca.clerk.sqlite.models import BotConfigResource
from app.broker.alpaca.clerk.sqlite.runtime import decision_id_from_durable
from app.broker.alpaca.clerk.sqlite.schema import SCHEMA_VERSION, migrate_schema
from app.broker.alpaca.clerk.sqlite.timeline_query import uncertainty_sequence
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_NOT_FLAT_REASON_CODE

logger = logging.getLogger(__name__)

#: The broker's order states each order count reads. Lower-cased: Alpaca's
#: own spelling ("canceled") is the stored one.
_FILLED_STATES = ("filled",)
_CANCELLED_STATES = ("canceled", "expired")
_REJECTED_STATES = ("rejected",)


def _reported_filled(broker_state: str | None) -> bool:
    """Whether the broker reports an order filled -- whole, not in part."""
    return broker_state is not None and broker_state.lower() in _FILLED_STATES


@dataclass(frozen=True)
class OrderCounts:
    """One bot's (or one run's) orders by the broker's last reported state."""

    sent: int = 0
    filled: int = 0
    cancelled: int = 0
    rejected: int = 0

    def plus(self, broker_state: str | None) -> OrderCounts:
        """These counts with one more order, in its reported state."""
        if broker_state is None:
            return self
        state = broker_state.lower()
        return OrderCounts(
            sent=self.sent + 1,
            filled=self.filled + int(_reported_filled(state)),
            cancelled=self.cancelled + int(state in _CANCELLED_STATES),
            rejected=self.rejected + int(state in _REJECTED_STATES),
        )


@dataclass(frozen=True)
class RunFacts:
    """One run of one bot: when it ran, and what it traded."""

    run_id: str
    lifecycle_run_id: str
    active: bool
    started_at_ms: int
    stopped_at_ms: int | None
    transactions: int
    orders: OrderCounts
    #: The owner's flatten after the stop sold what the run held (module doc).
    flattened: bool


@dataclass(frozen=True)
class BotFacts:
    """One registered bot's whole history in this custody database."""

    strategy_instance_id: str
    symbol: str
    created_at_ms: int
    retired_at_ms: int | None
    config: BotConfigResource | None
    #: Newest first.
    runs: tuple[RunFacts, ...]
    transactions: int
    orders: OrderCounts
    holds_money: bool
    live_custody: bool
    committed_cents: int | None
    result: Decimal | None
    fees: Decimal | None


@dataclass(frozen=True)
class CustodyHistory:
    """Every bot one custody database holds, from one snapshot.

    ``money_unavailable`` names why results and fees are unknown (the fee
    evidence cannot vouch for them); every bot then carries ``None`` for
    both, never zero.
    """

    account_id: str
    bots: tuple[BotFacts, ...]
    money_unavailable: str | None


def project_custody_history(
    conn: sqlite3.Connection,
    *,
    fees: BudgetFees | None,
    fees_unavailable: str | None = None,
    strategy_instance_ids: Sequence[str] | None = None,
) -> CustodyHistory:
    """Project every bot (or the named ones) on the caller's snapshot.

    ``fees`` is the canonical fee reconciler's answer for this snapshot, or
    ``None`` with ``fees_unavailable`` naming why it could not be read.

    Formula: transactions = |effective fills| (per run: those whose order's
      effect operation names the run); orders = order rows by provenance,
      bucketed by broker state; result = ``bot_results_from_fills`` over the
      same fills; fees = the fee reconciler's per-subject total.
    Reference: https://github.com/tim1016/learn-ai/issues/2574; money
      semantics are PRD #2540's, unchanged.
    Canonical implementation: this composition; the counts reuse
      ``effective_fill_records`` and the money ``project_bot_results``'
      ``bot_results_from_fills``, over the one fill read.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_bot_history.py.
    """
    account_id = str(conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0])
    registrations = [
        row for row in reads.strategy_instances(conn)
        if strategy_instance_ids is None or row["strategy_instance_id"] in strategy_instance_ids
    ]
    sids = [str(row["strategy_instance_id"]) for row in registrations]
    if not sids:
        return CustodyHistory(account_id=account_id, bots=(), money_unavailable=fees_unavailable)

    order_runs: dict[str, tuple[str, str | None]] = {}
    #: Each order the broker reports filled, to the effect operation it was placed for.
    filled_order_effects: dict[str, str] = {}
    orders_by_bot: dict[str, OrderCounts] = {}
    orders_by_run: dict[str, OrderCounts] = {}
    for row in conn.execute(
        "SELECT o.order_ref, o.effect_operation_id, o.broker_state, e.strategy_instance_id, e.run_id "
        "FROM orders o JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.strategy_instance_id IS NOT NULL"
    ):
        sid, run_id, state = str(row["strategy_instance_id"]), row["run_id"], row["broker_state"]
        order_runs[str(row["order_ref"])] = (sid, run_id)
        if _reported_filled(state):
            filled_order_effects[str(row["order_ref"])] = str(row["effect_operation_id"])
        orders_by_bot[sid] = orders_by_bot.get(sid, OrderCounts()).plus(state)
        if run_id is not None:
            orders_by_run[run_id] = orders_by_run.get(run_id, OrderCounts()).plus(state)

    fills_by_bot: dict[str, int] = {}
    fills_by_run: dict[str, int] = {}
    #: Effect operations whose own order filled and whose effective fill the Clerk holds.
    sold_effects: set[str] = set()
    # A fill's subject is its order's bot's own: the effect operation's
    # subject trigger admits no other.
    fills = effective_fill_records(conn, account_id=account_id, strategy_instance_ids=sids)
    for fill in fills:
        sid, run_id = order_runs[fill.order_ref]
        fills_by_bot[sid] = fills_by_bot.get(sid, 0) + 1
        if run_id is not None:
            fills_by_run[run_id] = fills_by_run.get(run_id, 0) + 1
        if fill.order_ref in filled_order_effects:
            sold_effects.add(filled_order_effects[fill.order_ref])

    flattens = _owner_flatten_instants(conn, sold_effects=sold_effects)
    runs_by_bot: dict[str, list[RunFacts]] = {}
    for row in conn.execute(
        "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, stopped_at_ms "
        "FROM runs ORDER BY started_at_ms DESC, run_id DESC"
    ):
        run_id, sid = str(row["run_id"]), str(row["strategy_instance_id"])
        bot_runs = runs_by_bot.setdefault(sid, [])
        stopped_at_ms = None if row["stopped_at_ms"] is None else int(row["stopped_at_ms"])
        # Newest first, so the run appended last is the one that began after this one.
        next_started_at_ms = bot_runs[-1].started_at_ms if bot_runs else None
        bot_runs.append(RunFacts(
            run_id=run_id,
            lifecycle_run_id=str(row["lifecycle_run_id"]),
            active=row["state"] == "ACTIVE",
            started_at_ms=int(row["started_at_ms"]),
            stopped_at_ms=stopped_at_ms,
            transactions=fills_by_run.get(run_id, 0),
            orders=orders_by_run.get(run_id, OrderCounts()),
            flattened=stopped_at_ms is not None and any(
                stopped_at_ms <= at_ms and (next_started_at_ms is None or at_ms < next_started_at_ms)
                for at_ms in flattens.get(sid, ())
            ),
        ))

    budgets = {
        str(row["strategy_instance_id"]): int(row["committed_cents"])
        for row in conn.execute("SELECT strategy_instance_id, committed_cents FROM deployment_budgets")
    }
    holding = bots_holding_money(conn)
    live_custody = reads.strategy_instances_with_live_custody(conn)
    results: dict[str, BotResult] = {}
    money_unavailable = fees_unavailable
    if fees is not None and money_unavailable is None:
        try:
            results = bot_results_from_fills(fills, fees=fees, strategy_instance_ids=sids)
        except BudgetUnavailable as exc:
            money_unavailable = str(exc)

    known = fees if money_unavailable is None else None
    bots: list[BotFacts] = []
    with money_context():
        for registration in registrations:
            sid = str(registration["strategy_instance_id"])
            bots.append(BotFacts(
                strategy_instance_id=sid,
                symbol=str(registration["symbol"]),
                created_at_ms=int(registration["created_at_ms"]),
                retired_at_ms=(
                    None if registration["retired_at_ms"] is None else int(registration["retired_at_ms"])
                ),
                config=reads.bot_config(conn, sid),
                runs=tuple(runs_by_bot.get(sid, ())),
                transactions=fills_by_bot.get(sid, 0),
                orders=orders_by_bot.get(sid, OrderCounts()),
                holds_money=sid in holding,
                live_custody=sid in live_custody,
                committed_cents=budgets.get(sid),
                result=None if known is None else results[sid].result,
                fees=None if known is None else known.total_for(bot_subject_id(sid)),
            ))
    return CustodyHistory(account_id=account_id, bots=tuple(bots), money_unavailable=money_unavailable)


def is_owner_flatten_decision(durable_decision_id: str) -> bool:
    """Whether an EXIT's recorded decision is one of the owner's own flattens.

    The Clerk's safe flatten and the panel's flatten-and-stop are; a
    watchdog re-drive or a strategy's own EXIT is not.
    """
    return decision_id_from_durable(durable_decision_id).startswith(OWNER_FLATTEN_DECISION_PREFIXES)


@dataclass(frozen=True)
class _AcceptedExit:
    strategy_instance_id: str
    accepted_at_ms: int
    #: As stored (``decision_id_from_durable`` reads it back).
    durable_decision_id: str

    @property
    def redriven_episode_token(self) -> str | None:
        return redriven_episode_token(decision_id_from_durable(self.durable_decision_id))


def _owner_flatten_instants(
    conn: sqlite3.Connection, *, sold_effects: set[str]
) -> dict[str, tuple[int, ...]]:
    """When each bot's owner flattens that sold were accepted.

    A flatten sold when its own EXIT is in ``sold_effects``, or a re-drive of
    it is (module doc). The instant is the owner flatten's own acceptance; an
    owner flatten nothing sold for -- refused, expired unsent, only partly
    filled and never re-driven -- is no flatten at all.
    """
    exits = {
        str(row["effect_operation_id"]): _AcceptedExit(
            str(row["strategy_instance_id"]), int(row["created_at_ms"]), str(row["decision_id"]),
        )
        for row in conn.execute(
            "SELECT e.effect_operation_id, e.strategy_instance_id, e.created_at_ms, "
            "json_extract(t.facts_json, '$.decision_id') AS decision_id "
            "FROM effect_operations e JOIN custody_transitions t "
            "ON t.effect_operation_id = e.effect_operation_id AND t.transition_kind = 'EXIT_ACCEPTED' "
            "WHERE e.kind = 'EXIT' AND e.strategy_instance_id IS NOT NULL"
        )
    }
    redriven = _redriven_exits(conn, tokens={
        token for effect_id in sold_effects & exits.keys()
        if (token := exits[effect_id].redriven_episode_token) is not None
    })

    def owner_flatten(effect_id: str, seen: frozenset[str]) -> str | None:
        """The owner flatten this EXIT carries out: itself, or the one its episode re-drives."""
        accepted = exits[effect_id]
        if is_owner_flatten_decision(accepted.durable_decision_id):
            return effect_id
        for origin in redriven.get(accepted.redriven_episode_token or "", ()):
            if origin in exits and origin not in seen and (found := owner_flatten(origin, seen | {origin})):
                return found
        return None

    flattens: dict[str, list[int]] = {}
    for effect_id in sold_effects & exits.keys():
        origin = owner_flatten(effect_id, frozenset({effect_id}))
        if origin is not None:
            flattens.setdefault(exits[origin].strategy_instance_id, []).append(exits[origin].accepted_at_ms)
    return {sid: tuple(instants) for sid, instants in flattens.items()}


def _redriven_exits(conn: sqlite3.Connection, *, tokens: set[str]) -> dict[str, tuple[str, ...]]:
    """For each re-drive episode token asked about, every EXIT its episode was about.

    An ``EXIT_NOT_FLAT`` episode's evidence is the stuck EXIT's order, and a
    failed re-drive refreshes the episode with its own order, so the
    episode's row names only the latest. Its raise (the transition its id
    names) and each refresh (``proof_reference`` = the episode) stay in the
    log, and together name them all. Only the episodes a sold re-drive names
    are read; facts that cannot be read prove no flatten, and never fail the
    whole history.
    """
    if not tokens:
        return {}
    effect_of_order = {
        str(row["order_ref"]): str(row["effect_operation_id"])
        for row in conn.execute("SELECT order_ref, effect_operation_id FROM orders")
    }
    redriven: dict[str, tuple[str, ...]] = {}
    for row in conn.execute("SELECT uncertainty_id FROM uncertainties WHERE reason_code = ?", (EXIT_NOT_FLAT_REASON_CODE,)):
        uncertainty_id = str(row["uncertainty_id"])
        token = redrive_episode_token(uncertainty_id)
        if token not in tokens:
            continue
        refs: set[str] = set()
        for transition in conn.execute(
            "SELECT facts_json FROM custody_transitions "
            "WHERE (sequence = ? AND transition_kind = 'UNCERTAINTY_RAISED') "
            "OR (transition_kind = 'UNCERTAINTY_REFRESHED' AND proof_reference = ?)",
            (uncertainty_sequence(uncertainty_id), uncertainty_id),
        ):
            try:
                refs.update(UncertaintyRaisedFacts.from_facts_json(str(transition["facts_json"])).evidence_refs)
            except (TypeError, ValueError, KeyError):
                logger.warning(
                    "A stuck-EXIT episode's facts could not be read; its re-drives prove no flatten",
                    exc_info=True,
                    extra={"action": "bot_history_episode_unreadable", "uncertainty_id": uncertainty_id},
                )
        redriven[token] = tuple(sorted({effect_of_order[ref] for ref in refs if ref in effect_of_order}))
    return redriven


def read_custody_history(
    db_path: Path,
    *,
    now_ms: int,
    fee_evidence_checked_at_ms: int | None,
    strategy_instance_ids: Sequence[str] | None = None,
    memo: RevisionMemo[CustodyHistory] | None = None,
) -> CustodyHistory:
    """``project_custody_history`` on its own query-only snapshot of a custody file.

    Never under the Clerk's writer, like ``read_bot_results``: a history read
    must not hold up trading. ``fee_evidence_checked_at_ms`` is the owning
    process's fee-evidence freshness (``None`` for a file no running Clerk
    owns: a real account's fees are then unknown, while a simulated one's
    need no broker evidence). A ``memo`` answers again at an unchanged
    custody revision without projecting (``RevisionMemo``). Blocking work:
    callers on the event loop run it in a worker thread.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution

    def project(snapshot: sqlite3.Connection) -> CustodyHistory:
        with _at_current_schema(snapshot) as conn:
            fees = custody_fee_attribution(conn, now_ms=now_ms, evidence_checked_at_ms=fee_evidence_checked_at_ms)
            return project_custody_history(
                conn, fees=fees if fees.known else None, strategy_instance_ids=strategy_instance_ids,
                fees_unavailable=None if fees.known else "Fee evidence is unresolved: " + "; ".join(fees.unresolved),
            )

    # Unknown money is kept only for a file no running Clerk owns: with no
    # fee evidence to go stale, it too moves only with the custody revision.
    return read_at_revision(
        db_path, strategy_instance_ids=strategy_instance_ids, memo=memo, project=project,
        keep=lambda history: history.money_unavailable is None or fee_evidence_checked_at_ms is None,
    )


class CustodySchemaUnreadable(Exception):
    """A custody file keeps a schema no registered migration brings to this build's."""

    def __init__(self, schema_version: int) -> None:
        super().__init__(f"custody schema_version={schema_version}; this build reads {SCHEMA_VERSION}")
        self.schema_version = schema_version


@contextmanager
def _at_current_schema(snapshot: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``snapshot`` at this build's schema: itself, or a private copy migrated to it (module doc).

    The copy is taken inside the snapshot's read transaction, so it is the
    same revision, and the Clerk's own ``migrate_schema`` brings it forward.
    """
    version = int(snapshot.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0])
    if version == SCHEMA_VERSION:
        yield snapshot
        return
    if version > SCHEMA_VERSION:
        raise CustodySchemaUnreadable(version)
    copy = sqlite3.connect(":memory:")
    try:
        snapshot.backup(copy)
        copy.row_factory = sqlite3.Row
        try:
            migrate_schema(copy, from_version=version)
        except ValueError as exc:
            # No registered path from here: a version below the v13 floor.
            raise CustodySchemaUnreadable(version) from exc
        copy.execute("PRAGMA query_only = ON")
        copy.execute("BEGIN")
        yield copy
    finally:
        copy.close()
