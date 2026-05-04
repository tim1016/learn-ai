"""Deterministic fake broker for live-runtime tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrOrderAck,
    IbkrOrderSpec,
    IbkrPositionsSnapshot,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent


@dataclass
class _PendingSpec:
    order_id: int
    spec: IbkrOrderSpec


class FakeBroker:
    """In-memory broker that fills market orders at the next minute open."""

    def __init__(self, *, initial_cash: Decimal = Decimal("100000")) -> None:
        self.cash = initial_cash
        self.positions: dict[str, int] = {}
        self.avg_price: dict[str, Decimal] = {}
        self.total_fees = Decimal(0)
        self.orders: list[IbkrOrderSpec] = []
        self.position_snapshot: IbkrPositionsSnapshot | None = None
        self._pending: list[_PendingSpec] = []
        self._events: list[OrderEvent] = []

    async def fetch_account_summary(self) -> IbkrAccountSummary:
        return IbkrAccountSummary(
            account_id="DU123",
            is_paper=True,
            cash_balance=float(self.cash),
            net_liquidation=float(self.cash),
            fetched_at_ms=1,
        )

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        if self.position_snapshot is not None:
            return self.position_snapshot
        return IbkrPositionsSnapshot(
            account_id="DU123",
            is_paper=True,
            positions=[],
            fetched_at_ms=1,
        )

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        order_id = self._resolve_order_id(spec)
        self.orders.append(spec)
        self._pending.append(_PendingSpec(order_id=order_id, spec=spec))
        return IbkrOrderAck(
            account_id="DU123",
            is_paper=True,
            order_id=order_id,
            client_id=1,
            con_id=756733,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PendingSubmit",
            placed_at_ms=1,
        )

    async def advance_bar(self, bar: TradeBar) -> None:
        pending = list(self._pending)
        self._pending.clear()
        for item in pending:
            spec = item.spec
            signed_qty = int(spec.quantity) if spec.action == "BUY" else -int(spec.quantity)
            direction = Direction.LONG if signed_qty > 0 else Direction.SHORT
            event = OrderEvent(
                order_id=item.order_id,
                symbol=spec.symbol,
                time=bar.time,
                fill_price=bar.open,
                fill_quantity=signed_qty,
                direction=direction,
                fee=Decimal("1.00"),
                tag="SetHoldings" if signed_qty > 0 else "Liquidate",
            )
            self._apply_fill(event)
            self._events.append(event)

    def drain_order_events(self) -> list[OrderEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def _resolve_order_id(self, spec: IbkrOrderSpec) -> int:
        if spec.client_order_id and spec.client_order_id.startswith("live-"):
            return int(spec.client_order_id.removeprefix("live-"))
        return len(self.orders) + 1

    def _apply_fill(self, event: OrderEvent) -> None:
        qty = self.positions.get(event.symbol, 0) + event.fill_quantity
        self.positions[event.symbol] = qty
        if qty == 0:
            self.avg_price[event.symbol] = Decimal(0)
        else:
            self.avg_price[event.symbol] = event.fill_price
        self.cash -= Decimal(event.fill_quantity) * event.fill_price
        self.cash -= event.fee
        self.total_fees += event.fee


async def iter_bars(bars: Iterable[TradeBar]) -> AsyncIterator[TradeBar]:
    for bar in bars:
        yield bar
