"""NoSubmitBrokerAdapter (PRD-C shadow mode).

A broker adapter that drives the *same* ``LiveEngine`` as the executing
``IbkrBrokerAdapter`` but **never** submits an order to the broker. It
connects to the IBKR Gateway for market data only; ``place_order`` records
the intent and, at the next bar, synthesises a ``shadow_sim`` fill via
``ShadowFillSimulator`` instead of calling ``ib.placeOrder``.

Shadow invariants (ADR 0002): the namespace yields zero broker open orders
/ positions ever (asserted here + by PRD-A's ``ColdStartReconciler``);
shadow fills are typed ``execution_source = "shadow_sim"`` with explicit
``fill_model`` + ``source_bar_close_ms``; shadow ``exec_id`` is
``shadow:``-prefixed so it can never collide with a real IBKR execId.

No-submission is structural, not a runtime flag: this class has no code
path that reaches ``ib.placeOrder``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.live.divergence.bar_series_joiner import CanonicalBar
from app.engine.live.shadow_fill_simulator import PendingFill, simulate_shadow_fill


class ShadowOrderAck(IbkrOrderAck):
    """A place_order ack from the shadow adapter. Is-a ``IbkrOrderAck`` so the
    engine consumes it unchanged, but carries ``submit_mode = "shadow"`` so any
    downstream consumer expecting a real-broker ack can detect the difference.
    The discriminator is this type + ``submit_mode``, NOT the base ``status``
    field (which is IBKR's constrained vocabulary; shadow uses a neutral
    ``PreSubmitted`` since the order is accepted locally and never reaches the
    broker)."""

    submit_mode: Literal["shadow"] = "shadow"
    shadow_client_order_id: str = ""


class ShadowInvariantBreached(RuntimeError):
    """Raised when the shadow namespace is found non-empty at the broker —
    a shadow strategy must never have a real open order or position."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Pending:
    spec: IbkrOrderSpec
    source_bar: CanonicalBar | None  # None until the first bar is seen


def _to_canonical(bar: TradeBar) -> CanonicalBar:
    return CanonicalBar(
        bar_close_ms=int(bar.end_time.timestamp() * 1000),
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
    )


class NoSubmitBrokerAdapter:
    """Market-data-only adapter; synthesises shadow_sim fills, never submits."""

    def __init__(
        self,
        ib_client,
        *,
        strategy_instance_id: str,
        bot_order_namespace: str,
        account_id: str = "SHADOW",
        fill_model: str = "NEXT_BAR_OPEN",
        ib_client_id: int = 43,
        initial_cash: Decimal = Decimal("100000"),
    ) -> None:
        self._ib = ib_client
        self._strategy_instance_id = strategy_instance_id
        self._namespace = bot_order_namespace
        self._account_id = account_id
        self._fill_model = fill_model
        self._ib_client_id = ib_client_id
        self._initial_cash = initial_cash
        self._pending: list[_Pending] = []
        self._events: list[OrderEvent] = []
        self._current_bar: CanonicalBar | None = None
        self._order_seq = 0

    # ── BrokerAdapter surface ────────────────────────────────────────────

    async def fetch_account_summary(self):
        return await self._ib_account_summary()

    async def _ib_account_summary(self):
        from app.broker.ibkr.models import IbkrAccountSummary

        return IbkrAccountSummary(
            account_id=self._account_id,
            is_paper=True,
            cash_balance=float(self._initial_cash),
            net_liquidation=float(self._initial_cash),
            fetched_at_ms=1,
        )

    async def fetch_positions(self):
        from app.broker.ibkr.models import IbkrPositionsSnapshot

        return IbkrPositionsSnapshot(
            account_id=self._account_id, is_paper=True, positions=[], fetched_at_ms=1
        )

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        """Record the shadow intent against the current bar. NEVER reaches
        ``ib.placeOrder`` — that method does not appear anywhere in this path."""
        self._order_seq += 1
        # The bar the strategy acted on is the current bar; the fill prices
        # against the next bar's open once it arrives (advance_bar).
        self._pending.append(_Pending(spec=spec, source_bar=self._current_bar))
        source_ms = self._current_bar.bar_close_ms if self._current_bar is not None else 0
        return ShadowOrderAck(
            account_id=self._account_id,
            is_paper=True,
            order_id=self._order_seq,
            client_id=self._ib_client_id,
            con_id=0,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PreSubmitted",
            placed_at_ms=1,
            shadow_client_order_id=(
                f"shadow-{self._strategy_instance_id}-{source_ms}-{spec.action}"
            ),
        )

    async def cancel_open_orders(self) -> list[int]:
        """No-op: shadow has no real orders to cancel."""
        cancelled = [self._order_seq] if self._pending else []
        self._pending.clear()
        return cancelled

    # ── Replay-style engine drive hooks ──────────────────────────────────

    async def advance_bar(self, bar: TradeBar) -> None:
        """Synthesise shadow fills for orders placed on the prior bar at this
        bar's open, then mark this bar as the current (source) bar."""
        canonical = _to_canonical(bar)
        still_pending: list[_Pending] = []
        for item in self._pending:
            if item.source_bar is None:
                # Order placed before any bar was seen — defer one bar.
                item.source_bar = canonical
                still_pending.append(item)
                continue
            fill = simulate_shadow_fill(
                item.spec,
                source_bar=item.source_bar,
                next_bar=canonical,
                fill_model=self._fill_model,
                account_id=self._account_id,
                strategy_instance_id=self._strategy_instance_id,
            )
            if isinstance(fill, PendingFill):
                still_pending.append(item)
                continue
            self._events.append(self._execution_to_event(fill))
        self._pending = still_pending
        self._current_bar = canonical

    def drain_order_events(self) -> list[OrderEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def _execution_to_event(self, fill) -> OrderEvent:
        signed_qty = int(fill.fill_quantity)
        # Timestamp the fill at the simulator's ts_ms (the next bar's close, when
        # the fill is known) — not the bar's start — so the receipt agrees with
        # the simulated ExecutionRow. Carry the shadow ids through so the
        # writer preserves the broker-noncolliding invariant.
        return OrderEvent(
            order_id=self._order_seq,
            symbol=fill.symbol,
            time=datetime.fromtimestamp(fill.ts_ms / 1000, tz=UTC),
            fill_price=Decimal(str(fill.fill_price)),
            fill_quantity=signed_qty,
            direction=Direction.LONG if signed_qty > 0 else Direction.SHORT,
            fee=Decimal("0"),  # shadow has no portfolio-facing fee
            tag="ShadowFill",
            recorded_fee=None,  # shadow has no recorded broker commission
            execution_source="shadow_sim",
            fill_model=fill.fill_model,
            source_bar_close_ms=fill.source_bar_close_ms,
            exec_id=fill.exec_id,
            client_order_id=fill.client_order_id,
        )

    # ── Shadow invariant ─────────────────────────────────────────────────

    def assert_shadow_invariant(
        self, *, open_orders: Sequence[object], positions: Sequence[object]
    ) -> None:
        """Enforce ADR 0002 invariant 1: the shadow namespace yields zero
        broker open orders / positions. Raises ``ShadowInvariantBreached`` on
        any. (Broker querying is PRD-A's ``ColdStartReconciler``; this is the
        pure check it — or a caller — invokes on the gathered, namespaced
        results.)"""
        if open_orders:
            raise ShadowInvariantBreached("unexpected_open_order_at_broker")
        if positions:
            raise ShadowInvariantBreached("unexpected_position_at_broker")
