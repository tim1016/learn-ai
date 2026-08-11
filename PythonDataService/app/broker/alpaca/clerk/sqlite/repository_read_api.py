"""Committed-read façade shared by the SQLite Clerk repository.

The repository owns transaction admission, mirror fencing, leases, and every
write. This mixin owns only its public read delegates. Keeping these methods
separate makes the single writer spine easier to audit while preserving the
repository's serialized-connection boundary: every read takes the same
coordinator as writers before using the shared connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite import reads, writes
from app.broker.alpaca.clerk.sqlite.models import (
    BotConfigResource,
    CommandResource,
    ControlMetaSnapshot,
    DecisionReceiptResource,
    EffectOperationResource,
    ExternalOrderResource,
    OrderResource,
    RunResource,
)

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


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
                    "price": order.price,
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
    ) -> list[EffectOperationResource]:
        with self._write_lock:
            return reads.reconcilable_effect_operations(self._conn)

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
        strategy_instance_id: str,
    ) -> list[dict]:
        with self._write_lock:
            return reads.active_uncertainties_for_admission(
                self._conn,
                strategy_instance_id=strategy_instance_id,
            )

    def active_holds_for_admission(
        self: ClerkSqliteRepository,
        *,
        strategy_instance_id: str,
    ) -> list[dict]:
        with self._write_lock:
            return reads.active_holds_for_admission(
                self._conn,
                strategy_instance_id=strategy_instance_id,
            )
