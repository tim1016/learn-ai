"""Retained-bar-backed broker port for one isolated synthetic Clerk authority.

The ``sim:`` world's whole difference from the shadow world is its fill
model: a leg either transacts at the decision bar's close or is cancelled on
the spot (ruling R9). Everything durable — the order WAL, the decision-bar
binding, the position projection — is ``synthesized_orders.py``, shared with
``shadow_broker.py``; the position projection's provenance lives there.
"""

from __future__ import annotations

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.clerk.fill_models import immediate_fill_price
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.synthesized_orders import (
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOrderLedger,
    project_positions,
)
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
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger
from app.utils.timestamps import now_ms_utc

SYNTHETIC_BROKER_ID = "synthetic"
SYNTHETIC_CAPABILITIES = BrokerCapabilities(
    broker=SYNTHETIC_BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    extended_hours_window=ALPACA_EXTENDED_HOURS_WINDOW,
    supported_order_types=("market", "limit"),
    data_feed="retained_source_bars",
    bars_may_gap=False,
    max_stream_symbols=0,
    max_concurrent_streams=0,
    rest_rate_limit_per_min=0,
)
# The sim world's historical name for the shared binding error.
SyntheticBarBindingError = SynthesizedBarBindingError


class SimulatedPriceUnavailableError(RuntimeError):
    """No retained bar exists from which a synthetic fill may be derived."""


class SyntheticBroker:
    """Durable immediate-fill port that derives prices only from retained bars.

    A market leg, or a limit leg marketable against the decision bar's close,
    fills immediately at that close. The sim world cannot rest an order: a
    non-marketable limit is canceled on the spot with zero fills (ruling R9)
    rather than waiting for a later bar to touch it.

    The Clerk remains the custody authority.  This adapter supplies the
    broker-shaped acknowledgement and fills it needs without contacting Alpaca
    or reading a second market-data source.
    """

    broker_id = SYNTHETIC_BROKER_ID

    def __init__(self, *, account_id: str, source_bars: SourceBarLedger | None = None) -> None:
        self._account_id = require_synthetic_account_id(account_id)
        self._source_bars = source_bars
        self._ledger: SynthesizedOrderLedger | None = (
            None
            if source_bars is None
            else SynthesizedOrderLedger.beside_source_bars(
                account_id=self._account_id, source_bars=source_bars, label="simulated_order"
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
        return synthesized_positions(self.broker_id, self._latest_orders())

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        return filter_synthesized_orders(self._latest_orders(), status=status, limit=limit, after_ms=after_ms)

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
        """Bind one minted Clerk order identity to one exact retained decision bar."""
        if self._ledger is None:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding requires an authority-scoped retained-bar ledger."
            )
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar)

    async def submit(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
        retained_bar: RetainedSourceBar | None = None,
    ) -> BrokerOrder:
        if self._ledger is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        with self._ledger.transaction() as records:
            existing = self._ledger.find_order(records, client_order_id)
            if existing is not None:
                self._ledger.consume_bound_bar(client_order_id)
                return existing
            bar = self._submission_bar(
                client_order_id=client_order_id,
                symbol=leg.symbol,
                retained_bar=retained_bar,
            )
            order = self._resolved_order(leg, client_order_id=client_order_id, bar=bar)
            self._ledger.append_locked(
                records,
                order=order,
                leg=leg,
                anchor=SynthesizedAnchor(
                    fill_model="decision_bar_close",
                    evidence_account_id=bar.account_id,
                    provider=bar.provider,
                    bar_identity=bar.bar_identity,
                    bar_ref=bar.bar_ref,
                    decision_bar_start_ms=bar.start_ms,
                    decision_bar_end_ms=bar.end_ms,
                    fill_bar_ref=bar.bar_ref if order.status == "filled" else None,
                ),
            )
            return order

    async def cancel(self, order_id: str) -> None:
        if self._ledger is None:
            return
        with self._ledger.transaction() as records:
            record = next(
                (row for row in self._ledger.latest_records_from(records).values() if row.order.order_id == order_id),
                None,
            )
            if record is None or record.order.status == "filled":
                return
            now = now_ms_utc()
            self._ledger.append_locked(
                records,
                order=record.order.model_copy(
                    update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}
                ),
                leg=record.leg,
                anchor=record.anchor,
            )

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self._latest_orders() if order.client_order_id == client_order_id),
            None,
        )

    def _latest_orders(self) -> list[BrokerOrder]:
        return [] if self._ledger is None else self._ledger.latest_orders()

    def _submission_bar(
        self,
        *,
        client_order_id: str,
        symbol: str,
        retained_bar: RetainedSourceBar | None,
    ) -> RetainedSourceBar:
        assert self._ledger is not None and self._source_bars is not None
        bound = self._ledger.consume_bound_bar(client_order_id)
        candidate = retained_bar if retained_bar is not None else bound
        if candidate is not None:
            return self._ledger.verified_retained_bar(candidate, symbol=symbol)
        latest = self._source_bars.latest_for_symbol(symbol)
        if latest is None:
            raise SimulatedPriceUnavailableError(
                f"No retained source bar exists for {symbol!r}; refusing a synthetic fill."
            )
        return latest

    def _resolved_order(self, leg: BrokerOrderLeg, *, client_order_id: str, bar: RetainedSourceBar) -> BrokerOrder:
        """The order this leg becomes against one decision bar: filled, or cancelled unfilled (ruling R9)."""
        return shape_immediate_order(
            leg,
            client_order_id=client_order_id,
            bar=bar,
            broker_id=self.broker_id,
            id_prefix="sim",
            observed_at_ms=now_ms_utc(),
        )


