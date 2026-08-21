"""Retained-bar-backed broker port for one isolated synthetic Clerk authority."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.services.jsonl_wal import JsonlWal
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.timestamps import now_ms_utc

SYNTHETIC_BROKER_ID = "synthetic"
SYNTHETIC_CAPABILITIES = BrokerCapabilities(
    broker=SYNTHETIC_BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    supported_order_types=("market", "limit"),
    data_feed="retained_source_bars",
    bars_may_gap=False,
    max_stream_symbols=0,
    max_concurrent_streams=0,
    rest_rate_limit_per_min=0,
)
_ORDER_LEDGER_FILENAME = "simulated_orders.jsonl"


class SimulatedPriceUnavailableError(RuntimeError):
    """No retained bar exists from which a synthetic fill may be derived."""


class _SimulatedOrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    order: BrokerOrder


def _corrupt_order_ledger(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Synthetic order ledger corrupt at {path}: {detail}")


class SyntheticBroker:
    """Durable immediate-fill port that derives prices only from retained bars.

    The Clerk remains the custody authority.  This adapter supplies the
    broker-shaped acknowledgement and fills it needs without contacting Alpaca
    or reading a second market-data source.
    """

    broker_id = SYNTHETIC_BROKER_ID

    def __init__(self, *, account_id: str, source_bars: SourceBarLedger | None = None) -> None:
        self._account_id = require_synthetic_account_id(account_id)
        self._source_bars = source_bars
        self._orders: JsonlWal[_SimulatedOrderRecord] | None = (
            None
            if source_bars is None
            else JsonlWal(
                source_bars.path.with_name(_ORDER_LEDGER_FILENAME),
                record_model=_SimulatedOrderRecord,
                corrupt_error=_corrupt_order_ledger,
                seq_of=lambda row: row.seq,
                label="simulated_order",
                trusted_root=source_bars.path.parent,
            )
        )

    def capabilities(self) -> BrokerCapabilities:
        return SYNTHETIC_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        observed_at_ms = now_ms_utc()
        return BrokerAccountSnapshot(
            broker=self.broker_id,
            account_id=self._account_id,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=0.0,
            equity=0.0,
            buying_power=0.0,
            portfolio_value=0.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=observed_at_ms,
            observed_at_ms=observed_at_ms,
        )

    async def list_positions(self) -> list[BrokerPosition]:
        quantities: dict[str, tuple[float, float]] = {}
        for order in self._latest_orders():
            if order.filled_quantity <= 0 or order.filled_avg_price is None:
                continue
            signed = order.filled_quantity if order.side.lower() == "buy" else -order.filled_quantity
            quantity, cost = quantities.get(order.symbol, (0.0, 0.0))
            quantities[order.symbol] = (quantity + signed, cost + signed * order.filled_avg_price)
        observed_at_ms = now_ms_utc()
        return [
            BrokerPosition(
                broker=self.broker_id,
                symbol=symbol,
                asset_id=None,
                asset_class="us_equity",
                quantity=quantity,
                side="long" if quantity > 0 else "short",
                average_entry_price=(abs(cost / quantity) if quantity else 0.0),
                market_value=abs(cost),
                cost_basis=abs(cost),
                current_price=None,
                unrealized_pl=0.0,
                unrealized_plpc=None,
                observed_at_ms=observed_at_ms,
            )
            for symbol, (quantity, cost) in quantities.items()
            if quantity != 0
        ]

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        orders = self._latest_orders()
        if status is not None:
            orders = [order for order in orders if order.status == status]
        if after_ms is not None:
            orders = [order for order in orders if (order.updated_at_ms or 0) >= after_ms]
        return list(reversed(orders))[:limit]

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        del after_ms, limit
        return []

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        del status, limit
        return []

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        del symbol
        return None

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        observed_at_ms = now_ms_utc()
        return BrokerClockEvidence(
            broker=self.broker_id,
            is_open=False,
            vendor_timestamp_ms=observed_at_ms,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=observed_at_ms,
        )

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        del history_range
        return BrokerPortfolioHistory(
            timestamps=[],
            equity=[],
            profit_loss=[],
            base_value=0.0,
            timeframe="synthetic",
        )

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        if self._source_bars is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        bar = self._source_bars.latest_for_symbol(leg.symbol)
        if bar is None:
            raise SimulatedPriceUnavailableError(
                f"No retained source bar exists for {leg.symbol!r}; refusing a synthetic fill."
            )
        existing = await self.get_order_by_client_order_id(client_order_id)
        if existing is not None:
            return existing
        filled_at_ms = bar.end_ms
        order = BrokerOrder(
            broker=self.broker_id,
            order_id=f"sim-order:{client_order_id}",
            client_order_id=client_order_id,
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side,
            order_type="market",
            time_in_force="day",
            quantity=leg.quantity,
            filled_quantity=leg.quantity,
            limit_price=None,
            stop_price=None,
            filled_avg_price=float(bar.close),
            status="filled",
            submitted_at_ms=filled_at_ms,
            created_at_ms=filled_at_ms,
            updated_at_ms=filled_at_ms,
            filled_at_ms=filled_at_ms,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[
                {
                    "event_type": "fill",
                    "occurred_at_ms": filled_at_ms,
                    "price": float(bar.close),
                    "quantity": leg.quantity,
                    "execution_id": f"sim-execution:{client_order_id}",
                }
            ],
            observed_at_ms=now_ms_utc(),
        )
        self._append_order(order)
        return order

    async def cancel(self, order_id: str) -> None:
        for order in self._latest_orders():
            if order.order_id != order_id:
                continue
            if order.status == "filled":
                return
            now = now_ms_utc()
            self._append_order(order.model_copy(update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}))
            return

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self._latest_orders() if order.client_order_id == client_order_id),
            None,
        )

    def _latest_orders(self) -> list[BrokerOrder]:
        if self._orders is None:
            return []
        latest: dict[str, BrokerOrder] = {}
        for row in self._orders.read_all():
            latest[row.order.client_order_id or row.order.order_id] = row.order
        return list(latest.values())

    def _append_order(self, order: BrokerOrder) -> None:
        if self._orders is None:
            return
        self._orders.append(_SimulatedOrderRecord(seq=self._orders.allocate_seq(), order=order))


__all__ = [
    "SYNTHETIC_BROKER_ID",
    "SYNTHETIC_CAPABILITIES",
    "SimulatedPriceUnavailableError",
    "SyntheticBroker",
]
