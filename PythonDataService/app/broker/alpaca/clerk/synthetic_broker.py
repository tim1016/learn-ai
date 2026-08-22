"""Retained-bar-backed broker port for one isolated synthetic Clerk authority.

Math Provenance Contract
------------------------
Formula: for each symbol, the projected position is an average-cost fold of
durable synthetic fills. Same-direction fills add signed entry notional;
reductions retain the prior average cost for the remaining quantity; a flip
opens only the residual at the flip fill price. A position is emitted iff
``position_quantity_is_nonzero(quantity)``.
Reference: average-cost broker position convention, recorded in
``docs/references/synthetic-broker-position-projection.md``.
Canonical implementation: ``_project_positions`` in this module.
Validated against: ``tests/services/test_source_bar_ledger.py`` exact
buy/reduce/add/flip parity fixture (``atol=0``, ``rtol=0``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
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
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.advisory_lock import advisory_file_lock
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
_ORDER_LOCKS: dict[str, threading.Lock] = {}
_ORDER_LOCKS_GUARD = threading.Lock()


class SimulatedPriceUnavailableError(RuntimeError):
    """No retained bar exists from which a synthetic fill may be derived."""


class SyntheticBarBindingError(RuntimeError):
    """A proposed synthetic fill is not bound to its exact retained source bar."""


class _SimulatedOrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    order: BrokerOrder


def _corrupt_order_ledger(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Synthetic order ledger corrupt at {path}: {detail}")


def _order_lock(path: Path) -> threading.Lock:
    """Return the process-local half of one order-ledger transaction lock."""
    key = str(path)
    with _ORDER_LOCKS_GUARD:
        lock = _ORDER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ORDER_LOCKS[key] = lock
        return lock


class SyntheticBroker:
    """Durable immediate-fill port that derives prices only from retained bars.

    The Clerk remains the custody authority.  This adapter supplies the
    broker-shaped acknowledgement and fills it needs without contacting Alpaca
    or reading a second market-data source.
    """

    broker_id = SYNTHETIC_BROKER_ID

    def __init__(self, *, account_id: str, source_bars: SourceBarLedger | None = None) -> None:
        self._account_id = require_synthetic_account_id(account_id)
        if source_bars is not None and source_bars.account_id != self._account_id:
            raise SyntheticBarBindingError(
                "Synthetic broker and retained source-bar ledger must share one account authority."
            )
        self._source_bars = source_bars
        self._bound_bars: dict[str, RetainedSourceBar] = {}
        self._binding_lock = threading.Lock()
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
        quantities = _project_positions(self._latest_orders())
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
            if position_quantity_is_nonzero(quantity)
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

    def bind_evaluated_bar(self, client_order_id: str, retained_bar: RetainedSourceBar) -> None:
        """Bind one minted Clerk order identity to one exact retained decision bar.

        The binding is consumed by that exact ``client_order_id`` on submit.
        Rebinding the same id deterministically replaces an unconsumed binding,
        while a later retained bar for the same symbol cannot replace it.
        """
        if not client_order_id:
            raise SyntheticBarBindingError("Synthetic bar binding requires a client order id.")
        canonical = self._verified_retained_bar(retained_bar, symbol=None)
        with self._binding_lock:
            self._bound_bars[client_order_id] = canonical

    async def submit(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
        retained_bar: RetainedSourceBar | None = None,
    ) -> BrokerOrder:
        if self._source_bars is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        with self._order_transaction() as records:
            existing = self._find_order(records, client_order_id)
            if existing is not None:
                self._consume_bound_bar(client_order_id)
                return existing
            bar = self._submission_bar(
                client_order_id=client_order_id,
                symbol=leg.symbol,
                retained_bar=retained_bar,
            )
            order = self._filled_order(leg, client_order_id=client_order_id, bar=bar)
            self._append_order_locked(order, records)
            return order

    async def cancel(self, order_id: str) -> None:
        with self._order_transaction() as records:
            for order in self._latest_orders_from_records(records):
                if order.order_id != order_id:
                    continue
                if order.status == "filled":
                    return
                now = now_ms_utc()
                self._append_order_locked(
                    order.model_copy(
                        update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}
                    ),
                    records,
                )
                return

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self._latest_orders() if order.client_order_id == client_order_id),
            None,
        )

    def _latest_orders(self) -> list[BrokerOrder]:
        if self._orders is None:
            return []
        return self._latest_orders_from_records(self._orders.read_all())

    @staticmethod
    def _latest_orders_from_records(records: list[_SimulatedOrderRecord]) -> list[BrokerOrder]:
        latest: dict[str, BrokerOrder] = {}
        for row in records:
            latest[row.order.client_order_id or row.order.order_id] = row.order
        return list(latest.values())

    def _append_order(self, order: BrokerOrder) -> None:
        if self._orders is None:
            return
        with self._order_transaction() as records:
            self._append_order_locked(order, records)

    @contextmanager
    def _order_transaction(self) -> Iterator[list[_SimulatedOrderRecord]]:
        """Serialize a full order-ledger read/check/append across threads and processes."""
        if self._orders is None:
            yield []
            return
        with _order_lock(self._orders.path), advisory_file_lock(self._orders.path):
            yield self._orders.read_all()

    def _append_order_locked(
        self,
        order: BrokerOrder,
        records: list[_SimulatedOrderRecord],
    ) -> None:
        if self._orders is None:
            return
        next_seq = records[-1].seq + 1 if records else 1
        # A sibling broker instance may have appended since this instance's
        # previous write. Refresh the WAL's sequence cache while holding the
        # cross-process transaction lock so it cannot reuse a sequence.
        self._orders._next_seq = next_seq
        self._orders.append(_SimulatedOrderRecord(seq=next_seq, order=order))

    def _submission_bar(
        self,
        *,
        client_order_id: str,
        symbol: str,
        retained_bar: RetainedSourceBar | None,
    ) -> RetainedSourceBar:
        bound = self._consume_bound_bar(client_order_id)
        candidate = retained_bar if retained_bar is not None else bound
        if candidate is not None:
            return self._verified_retained_bar(candidate, symbol=symbol)
        if self._source_bars is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        latest = self._source_bars.latest_for_symbol(symbol)
        if latest is None:
            raise SimulatedPriceUnavailableError(
                f"No retained source bar exists for {symbol!r}; refusing a synthetic fill."
            )
        return latest

    def _consume_bound_bar(self, client_order_id: str) -> RetainedSourceBar | None:
        with self._binding_lock:
            return self._bound_bars.pop(client_order_id, None)

    def _verified_retained_bar(
        self,
        retained_bar: RetainedSourceBar,
        *,
        symbol: str | None,
    ) -> RetainedSourceBar:
        if self._source_bars is None:
            raise SyntheticBarBindingError(
                "Synthetic bar binding requires an authority-scoped retained-bar ledger."
            )
        if retained_bar.account_id != self._account_id:
            raise SyntheticBarBindingError(
                "Synthetic bar binding belongs to a different account authority."
            )
        if symbol is not None and retained_bar.symbol != symbol:
            raise SyntheticBarBindingError(
                "Synthetic bar binding does not match the submitted order symbol."
            )
        persisted = self._source_bars.by_identity(retained_bar.bar_identity)
        if persisted != retained_bar:
            raise SyntheticBarBindingError(
                "Synthetic bar binding is not the exact retained source-bar observation."
            )
        return persisted

    def _filled_order(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
        bar: RetainedSourceBar,
    ) -> BrokerOrder:
        filled_at_ms = bar.end_ms
        return BrokerOrder(
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

    def _find_order(
        self,
        records: list[_SimulatedOrderRecord],
        client_order_id: str,
    ) -> BrokerOrder | None:
        return next(
            (
                order
                for order in self._latest_orders_from_records(records)
                if order.client_order_id == client_order_id
            ),
            None,
        )


def _project_positions(orders: list[BrokerOrder]) -> dict[str, tuple[float, float]]:
    """Fold fills into canonical average-cost ``(quantity, signed_notional)``.

    The result intentionally contains signed notional: a long's notional is
    positive and a short's is negative. That representation makes both the
    same-direction weighted average and a side-flip's residual opening price
    exact at the broker model's float boundary.
    """
    positions: dict[str, tuple[float, float]] = {}
    for order in orders:
        if order.filled_quantity <= 0 or order.filled_avg_price is None:
            continue
        signed_fill = order.filled_quantity if order.side.lower() == "buy" else -order.filled_quantity
        quantity, notional = positions.get(order.symbol, (0.0, 0.0))
        if not position_quantity_is_nonzero(quantity) or quantity * signed_fill > 0:
            positions[order.symbol] = (
                quantity + signed_fill,
                notional + signed_fill * order.filled_avg_price,
            )
            continue

        next_quantity = quantity + signed_fill
        if not position_quantity_is_nonzero(next_quantity):
            positions[order.symbol] = (0.0, 0.0)
            continue
        if quantity * next_quantity > 0:
            average_entry_price = abs(notional / quantity)
            positions[order.symbol] = (next_quantity, next_quantity * average_entry_price)
            continue
        positions[order.symbol] = (next_quantity, next_quantity * order.filled_avg_price)
    return positions


__all__ = [
    "SYNTHETIC_BROKER_ID",
    "SYNTHETIC_CAPABILITIES",
    "SimulatedPriceUnavailableError",
    "SyntheticBarBindingError",
    "SyntheticBroker",
]