def shape_immediate_order(
    leg: BrokerOrderLeg,
    *,
    client_order_id: str,
    bar: RetainedSourceBar,
    broker_id: str,
    id_prefix: str,
    observed_at_ms: int,
) -> BrokerOrder:
    """One decision bar, one answer: filled at its close, or cancelled unfilled.

    The fill decision itself belongs to ``fill_models`` — a second copy of
    "would this have transacted?" living here is exactly how the sim world and
    the shadow port drift apart. This function only shapes the resulting
    ``BrokerOrder``; both no-submit worlds call it for a regular-session leg.
    """
    at_ms = bar.end_ms
    fill = immediate_fill_price(leg, bar.close)
    filled = fill is not None
    return BrokerOrder(
        broker=broker_id,
        order_id=f"{id_prefix}-order:{client_order_id}",
        client_order_id=client_order_id,
        symbol=leg.symbol,
        asset_class="us_equity",
        side=leg.side,
        order_type=str(leg.order_type),
        time_in_force=str(leg.time_in_force),
        quantity=leg.quantity,
        limit_price=leg.limit_price,
        stop_price=None,
        extended_hours=leg.extended_hours,
        submitted_at_ms=at_ms,
        created_at_ms=at_ms,
        updated_at_ms=at_ms,
        expired_at_ms=None,
        observed_at_ms=observed_at_ms,
        filled_quantity=leg.quantity if filled else 0.0,
        filled_avg_price=float(fill) if fill is not None else None,
        status="filled" if filled else "canceled",
        filled_at_ms=at_ms if filled else None,
        canceled_at_ms=None if filled else at_ms,
        events=(
            [
                {
                    "event_type": "fill",
                    "occurred_at_ms": at_ms,
                    "price": float(fill),
                    "quantity": leg.quantity,
                    "execution_id": f"{id_prefix}-execution:{client_order_id}",
                }
            ]
            if fill is not None
            else []
        ),
    )


def synthesized_positions(broker_id: str, orders: list[BrokerOrder]) -> list[BrokerPosition]:
    """Shape the canonical projection as broker positions (shared with the shadow read port)."""
    quantities = project_positions(orders)
    observed_at_ms = now_ms_utc()
    return [
        BrokerPosition(
            broker=broker_id,
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


def filter_synthesized_orders(
    orders: list[BrokerOrder],
    *,
    status: str | None,
    limit: int | None,
    after_ms: int | None,
) -> list[BrokerOrder]:
    """The read port's order filter, newest first (shared with the shadow read port)."""
    if status is not None:
        orders = [order for order in orders if order.status == status]
    if after_ms is not None:
        orders = [order for order in orders if (order.updated_at_ms or 0) >= after_ms]
    return list(reversed(orders))[:limit]


__all__ = [
    "SYNTHETIC_BROKER_ID",
    "SYNTHETIC_CAPABILITIES",
    "SimulatedPriceUnavailableError",
    "SyntheticBarBindingError",
    "SyntheticBroker",
    "filter_synthesized_orders",
    "shape_immediate_order",
    "synthesized_positions",
]
