"""Committed-read façade shared by the SQLite Clerk repository.

The repository owns transaction admission, mirror fencing, leases, and every
write. This mixin owns only its public read delegates. Keeping these methods
separate makes the single writer spine easier to audit while preserving the
repository's serialized-connection boundary: every read takes the same
coordinator as writers before using the shared connection.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.sqlite import reads, writes
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
    RunResource,
)
from app.broker.alpaca.clerk.sqlite.order_projection import read_open_opposite_side_orders
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.broker.contract.models import OrderSide

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.account_money import AccountMoney
    from app.broker.alpaca.clerk.budgets import AccountBudget, ReleaseAtStop
    from app.broker.alpaca.clerk.sqlite.bot_history import CustodyHistory
    from app.broker.alpaca.clerk.sqlite.budget_projection import BotResult
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.services.alpaca_fee_attribution import FeeAttribution

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SqliteLifecycleProjectionSnapshot:
    """One coordinator-locked read of SQLite-owned lifecycle facts."""

    strategy_instance_exists: bool
    active_run_id: str | None
    retired_at_ms: int | None
    expected_run_state: Literal["ACTIVE", "STOPPED", "MISSING"] | None
    control_revision: int


@dataclass(frozen=True, slots=True)
class SqliteLifecycleRecoveryCandidate:
    """One SQLite-active run that boot must reconcile with process liveness."""

    strategy_instance_id: str
    run_id: str


class ClerkSqliteRepositoryReadApi:
    """Read-only public methods mixed into :class:`ClerkSqliteRepository`."""

    def budget_authority_version(self: ClerkSqliteRepository) -> int:
        from app.broker.alpaca.clerk.sqlite.budget_authority import authorization_version

        with self._write_lock:
            return authorization_version(self._conn)

    def get_command(self: ClerkSqliteRepository, command_id: str) -> CommandResource | None:
        """Return one command through the shared committed-read coordinator."""
        with self._write_lock:
            return reads.command(self._conn, command_id)

    def active_run(self: ClerkSqliteRepository, strategy_instance_id: str) -> RunResource | None:
        with self._write_lock:
            row = self._conn.execute(
                "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, "
                "stopped_at_ms FROM runs WHERE strategy_instance_id = ? AND state = 'ACTIVE'",
                (strategy_instance_id,),
            ).fetchone()
            return RunResource(**dict(row)) if row is not None else None

    def latest_run(self: ClerkSqliteRepository, strategy_instance_id: str) -> RunResource | None:
        """Return the instance's newest run, whether it is active or stopped.

        A terminal receipt (``run_outcomes/{run_id}.json``) is keyed by run
        id. When the lifecycle projection carries no duty outcome there is no
        run id inside it to key on, so a reader asks SQLite -- the run
        authority -- which run last ran. Ordered by ``run_id`` as well as
        ``started_at_ms`` so two runs stamped in the same millisecond still
        resolve deterministically.
        """
        with self._write_lock:
            row = self._conn.execute(
                "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, "
                "stopped_at_ms FROM runs WHERE strategy_instance_id = ? "
                "ORDER BY started_at_ms DESC, run_id DESC LIMIT 1",
                (strategy_instance_id,),
            ).fetchone()
            return RunResource(**dict(row)) if row is not None else None

    def latest_run_stops(self: ClerkSqliteRepository) -> dict[str, int | None]:
        """Each instance's newest run's stop instant, in one read.

        The same run ``latest_run`` answers -- ordered by ``started_at_ms``
        then ``run_id`` -- for every instance at once, so a roster poll never
        issues one query per bot. ``None`` while that run has not stopped.
        """
        with self._write_lock:
            rows = self._conn.execute(
                "SELECT strategy_instance_id, stopped_at_ms FROM ("
                "SELECT strategy_instance_id, stopped_at_ms, ROW_NUMBER() OVER ("
                "PARTITION BY strategy_instance_id ORDER BY started_at_ms DESC, run_id DESC) AS newest "
                "FROM runs) WHERE newest = 1"
            ).fetchall()
            return {str(row[0]): None if row[1] is None else int(row[1]) for row in rows}

    def last_clean_account_check_ms(self: ClerkSqliteRepository) -> int | None:
        """When the whole account last reconciled cleanly against the broker.

        An account-wide reconciliation (no operation, no order) that resolved
        successfully; ``None`` when none ever has.
        """
        with self._write_lock:
            row = self._conn.execute(
                "SELECT MAX(attempted_at_ms) FROM reconciliations "
                "WHERE effect_operation_id IS NULL AND order_ref IS NULL AND outcome = 'RESOLVED_SUCCESS'"
            ).fetchone()
            return None if row[0] is None else int(row[0])

    def lifecycle_projection_snapshot(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
        expected_run_id: str | None,
    ) -> SqliteLifecycleProjectionSnapshot:
        """Read active run, retirement, and expected run in one snapshot."""

        with self._write_lock:
            control_revision = reads.control_meta_snapshot(self._conn).control_revision
            instance = reads.strategy_instance(self._conn, strategy_instance_id)
            active = self._conn.execute(
                "SELECT lifecycle_run_id FROM runs "
                "WHERE strategy_instance_id = ? AND state = 'ACTIVE'",
                (strategy_instance_id,),
            ).fetchone()
            expected = (
                self._conn.execute(
                    "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, "
                    "stopped_at_ms FROM runs WHERE strategy_instance_id = ? AND lifecycle_run_id = ?",
                    (strategy_instance_id, expected_run_id),
                ).fetchone()
                if expected_run_id is not None
                else None
            )
            return SqliteLifecycleProjectionSnapshot(
                strategy_instance_exists=instance is not None,
                active_run_id=active["lifecycle_run_id"] if active is not None else None,
                retired_at_ms=instance["retired_at_ms"] if instance is not None else None,
                expected_run_state=(
                    expected["state"]
                    if expected is not None
                    else ("MISSING" if expected_run_id is not None else None)
                ),
                control_revision=control_revision,
            )

    def lifecycle_recovery_candidates(
        self: ClerkSqliteRepository,
    ) -> tuple[SqliteLifecycleRecoveryCandidate, ...]:
        """Return every SQLite-active run under the repository coordinator."""

        with self._write_lock:
            rows = self._conn.execute(
                "SELECT strategy_instance_id, lifecycle_run_id FROM runs "
                "WHERE state = 'ACTIVE' ORDER BY strategy_instance_id ASC"
            ).fetchall()
            return tuple(
                SqliteLifecycleRecoveryCandidate(
                    strategy_instance_id=row["strategy_instance_id"],
                    run_id=row["lifecycle_run_id"],
                )
                for row in rows
            )

    def reconciliation_in_progress(self: ClerkSqliteRepository) -> bool:
        with self._write_lock:
            return self._reconciliation_in_progress

    def verify_operation_claim(
        self: ClerkSqliteRepository,
        *,
        effect_operation_id: str,
        token: str,
    ) -> bool:
        """Whether ``token`` is still the live claim on one operation."""
        with self._write_lock:
            row = self._conn.execute(
                "SELECT claim_token, claim_expires_at_ms FROM effect_operations "
                "WHERE effect_operation_id = ?",
                (effect_operation_id,),
            ).fetchone()
            if row is None:
                return False
            return row["claim_token"] == token and row["claim_expires_at_ms"] >= self._clock()

    def control_meta_snapshot(self: ClerkSqliteRepository) -> ControlMetaSnapshot:
        with self._write_lock:
            return reads.control_meta_snapshot(self._conn)

    def custody_transitions(self: ClerkSqliteRepository) -> list[dict]:
        with self._write_lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions ORDER BY sequence ASC"
            ).fetchall()
            return [writes.row_to_payload(row) for row in rows]

    def last_custody_sequence(self: ClerkSqliteRepository, strategy_instance_id: str | None = None) -> int:
        """The newest custody transition's sequence, the ledger's or one bot's; 0 when none.

        Per bot it reads the ``(strategy_instance_id, sequence)`` index. Every
        transition that moves a bot's orders, effects, fills or runs carries
        its ``strategy_instance_id``; the untagged ones are account-level
        (uncertainties, fee evidence, budgets, risk limits) or belong to
        manual and outside orders, which are never a bot's custody.
        """
        with self._write_lock:
            if strategy_instance_id is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions "
                    "WHERE strategy_instance_id = ?",
                    (strategy_instance_id,),
                ).fetchone()
            return int(row["sequence"])

    def last_strategy_transition(
        self: ClerkSqliteRepository, *, strategy_instance_id: str, transition_kind: str,
    ) -> dict | None:
        """Read one instance's latest observation using its sequence index."""
        with self._write_lock:
            row = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions "
                "WHERE strategy_instance_id = ? AND transition_kind = ? ORDER BY sequence DESC LIMIT 1",
                (strategy_instance_id, transition_kind),
            ).fetchone()
            return None if row is None else writes.row_to_payload(row)

    def transitions_for_order(self: ClerkSqliteRepository, order_ref: str) -> list[dict]:
        """Return every transition for one order in sequence order."""
        with self._write_lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions "
                "WHERE order_ref = ? ORDER BY sequence ASC",
                (order_ref,),
            ).fetchall()
            return [writes.row_to_payload(row) for row in rows]

    def first_order_transition(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
        transition_kind: str | None = None,
    ) -> dict | None:
        """The earliest transition for one order — of ``transition_kind`` if given.

        With :meth:`last_order_transition`, this is how a caller that wants one
        fact out of an order's history should ask for it. Seven callers instead
        fetched *every* transition for the order and scanned the list in
        Python; one reconciliation pass over a 10k-transition ledger did that
        per order, per effect, and held the event loop for minutes (#1942).

        Distinct from :meth:`has_order_transition`, which answers the same
        ``WHERE`` with ``SELECT 1``: an existence check should not build a
        22-column dict, so both exist. Callers that only need a yes/no keep
        using that one.

        ``sequence`` is the table's ``INTEGER PRIMARY KEY``, so
        ``ix_custody_transitions_order_ref`` yields sequence order for free and
        ``LIMIT 1`` genuinely short-circuits (no temp b-tree).
        """
        return self._one_order_transition(order_ref, transition_kind, "ASC")

    def last_order_transition(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
        transition_kind: str | None = None,
    ) -> dict | None:
        """The latest transition for one order — the mirror of :meth:`first_order_transition`."""
        return self._one_order_transition(order_ref, transition_kind, "DESC")

    def _one_order_transition(
        self: ClerkSqliteRepository,
        order_ref: str,
        transition_kind: str | None,
        direction: Literal["ASC", "DESC"],
    ) -> dict | None:
        clauses = ["order_ref = ?"]
        params: list[str] = [order_ref]
        if transition_kind is not None:
            clauses.append("transition_kind = ?")
            params.append(transition_kind)
        with self._write_lock:
            row = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions "
                f"WHERE {' AND '.join(clauses)} ORDER BY sequence {direction} LIMIT 1",
                tuple(params),
            ).fetchone()
            return None if row is None else writes.row_to_payload(row)

    def first_effect_transition(
        self: ClerkSqliteRepository,
        *,
        effect_operation_id: str,
        transition_kind: str,
    ) -> dict | None:
        """The earliest transition of one kind for one effect operation.

        The effect-scoped twin of :meth:`first_order_transition`, and not a
        convenience: a re-driven EXIT reuses the same entry ``order_ref``, so
        the order-scoped read hands back the *first* EXIT's acceptance for
        every later one. A reduction rebuilt from the wrong acceptance would
        carry the wrong decision's leg shape.

        ``ix_custody_transitions_effect_sequence`` yields sequence order, so
        the ``LIMIT 1`` short-circuits rather than sorting the operation's
        whole history.
        """
        with self._write_lock:
            row = self._conn.execute(
                f"SELECT {', '.join(writes.TRANSITION_COLUMNS)} FROM custody_transitions "
                "WHERE effect_operation_id = ? AND transition_kind = ? "
                "ORDER BY sequence ASC LIMIT 1",
                (effect_operation_id, transition_kind),
            ).fetchone()
            return None if row is None else writes.row_to_payload(row)

    def max_order_transition_recorded_at_ms(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
        transition_kind: str,
    ) -> int | None:
        """The greatest ``recorded_at_ms`` among an order's transitions of one kind.

        Distinct from :meth:`last_order_transition` on purpose. ``sequence`` is
        append order; ``recorded_at_ms`` comes from the repository clock, which
        is wall time. A host clock that steps backwards and rebounds can leave
        the latest-by-sequence row holding a *lower* timestamp than an earlier
        one, so a grace window anchored on "the most recent uncertainty" has to
        ask for the maximum rather than the last (#1942).
        """
        with self._write_lock:
            row = self._conn.execute(
                "SELECT MAX(recorded_at_ms) FROM custody_transitions "
                "WHERE order_ref = ? AND transition_kind = ?",
                (order_ref, transition_kind),
            ).fetchone()
            return None if row is None else row[0]

    def has_order_transition(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
        transition_kind: str,
    ) -> bool:
        with self._write_lock:
            row = self._conn.execute(
                "SELECT 1 FROM custody_transitions WHERE order_ref = ? "
                "AND transition_kind = ? LIMIT 1",
                (order_ref, transition_kind),
            ).fetchone()
            return row is not None

    def strategy_instances(self: ClerkSqliteRepository) -> list[dict]:
        with self._write_lock:
            return reads.strategy_instances(self._conn)

    def strategy_instances_with_live_custody(self: ClerkSqliteRepository) -> set[str]:
        with self._write_lock:
            return reads.strategy_instances_with_live_custody(self._conn)

    def strategy_instance(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> dict | None:
        with self._write_lock:
            return reads.strategy_instance(self._conn, strategy_instance_id)

    def bot_config(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> BotConfigResource | None:
        with self._write_lock:
            return reads.bot_config(self._conn, strategy_instance_id)

    def decision_receipt_tail(
        self: ClerkSqliteRepository,
        *,
        strategy_instance_id: str,
        limit: int,
    ) -> list[DecisionReceiptResource]:
        with self._write_lock:
            return reads.decision_receipt_tail(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                limit=limit,
            )

    def external_order(
        self: ClerkSqliteRepository,
        external_order_id: str,
    ) -> ExternalOrderResource | None:
        with self._write_lock:
            return reads.external_order(self._conn, external_order_id)

    def external_order_by_broker_order_id(
        self: ClerkSqliteRepository,
        broker_order_id: str,
    ) -> ExternalOrderResource | None:
        with self._write_lock:
            return reads.external_order_by_broker_order_id(self._conn, broker_order_id)

    def external_orders(self: ClerkSqliteRepository) -> list[dict]:
        """Return every outside order (``reads.external_orders``) as a compatibility-friendly mapping list."""
        with self._write_lock:
            return [
                {
                    "external_order_id": order.external_order_id,
                    "broker_order_id": order.broker_order_id,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "qty": order.qty,
                    "order_type": order.order_type,
                    "limit_price": order.limit_price,
                    "stop_price": order.stop_price,
                    "filled_avg_price": order.filled_avg_price,
                    "observed_at_ms": order.observed_at_ms,
                    "acknowledged_at_ms": order.acknowledged_at_ms,
                    "ack_operator": order.ack_operator,
                    "evidence_refs": order.evidence_refs,
                }
                for order in reads.external_orders(self._conn)
            ]

    def external_order_resources(self: ClerkSqliteRepository) -> tuple[ExternalOrderResource, ...]:
        """Retained outside-order evidence, including current lifecycle proof for reconciliation.

        A manual chain's member is not refreshed as a foreign order: the
        chain's own resolution follows it (#2787).
        """
        with self._write_lock:
            return tuple(reads.external_orders(self._conn))

    def effect_operation(
        self: ClerkSqliteRepository,
        effect_operation_id: str,
    ) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.effect_operation(self._conn, effect_operation_id)

    def order(self: ClerkSqliteRepository, order_ref: str) -> OrderResource | None:
        with self._write_lock:
            return reads.order(self._conn, order_ref)

    def manual_chain_order_ref(self: ClerkSqliteRepository, broker_order_id: str) -> str | None:
        with self._write_lock:
            return reads.manual_chain_order_ref(self._conn, broker_order_id)

    def manual_chain_member_ids(self: ClerkSqliteRepository, order_ref: str) -> frozenset[str]:
        with self._write_lock:
            return reads.manual_chain_member_ids(self._conn, order_ref)

    def order_for_effect_operation(
        self: ClerkSqliteRepository,
        effect_operation_id: str,
    ) -> OrderResource | None:
        with self._write_lock:
            return reads.order_for_effect_operation(self._conn, effect_operation_id)

    def manual_order_ticket(
        self: ClerkSqliteRepository,
        ticket_id: str,
    ) -> ManualOrderTicketResource | None:
        with self._write_lock:
            return reads.manual_order_ticket(self._conn, ticket_id)

    def manual_order_cancellation(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
    ) -> ManualOrderCancellationResource | None:
        with self._write_lock:
            return reads.manual_order_cancellation(self._conn, order_ref=order_ref)

    def manual_order_leg_for_order_ref(
        self: ClerkSqliteRepository,
        *,
        order_ref: str,
    ) -> ManualOrderLegResource | None:
        with self._write_lock:
            return reads.manual_order_leg_for_order_ref(self._conn, order_ref=order_ref)

    def manual_order_cancellation_for_effect(
        self: ClerkSqliteRepository,
        *,
        effect_operation_id: str,
    ) -> ManualOrderCancellationResource | None:
        with self._write_lock:
            return reads.manual_order_cancellation_for_effect(
                self._conn,
                effect_operation_id=effect_operation_id,
            )

    def custody_subject(self: ClerkSqliteRepository, subject_id: str) -> dict | None:
        with self._write_lock:
            return reads.custody_subject(self._conn, subject_id)

    def orders_for_effect_operation(
        self: ClerkSqliteRepository,
        effect_operation_id: str,
    ) -> list[OrderResource]:
        with self._write_lock:
            return reads.orders_for_effect_operation(self._conn, effect_operation_id)

    def terminal_entry_orders_with_fills(
        self: ClerkSqliteRepository, *, order_ref: str | None = None
    ) -> list[tuple[str, str]]:
        with self._write_lock:
            return reads.terminal_entry_orders_with_fills(self._conn, order_ref=order_ref)

    def terminal_entry_orders_unproven_at_broker(
        self: ClerkSqliteRepository,
        *,
        symbols: frozenset[str],
        exclude_client_order_ids: frozenset[str],
        rested_since_ms: int,
        limit: int,
    ) -> list[str]:
        with self._write_lock:
            return reads.terminal_entry_orders_unproven_at_broker(
                self._conn,
                symbols=symbols,
                exclude_client_order_ids=exclude_client_order_ids,
                rested_since_ms=rested_since_ms,
                limit=limit,
            )

    def all_order_refs(self: ClerkSqliteRepository) -> frozenset[str]:
        with self._write_lock:
            return reads.all_order_refs(self._conn)

    def entry_orders_for_strategy(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> list[OrderResource]:
        with self._write_lock:
            return reads.entry_orders_for_strategy(self._conn, strategy_instance_id)

    def entry_orders_owed_a_cancel(self: ClerkSqliteRepository) -> list[OrderResource]:
        with self._write_lock:
            return reads.entry_orders_owed_a_cancel(self._conn)

    def orders_for_strategy(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> list[OrderResource]:
        with self._write_lock:
            return reads.orders_for_strategy(self._conn, strategy_instance_id)

    def active_exit_for_order(
        self: ClerkSqliteRepository,
        order_ref: str,
    ) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.active_exit_for_order(self._conn, order_ref)

    def active_exit_for_strategy(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.active_exit_for_strategy(self._conn, strategy_instance_id)

    def strategies_with_active_exit(
        self: ClerkSqliteRepository, strategy_instance_ids: Collection[str]
    ) -> frozenset[str]:
        with self._write_lock:
            return reads.strategies_with_active_exit(self._conn, strategy_instance_ids)

    def reconcilable_effect_operations(
        self: ClerkSqliteRepository,
        *,
        subject_id: str | None = None,
    ) -> list[EffectOperationResource]:
        with self._write_lock:
            return reads.reconcilable_effect_operations(self._conn, subject_id=subject_id)

    def position(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
        symbol: str,
    ) -> float:
        with self._write_lock:
            return reads.position(self._conn, strategy_instance_id, symbol)

    def fills_for_order(self: ClerkSqliteRepository, order_ref: str) -> list[dict]:
        with self._write_lock:
            return reads.fills_for_order(self._conn, order_ref)

    def active_execution_price_conflicts(self: ClerkSqliteRepository):
        """Every open ``EXECUTION_PRICE_CONFLICT`` episode (#2460).

        The sweep re-derivation's worklist; see
        ``order_evidence.reconcile_execution_price_conflicts``.
        """
        with self._write_lock:
            return reads.active_execution_price_conflicts(self._conn)

    def effective_fill_totals_for_order(
        self: ClerkSqliteRepository,
        order_ref: str,
    ) -> tuple[float, float]:
        """Return the order's effective execution quantity and cost."""
        with self._write_lock:
            return reads.effective_fill_totals_for_order(self._conn, order_ref)

    def effective_exact_fill_totals_for_order(
        self: ClerkSqliteRepository,
        order_ref: str,
    ) -> tuple[float, float]:
        """Return only effective broker-issued execution slices for one order."""
        with self._write_lock:
            return reads.effective_exact_fill_totals_for_order(self._conn, order_ref)

    def latest_reported_filled_quantity(self: ClerkSqliteRepository, order_ref: str) -> float | None:
        """The cumulative filled quantity the order's latest acknowledgement reported."""
        with self._write_lock:
            return reads.latest_reported_filled_quantity(self._conn, order_ref)

    def order_fills_short_of_broker_cumulative(self: ClerkSqliteRepository, order_ref: str) -> bool:
        """Whether the order's effective fills fall short of the broker's cumulative (#2305)."""
        with self._write_lock:
            return reads.order_fills_short_of_broker_cumulative(self._conn, order_ref)

    def uncertain_orders(self: ClerkSqliteRepository) -> list[OrderResource]:
        with self._write_lock:
            return reads.uncertain_orders(self._conn)

    def attributed_positions_by_symbol(self: ClerkSqliteRepository) -> dict[str, float]:
        with self._write_lock:
            return reads.attributed_positions_by_symbol(self._conn)

    def attributed_positions_by_subject(
        self: ClerkSqliteRepository,
    ) -> dict[tuple[str, str], float]:
        with self._write_lock:
            return reads.attributed_positions_by_subject(self._conn)

    def market_data_symbols(self: ClerkSqliteRepository) -> tuple[str, ...]:
        """Server-owned demand from deployed strategies and live custody."""
        with self._write_lock:
            return reads.market_data_symbols(self._conn)

    def attributed_positions_for_strategy(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> dict[str, float]:
        with self._write_lock:
            return reads.attributed_positions_for_strategy(
                self._conn,
                strategy_instance_id,
            )

    def attributed_positions_for_subject(
        self: ClerkSqliteRepository,
        subject_id: str,
    ) -> dict[str, float]:
        with self._write_lock:
            return reads.attributed_positions_for_subject(self._conn, subject_id)

    def manual_reduction_available_quantity(
        self: ClerkSqliteRepository,
        *,
        subject_id: str,
        symbol: str,
    ) -> float:
        """Return the atomic manual-custody sell capacity for one symbol."""
        with self._write_lock:
            return reads.manual_reduction_available_quantity(
                self._conn,
                subject_id=subject_id,
                symbol=symbol,
            )

    def has_nonterminal_manual_order(self: ClerkSqliteRepository) -> bool:
        """Whether manual broker intent currently fences new account exposure."""
        with self._write_lock:
            return reads.has_nonterminal_manual_order(self._conn)

    def open_opposite_side_orders(
        self: ClerkSqliteRepository,
        *,
        symbol: str,
        side: OrderSide,
    ) -> tuple[ProjectedOrder, ...]:
        """This account's orders still open on ``symbol`` on the other side from ``side``.

        Every custody subject's, bots' and manual tickets' alike: the orders
        Alpaca's wash-trade protection would refuse a ``side`` order against.
        """
        with self._write_lock:
            return read_open_opposite_side_orders(self._conn, symbol=symbol, side=side)

    def has_nonterminal_manual_order_outside_ticket(
        self: ClerkSqliteRepository,
        *,
        ticket_id: str,
    ) -> bool:
        """Keep a ticket continuation fenced by other manual custody work."""
        with self._write_lock:
            return reads.has_nonterminal_manual_order_outside_ticket(
                self._conn,
                ticket_id=ticket_id,
            )

    def active_hold(
        self: ClerkSqliteRepository,
        *,
        scope: str,
        reason_code: str,
    ) -> dict | None:
        with self._write_lock:
            return reads.active_hold(self._conn, scope=scope, reason_code=reason_code)

    def active_uncertainties(self: ClerkSqliteRepository) -> list[dict]:
        """Every unresolved uncertainty on the account, oldest first (#2228)."""
        with self._write_lock:
            return reads.active_uncertainties(self._conn)

    def uncertainty_history(
        self: ClerkSqliteRepository,
        *,
        scope: str,
        reason_code: str,
        strategy_instance_id: str | None,
    ) -> list[dict]:
        """Every episode, active or resolved, for one cause identity."""
        with self._write_lock:
            return reads.uncertainty_history(
                self._conn,
                scope=scope,
                reason_code=reason_code,
                strategy_instance_id=strategy_instance_id,
            )

    def active_uncertainty(
        self: ClerkSqliteRepository,
        *,
        scope: str,
        reason_code: str,
        strategy_instance_id: str | None,
    ) -> dict | None:
        with self._write_lock:
            return reads.active_uncertainty(
                self._conn,
                scope=scope,
                reason_code=reason_code,
                strategy_instance_id=strategy_instance_id,
            )

    def active_uncertainties_for_admission(
        self: ClerkSqliteRepository,
        *,
        strategy_instance_id: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict]:
        with self._write_lock:
            return reads.active_uncertainties_for_admission(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                subject_id=subject_id,
            )

    def active_holds_for_admission(
        self: ClerkSqliteRepository,
        *,
        strategy_instance_id: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict]:
        with self._write_lock:
            return reads.active_holds_for_admission(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                subject_id=subject_id,
            )

    def deployment_budget(self: ClerkSqliteRepository, strategy_instance_id: str) -> dict | None:
        """Immutable consent and its durable launch/release outcome."""
        with self._write_lock:
            row = self._conn.execute("SELECT * FROM deployment_budgets WHERE strategy_instance_id=?", (strategy_instance_id,)).fetchone()
            return None if row is None else dict(row)

    def fee_attribution(
        self: ClerkSqliteRepository, *, now_ms: int, from_ms: int | None = None, require_fresh_evidence: bool = True,
    ) -> FeeAttribution:
        """The canonical custody fee projection, fresh only while this process's producer is.

        ``from_ms`` keeps only the fee days whose ET midnight is at or after it
        (an Activity period); admission always reads the whole lifetime.
        ``require_fresh_evidence=False`` values the evidence already recorded,
        for a read that spends nothing (``custody_fee_attribution``).
        """
        from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution

        with self._write_lock:
            return custody_fee_attribution(
                self._conn, now_ms=now_ms, evidence_checked_at_ms=self._fee_evidence_checked_at_ms,
                from_ms=from_ms, require_fresh_evidence=require_fresh_evidence,
            )

    def account_budget(self: ClerkSqliteRepository, *, cash: object, seen_before_ms: int, modelled_fees_seen_before_ms: int | None = None) -> AccountBudget:
        """One revision-coherent money authority for preview and admission."""
        from app.broker.alpaca.clerk.sqlite.budget_projection import project_account_budget

        with self._write_lock:
            fees = self.fee_attribution(now_ms=self.clock())
            return project_account_budget(self._conn, cash=cash, seen_before_ms=seen_before_ms, fees=fees, modelled_fees_seen_before_ms=modelled_fees_seen_before_ms)

    def release_at_stop(self: ClerkSqliteRepository, *, run_id: str) -> ReleaseAtStop | None:
        """What stopping ``run_id`` releases now, valued as every money read values it (#2555).

        ``None`` for a run with no budget, which releases nothing; only a
        budgeted run pays for the lifetime fee projection. A Stop spends
        nothing, so it values the fee evidence already recorded however old
        (owner decision 2026-09-29): a restart's recovery stops every running
        bot before this process has read any. A Stop is never refused for
        money: when the deployment still cannot be valued
        (``RELEASE_VALUATION_FAILURES``), this is ``None`` -- the Stop records
        no amounts and the bot's money shows a labelled estimate -- and why is
        logged with its traceback.
        """
        from app.broker.alpaca.clerk.sqlite import budget_projection

        with self._write_lock:
            if self._conn.execute("SELECT 1 FROM deployment_budgets WHERE run_id=?", (run_id,)).fetchone() is None:
                return None
            try:
                fees = self.fee_attribution(now_ms=self.clock(), require_fresh_evidence=False)
                return budget_projection.value_release(self._conn, run_id=run_id, fees=fees)
            except budget_projection.RELEASE_VALUATION_FAILURES as exc:
                logger.warning(
                    "Stop records no release: the deployment's money cannot be valued",
                    exc_info=True,
                    extra={"action": "stop_release_unvalued", "account_id": self.account_id, "run_id": run_id,
                           "reason": str(exc), "error_type": type(exc).__name__},
                )
                return None

    def account_money(self: ClerkSqliteRepository, *, cash: object, seen_before_ms: int, modelled_fees_seen_before_ms: int | None = None) -> AccountMoney:
        """Where the account's money is, from the same read ``account_budget`` makes."""
        from app.broker.alpaca.clerk.sqlite.budget_projection import project_account_money

        with self._write_lock:
            fees = self.fee_attribution(now_ms=self.clock())
            return project_account_money(self._conn, cash=cash, seen_before_ms=seen_before_ms, fees=fees, modelled_fees_seen_before_ms=modelled_fees_seen_before_ms)

    def bots_holding_money(self: ClerkSqliteRepository) -> frozenset[str]:
        """The bots with position cost or still-claimed money (see ``budget_projection``)."""
        from app.broker.alpaca.clerk.sqlite.budget_projection import bots_holding_money

        with self._write_lock:
            return bots_holding_money(self._conn)

    def bot_results(self: ClerkSqliteRepository, strategy_instance_ids: Sequence[str]) -> dict[str, BotResult]:
        """Whole-life results, read on a query-only snapshot: never under the writer's lock.

        Blocking: an async caller runs it in a worker thread. Answers at an
        unchanged custody revision are reused (``RevisionMemo``).
        """
        from app.broker.alpaca.clerk.sqlite.budget_projection import RevisionMemo, read_bot_results

        if self._bot_results_memo is None:
            self._bot_results_memo = RevisionMemo()
        return read_bot_results(
            self.db_path, now_ms=self.clock(), fee_evidence_checked_at_ms=self._fee_evidence_checked_at_ms,
            strategy_instance_ids=strategy_instance_ids, memo=self._bot_results_memo,
        )

    def neighbour_custody_file(self: ClerkSqliteRepository, account_id: str) -> Path:
        """Where another authority's ``clerk.db`` lives beside this one, for a read-only snapshot.

        The same account tree as this database (``writes.account_paths``), so a
        Live account's Shadow database is found wherever its Clerk keeps its own.
        """
        from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME

        return writes.neighbour_account_file(self._account_dir, account_id, DB_FILENAME)

    def bot_history(
        self: ClerkSqliteRepository, strategy_instance_ids: Sequence[str] | None = None,
    ) -> CustodyHistory:
        """Every bot this custody holds (or the named ones), run by run, on a query-only snapshot (#2574).

        Never under the writer's lock, like ``bot_results``, and with this
        process's own fee-evidence freshness. Blocking: an async caller runs
        it in a worker thread.
        """
        from app.broker.alpaca.clerk.sqlite.bot_history import read_custody_history

        return read_custody_history(
            self.db_path, now_ms=self.clock(), fee_evidence_checked_at_ms=self._fee_evidence_checked_at_ms,
            strategy_instance_ids=strategy_instance_ids,
        )
