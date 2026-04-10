"""BacktestEngine — lifecycle orchestration.

Pulls minute bars from a data source, pushes them through registered
consolidators, invokes strategy event handlers on consolidated bars, and
processes pending orders through the fill model.

Designed to reproduce LEAN's backtest semantics bit-exactly for the
SpyEmaCrossoverAlgorithm. See docs/lean-engine-implementation-plan.md §2
for the reproducibility details this engine guarantees.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode, OrderEvent
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import Strategy, StrategyContext


@dataclass
class BacktestResult:
    initial_cash: Decimal
    final_equity: Decimal
    net_profit: Decimal
    total_fees: Decimal
    order_events: list[OrderEvent] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)


class BacktestEngine:
    def __init__(
        self,
        data_source: LeanMinuteDataReader | LeanDailyDataReader,
        fill_model: Optional[FillModel] = None,
    ) -> None:
        self.data_source = data_source
        self.fill_model = fill_model or FillModel(mode=FillMode.SIGNAL_BAR_CLOSE)

    def run(self, strategy: Strategy) -> BacktestResult:
        # ------------------------------------------------------------------
        # 1. Let the strategy configure itself.
        # ------------------------------------------------------------------
        portfolio = Portfolio(initial_cash=Decimal(1))  # placeholder; set below
        ctx = StrategyContext(portfolio=portfolio)
        strategy.ctx = ctx
        strategy.initialize()

        if strategy.start_date is None or strategy.end_date is None:
            raise RuntimeError(
                "Strategy must call set_start_date and set_end_date in initialize()"
            )
        if not ctx.symbols:
            raise RuntimeError(
                "Strategy must call ctx.add_equity() in initialize() for at least one symbol"
            )

        # Now that initialize has run, reset portfolio cash to the configured amount.
        portfolio.initial_cash = strategy.initial_cash
        portfolio.cash = strategy.initial_cash

        order_events: list[OrderEvent] = []

        # ------------------------------------------------------------------
        # 2. Main loop over minute bars.
        # ------------------------------------------------------------------
        start_date: date = strategy.start_date.date()
        end_date: date = strategy.end_date.date()
        assert strategy.end_date is not None

        # For each symbol, iterate its minute bars in order. For a single-
        # symbol strategy (SPY), this is the natural order. Multi-symbol
        # support would require merging streams by time — out of scope for
        # Phase 1.
        if len(ctx.symbols) != 1:
            raise NotImplementedError(
                "Phase 1 engine supports a single symbol only"
            )
        symbol = ctx.symbols[0]

        # Keep a "previous minute bar" so that a NEXT_BAR_OPEN fill can use
        # the bar immediately after the signal bar.
        pending_fills: list[tuple[object, TradeBar]] = []  # (order, signal_bar)

        previous_minute_bar: TradeBar | None = None
        for minute_bar in self.data_source.iter_bars(symbol, start_date, end_date):
            # Update portfolio reference price with the latest close.
            portfolio.update_reference_price(symbol, minute_bar.close)

            # ----- Fill any deferred NEXT_BAR_OPEN orders with this bar as next_bar
            if pending_fills and self.fill_model.mode == FillMode.NEXT_BAR_OPEN:
                still_pending: list[tuple[object, TradeBar]] = []
                for order, signal_bar in pending_fills:
                    event = self.fill_model.fill_market_order(
                        order, signal_bar, next_bar=minute_bar
                    )
                    if event is None:
                        still_pending.append((order, signal_bar))
                    else:
                        portfolio.apply_fill(event)
                        order_events.append(event)
                        strategy.on_order_event(event)
                pending_fills = still_pending

            # ----- Feed consolidators. Consolidators will invoke strategy handlers
            #       when a bar fires. The strategy may submit orders inside those
            #       handlers; we drain those immediately below.
            consolidators = ctx.get_consolidators(symbol)
            fired_any = False
            for consolidator in consolidators:
                fired = consolidator.update(minute_bar)
                if fired is not None:
                    fired_any = True

            # ----- Drain any pending orders the strategy just submitted.
            if portfolio.pending_orders:
                # The "signal bar" for a SIGNAL_BAR_CLOSE fill is the latest
                # fired consolidated bar. For multi-consolidator strategies
                # this gets ambiguous, but Phase 1 uses one consolidator.
                assert len(consolidators) == 1
                signal_bar = self._last_fired(consolidators[0])
                assert signal_bar is not None, (
                    "Strategy submitted an order but no consolidated bar was available"
                )
                for order in portfolio.drain_pending():
                    if self.fill_model.mode == FillMode.SIGNAL_BAR_CLOSE:
                        event = self.fill_model.fill_market_order(
                            order, signal_bar, next_bar=None
                        )
                        assert event is not None
                        portfolio.apply_fill(event)
                        order_events.append(event)
                        strategy.on_order_event(event)
                    else:
                        # Defer until the next minute bar.
                        pending_fills.append((order, signal_bar))

            previous_minute_bar = minute_bar

        # ------------------------------------------------------------------
        # 3. Finalize.
        # ------------------------------------------------------------------
        strategy.on_end_of_algorithm()
        final_equity = portfolio.total_value()
        return BacktestResult(
            initial_cash=portfolio.initial_cash,
            final_equity=final_equity,
            net_profit=final_equity - portfolio.initial_cash,
            total_fees=portfolio.total_fees,
            order_events=order_events,
            log_lines=list(ctx.log_lines),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _last_fired(consolidator) -> TradeBar | None:
        """Return the most recently-emitted consolidated bar.

        The consolidator stores this internally as ``_last_emit`` (the start
        time) but not the bar itself. We instead capture it via a closure
        on the strategy side: StrategyContext's registered handler stores the
        bar. Here we just return the attribute set there.
        """
        return getattr(consolidator, "_last_fired_bar", None)
