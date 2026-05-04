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
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.broker.ibkr.bars import stream_minute_bars
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.config import PAPER_PORTS
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import EquitySnapshot
from app.engine.execution.order import OrderEvent
from app.engine.framework.insight_scorer import DefaultInsightScoreFunction
from app.engine.live.config import LiveConfig
from app.engine.live.live_context import LiveContext
from app.engine.live.live_portfolio import BrokerAdapter, IbkrBrokerAdapter, LivePortfolio
from app.engine.strategy.base import Strategy

logger = logging.getLogger(__name__)


@runtime_checkable
class ReplayBrokerAdapter(BrokerAdapter, Protocol):
    """Optional fake-broker hooks used by deterministic replay tests."""

    async def advance_bar(self, bar: TradeBar) -> None: ...

    def drain_order_events(self) -> list[OrderEvent]: ...


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
    ) -> LiveRunResult:
        """Run a strategy against supplied bars or the real IBKR bar stream."""
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
        source = bars if bars is not None else stream_minute_bars(self._client, symbol)

        order_events: list[OrderEvent] = []
        retained_bars: list[TradeBar] = []
        equity_curve: list[EquitySnapshot] = []
        submitted_order_ids: list[int] = []
        previous_bar: TradeBar | None = None

        async for minute_bar in source:
            await self._process_replay_broker_bar(minute_bar)
            for event in self._drain_replay_order_events():
                portfolio.record_broker_fill(event)
                order_events.append(event)
                strategy.on_order_event(event)

            portfolio.update_reference_price(symbol, minute_bar.close)
            for consolidator in ctx.get_consolidators(symbol):
                consolidator.update(minute_bar)

            submitted = await portfolio.submit_pending_orders()
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

    async def _process_replay_broker_bar(self, bar: TradeBar) -> None:
        if isinstance(self._broker, ReplayBrokerAdapter):
            await self._broker.advance_bar(bar)

    def _drain_replay_order_events(self) -> list[OrderEvent]:
        if isinstance(self._broker, ReplayBrokerAdapter):
            return self._broker.drain_order_events()
        return []
