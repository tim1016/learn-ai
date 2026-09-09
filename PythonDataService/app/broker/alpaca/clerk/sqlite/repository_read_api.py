"""Committed-read façade shared by the SQLite Clerk repository.

The repository owns transaction admission, mirror fencing, leases, and every
write. This mixin owns only its public read delegates. Keeping these methods
separate makes the single writer spine easier to audit while preserving the
repository's serialized-connection boundary: every read takes the same
coordinator as writers before using the shared connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.sqlite import envelope_reservations, reads, writes
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

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


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

    def decision_receipts_by_transaction(
        self: ClerkSqliteRepository,
        *,
        strategy_instance_id: str,
        transaction_ref: str,
        limit: int,
    ) -> list[DecisionReceiptResource]:
        with self._write_lock:
            return reads.decision_receipts_by_transaction(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                transaction_ref=transaction_ref,
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
        """Return all external observations as a compatibility-friendly mapping list."""
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

    def effect_operation(
        self: ClerkSqliteRepository,
        effect_operation_id: str,
    ) -> EffectOperationResource | None:
        with self._write_lock:
            return reads.effect_operation(self._conn, effect_operation_id)

    def order(self: ClerkSqliteRepository, order_ref: str) -> OrderResource | None:
        with self._write_lock:
            return reads.order(self._conn, order_ref)

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

    def all_order_refs(self: ClerkSqliteRepository) -> frozenset[str]:
        with self._write_lock:
            return reads.all_order_refs(self._conn)

    def entry_orders_for_strategy(
        self: ClerkSqliteRepository,
        strategy_instance_id: str,
    ) -> list[OrderResource]:
        with self._write_lock:
            return reads.entry_orders_for_strategy(self._conn, strategy_instance_id)

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

    def uncertain_orders(self: ClerkSqliteRepository) -> list[OrderResource]:
        with self._write_lock:
            return reads.uncertain_orders(self._conn)

    def attributed_positions_by_symbol(self: ClerkSqliteRepository) -> dict[str, float]:
        with self._write_lock:
            return reads.attributed_positions_by_symbol(self._conn)

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

    def reserved_cash_usd(self: ClerkSqliteRepository, *, observed_at_ms: int) -> float:
        """Cash the accepted ENTERs claim that ``observed_at_ms`` cannot see."""
        with self._write_lock:
            return envelope_reservations.reserved_cash_usd(
                self._conn, observed_at_ms=observed_at_ms
            )
