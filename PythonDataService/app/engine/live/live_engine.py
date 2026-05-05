"""Async live-engine driver.

Live paper fills are broker-timed, so this driver does not maintain the
backtest engine's deferred fill list. The fake broker used by the replay
gate can still emit deterministic next-minute-open fills, letting the
same strategy class prove live/backtest lifecycle parity without touching
IBKR.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from app.broker.ibkr.bars import stream_minute_bars
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import PAPER_PORTS
from app.broker.ibkr.models import IbkrMinuteBar, IbkrOrderEvent
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import EquitySnapshot
from app.engine.execution.order import Direction, OrderEvent
from app.engine.framework.insight_scorer import DefaultInsightScoreFunction
from app.engine.live.bar_adapter import trade_bars_from_ibkr
from app.engine.live.config import LiveConfig
from app.engine.live.live_context import LiveContext
from app.engine.live.live_portfolio import (
    BrokerAdapter,
    IbkrBrokerAdapter,
    LiveBrokerEventStreamError,
    LivePortfolio,
)
from app.engine.strategy.base import Strategy

logger = logging.getLogger(__name__)


_ENGINE_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class _OrderMeta:
    """Per-order context the engine needs to convert IBKR fills.

    ``IbkrOrderEvent`` carries ``order_id`` and the magnitude/price of
    the fill but no symbol, no signed quantity, and no strategy-side
    tag. The engine stamps that context in when it submits, so a fill
    on ``order_id=42`` can be expanded back to a full
    ``OrderEvent(symbol=..., fill_quantity=signed, tag=...)``.
    """

    symbol: str
    tag: str
    signed_qty: int


@runtime_checkable
class ReplayBrokerAdapter(BrokerAdapter, Protocol):
    """Optional fake-broker hooks used by deterministic replay tests."""

    async def advance_bar(self, bar: TradeBar) -> None: ...

    def drain_order_events(self) -> list[OrderEvent]: ...


@runtime_checkable
class IbkrEventAdapter(BrokerAdapter, Protocol):
    """Optional broker hooks for live IBKR fill streaming."""

    async def start_event_stream(self) -> None: ...

    async def stop_event_stream(self) -> None: ...

    def drain_broker_events(self) -> list[IbkrOrderEvent]: ...

    @property
    def stream_failure(self) -> BaseException | None: ...


@dataclass
class LiveRunResult:
    """Captured result of a finite live-engine run."""

    initial_cash: Decimal
    final_equity: Decimal
    total_fees: Decimal
    order_events: list[OrderEvent] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    bars: list[TradeBar] = field(default_factory=list)
    equity_curve: list[EquitySnapshot] = field(default_factory=list)
    insights: list = field(default_factory=list)
    insight_summary: dict = field(default_factory=dict)
    submitted_order_ids: list[int] = field(default_factory=list)
    open_positions: dict[str, int] = field(default_factory=dict)
    pending_orders: int = 0


class LiveEngine:
    """Async runtime for Strategy subclasses against a broker boundary."""

    def __init__(
        self,
        client: IbkrClient | None,
        config: LiveConfig | None = None,
        *,
        broker: BrokerAdapter | None = None,
    ) -> None:
        self._client = client
        self._config = config or LiveConfig()
        if broker is not None:
            self._broker = broker
        elif client is not None:
            self._broker = IbkrBrokerAdapter(client)
        else:
            raise ValueError("LiveEngine requires either an IbkrClient or a broker adapter.")
        # Per-order metadata captured at submit time; used to expand
        # IBKR fill events back into engine OrderEvents.
        self._order_meta: dict[int, _OrderMeta] = {}

    def _validate_paper_client(self) -> None:
        if self._client is None:
            return
        settings = self._client.settings
        if settings.mode != "paper":
            raise RuntimeError(f"LiveEngine paper runtime requires IBKR_MODE=paper, got {settings.mode!r}.")
        if settings.port not in PAPER_PORTS:
            raise RuntimeError(f"LiveEngine paper runtime requires paper port, got {settings.port}.")
        account_id = self._client.connected_account
        if account_id is None or not account_id.upper().startswith("DU"):
            raise RuntimeError(f"LiveEngine paper runtime requires a DU paper account, got {account_id!r}.")

    async def run(
        self,
        strategy: Strategy,
        bars: AsyncIterable[TradeBar] | None = None,
        *,
        ibkr_bars: AsyncIterable[IbkrMinuteBar] | None = None,
    ) -> LiveRunResult:
        """Run a strategy against supplied bars or the real IBKR bar stream.

        ``bars`` accepts engine ``TradeBar`` instances (used by replay
        tests). ``ibkr_bars`` accepts wire-typed ``IbkrMinuteBar``
        instances and is wrapped through the bar adapter — this is the
        production path's shape, exposed for tests that drive the
        engine without a live IBKR connection.
        """
        if bars is not None and ibkr_bars is not None:
            raise ValueError("supply at most one of bars or ibkr_bars")

        self._validate_paper_client()
        portfolio = LivePortfolio(self._broker)
        await portfolio.refresh_from_broker()
        initial_cash = portfolio.cash
        ctx = LiveContext(portfolio=portfolio)
        strategy.ctx = ctx
        strategy.initialize()

        if len(ctx.symbols) != 1:
            raise NotImplementedError("LiveEngine v1 supports a single symbol only")
        symbol = ctx.symbols[0]
        if bars is not None:
            source = bars
        elif ibkr_bars is not None:
            source = trade_bars_from_ibkr(ibkr_bars)
        else:
            source = trade_bars_from_ibkr(stream_minute_bars(self._client, symbol))

        order_events: list[OrderEvent] = []
        retained_bars: list[TradeBar] = []
        equity_curve: list[EquitySnapshot] = []
        submitted_order_ids: list[int] = []
        previous_bar: TradeBar | None = None
        last_force_flat_date: date | None = None
        force_flat_at = self._config.force_flat_at

        started_event_stream = False
        if isinstance(self._broker, IbkrEventAdapter):
            await self._broker.start_event_stream()
            started_event_stream = True

        try:
            async for minute_bar in source:
                self._raise_if_event_stream_failed()
                await self._process_replay_broker_bar(minute_bar)
                for event in self._drain_replay_order_events():
                    portfolio.record_broker_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)
                for event in self._drain_real_broker_order_events():
                    portfolio.record_broker_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)

                # Force-flat barrier: at most once per session date, when the
                # bar's wall-clock time crosses ``force_flat_at``, cancel the
                # runner's in-flight orders and submit a market liquidation
                # for every open position. Real-broker fills land on the
                # next print after submission; under FakeBroker they fill on
                # the next bar's open. Mirrors BacktestEngine's session-close
                # barrier in spirit; the backtest synthesizes fills at the
                # current bar's close so that path is not parity-equivalent
                # in the strict sense (documented in the Phase 5 audit), but
                # the operator-visible outcome — no position survives the
                # session — is.
                if (
                    force_flat_at is not None
                    and minute_bar.time.time() >= force_flat_at
                    and minute_bar.time.date() != last_force_flat_date
                ):
                    # Clear any orders the strategy queued earlier in this bar
                    # that have not been submitted; they would otherwise compete
                    # with the liquidation about to be sent.
                    portfolio.pending_orders.clear()
                    cancelled = await self._broker.cancel_open_orders()
                    if cancelled:
                        ctx.log(
                            f"[FORCE-FLAT] {minute_bar.time}: cancelled "
                            f"{len(cancelled)} open broker order(s) {cancelled!r}"
                        )
                    liquidations = 0
                    for sym, pos in list(portfolio.positions.items()):
                        if pos.quantity == 0:
                            continue
                        portfolio.liquidate(sym, minute_bar.end_time)
                        liquidations += 1
                    if liquidations:
                        flat_acks = await self._submit_pending_with_meta(portfolio)
                        submitted_order_ids.extend(ack.order_id for ack in flat_acks)
                        ctx.log(
                            f"[FORCE-FLAT] {minute_bar.time}: submitted "
                            f"{liquidations} liquidation order(s)"
                        )
                    strategy.on_force_flat()
                    last_force_flat_date = minute_bar.time.date()

                portfolio.update_reference_price(symbol, minute_bar.close)
                for consolidator in ctx.get_consolidators(symbol):
                    consolidator.update(minute_bar)

                submitted = await self._submit_pending_with_meta(portfolio)
                submitted_order_ids.extend(ack.order_id for ack in submitted)

                current_prices = {sym: portfolio.reference_price.get(sym, Decimal(0)) for sym in ctx.symbols}
                ctx.insight_manager.step(minute_bar.end_time, current_prices)
                equity_curve.append(
                    EquitySnapshot(
                        timestamp=minute_bar.end_time,
                        equity=portfolio.total_value(),
                        cash=portfolio.cash,
                        holdings_value=portfolio.total_value() - portfolio.cash,
                    )
                )
                retained_bars.append(minute_bar)
                previous_bar = minute_bar

            # Source exhausted. Stop the stream first so the task flushes
            # any in-flight event into the buffer, then drain. Without
            # this final pass, fills that arrive between the last per-bar
            # drain and shutdown stay buffered and never reach the
            # portfolio or strategy — common on finite test/replay
            # streams and on real-broker shutdown.
            if started_event_stream and isinstance(self._broker, IbkrEventAdapter):
                await self._broker.stop_event_stream()
                started_event_stream = False
                self._raise_if_event_stream_failed()
                for event in self._drain_real_broker_order_events():
                    portfolio.record_broker_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)
        finally:
            if started_event_stream and isinstance(self._broker, IbkrEventAdapter):
                await self._broker.stop_event_stream()

        strategy.on_end_of_algorithm()
        if previous_bar is not None:
            final_prices = {sym: portfolio.reference_price.get(sym, Decimal(0)) for sym in ctx.symbols}
            for insight in ctx.insight_manager.get_active_insights(previous_bar.end_time):
                if not insight.score.is_final_score:
                    insight.reference_value_final = final_prices.get(insight.symbol, Decimal(0))
                    DefaultInsightScoreFunction().score(insight)
                    insight.score.finalize(previous_bar.end_time)

        open_positions = {sym: pos.quantity for sym, pos in portfolio.positions.items() if pos.quantity != 0}
        return LiveRunResult(
            initial_cash=initial_cash,
            final_equity=portfolio.total_value(),
            total_fees=portfolio.total_fees,
            order_events=order_events,
            log_lines=list(ctx.log_lines),
            bars=retained_bars,
            equity_curve=equity_curve,
            insights=ctx.insight_manager.all_insights,
            insight_summary=ctx.insight_manager.get_summary().to_dict(),
            submitted_order_ids=submitted_order_ids,
            open_positions=open_positions,
            pending_orders=len(portfolio.pending_orders),
        )

    async def _submit_pending_with_meta(self, portfolio: LivePortfolio):
        """Submit queued orders and remember their per-id metadata.

        ``LivePortfolio.submit_pending_orders`` drains pending orders in
        FIFO order and returns acks in the same order. We snapshot the
        pending list first so we can pair each ack back to the originating
        ``Order`` for ``_OrderMeta`` bookkeeping. That pairing is what
        lets ``_drain_real_broker_order_events`` rebuild a full engine
        ``OrderEvent`` from a wire ``IbkrOrderEvent``.
        """
        pending_snapshot = list(portfolio.pending_orders)
        acks = await portfolio.submit_pending_orders()
        for order, ack in zip(pending_snapshot, acks, strict=True):
            self._order_meta[int(ack.order_id)] = _OrderMeta(
                symbol=order.symbol,
                tag=order.tag,
                signed_qty=int(order.quantity),
            )
        return acks

    def _raise_if_event_stream_failed(self) -> None:
        """Fail the run if the broker event-stream task died.

        Continuing to submit orders against a broker we can no longer
        receive fills from would silently desync portfolio and strategy
        state from broker reality.
        """
        if not isinstance(self._broker, IbkrEventAdapter):
            return
        failure = self._broker.stream_failure
        if failure is None:
            return
        raise LiveBrokerEventStreamError(
            "IBKR order-event stream terminated unexpectedly; aborting run"
        ) from failure

    async def _process_replay_broker_bar(self, bar: TradeBar) -> None:
        if isinstance(self._broker, ReplayBrokerAdapter):
            await self._broker.advance_bar(bar)

    def _drain_replay_order_events(self) -> list[OrderEvent]:
        if isinstance(self._broker, ReplayBrokerAdapter):
            return self._broker.drain_order_events()
        return []

    def _drain_real_broker_order_events(self) -> list[OrderEvent]:
        """Convert any pending IBKR fill events into engine OrderEvents."""
        if not isinstance(self._broker, IbkrEventAdapter):
            return []
        out: list[OrderEvent] = []
        for fill in self._broker.drain_broker_events():
            engine_event = self._convert_ibkr_fill(fill)
            if engine_event is not None:
                out.append(engine_event)
        return out

    def _convert_ibkr_fill(self, fill: IbkrOrderEvent) -> OrderEvent | None:
        """Translate one ``IbkrOrderEvent`` to an engine ``OrderEvent``.

        Returns ``None`` for non-fill events (status/cancel/error) and
        for events whose ``order_id`` was not placed by this runner —
        the latter means the foreign order leaked through the adapter
        ownership filter and is treated as a no-op so we never apply a
        stranger's fill to our portfolio.
        """
        if fill.event_type != "fill":
            return None
        meta = self._order_meta.get(int(fill.order_id))
        if meta is None:
            logger.warning(
                "Dropping IBKR fill for unknown order_id=%s (not placed by this runner)",
                fill.order_id,
            )
            return None
        magnitude = int(fill.fill_quantity or 0)
        if magnitude == 0:
            return None
        signed_fill_qty = magnitude if meta.signed_qty > 0 else -magnitude
        price_source = fill.last_fill_price if fill.last_fill_price is not None else fill.avg_fill_price
        if price_source is None:
            logger.warning(
                "Dropping IBKR fill for order_id=%s with no fill price",
                fill.order_id,
            )
            return None
        fill_price = Decimal(str(price_source))
        direction = Direction.LONG if signed_fill_qty > 0 else Direction.SHORT
        fill_time = datetime.fromtimestamp(fill.ts_ms / 1000, tz=UTC).astimezone(_ENGINE_TZ)
        # Commission is reported separately by IBKR (commissionReport
        # callback, not order-status events). Fee-tolerance reconciliation
        # is the Phase-9 paper-vs-broker step; keep this fee zero here so
        # the live receipt does not silently invent a commission number.
        return OrderEvent(
            order_id=int(fill.order_id),
            symbol=meta.symbol,
            time=fill_time,
            fill_price=fill_price,
            fill_quantity=signed_fill_qty,
            direction=direction,
            fee=Decimal("0"),
            tag=meta.tag,
        )
