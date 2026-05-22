"""BacktestEngine — lifecycle orchestration.

Pulls minute bars from a data source, pushes them through registered
consolidators, invokes strategy event handlers on consolidated bars, and
processes pending orders through the fill model.

Designed to reproduce LEAN's backtest semantics bit-exactly for the
SpyEmaCrossoverAlgorithm. See docs/lean-engine-implementation-plan.md §2
for the reproducibility details this engine guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.fill_model import DEFERRED_FILL_MODES, FillModel
from app.engine.execution.intrabar_resolver import (
    IntrabarOutcome,
    resolve_bracket_pessimistic,
)
from app.engine.execution.order import Direction, FillMode, Order, OrderEvent, OrderType
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.sizing import SimpleFloorSizing, SizingModel
from app.engine.strategy.base import Strategy, StrategyContext


@dataclass
class EquitySnapshot:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    holdings_value: Decimal


@dataclass
class _ActiveBracket:
    """Engine-internal bracket watcher for an open position."""

    entry_order_id: int
    symbol: str
    direction: Direction
    quantity: int  # signed — matches the entry fill_quantity
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    fill_time: datetime


@dataclass
class BacktestResult:
    initial_cash: Decimal
    final_equity: Decimal
    net_profit: Decimal
    total_fees: Decimal
    order_events: list[OrderEvent] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    # Retained bar data for LEAN statistics computation. Each entry is a
    # consolidated (or raw daily) bar that was actually iterated.
    bars: list[TradeBar] = field(default_factory=list)
    equity_curve: list[EquitySnapshot] = field(default_factory=list)
    # Phase 1: Insight tracking — all insights emitted during the backtest,
    # scored after their prediction period expires.
    insights: list = field(default_factory=list)
    insight_summary: dict = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        data_source: LeanMinuteDataReader | LeanDailyDataReader,
        fill_model: FillModel | None = None,
        execution_config: ExecutionConfig | None = None,
        sizing_model: SizingModel | None = None,
    ) -> None:
        self.data_source = data_source
        self.execution_config = execution_config or ExecutionConfig()
        # An explicit ``fill_model`` wins over one derived from the
        # config — keeps every existing call site (and the LEAN bit-
        # exact tests that pass a bespoke FillModel) working unchanged.
        self.fill_model = fill_model or self.execution_config.build_fill_model()
        # Position sizing for set_holdings. Defaults to the historical
        # plain-floor policy; LEAN-pinned callers (cross_runner) pass
        # LeanSetHoldingsSizing to reproduce LEAN's buffered share count.
        self.sizing_model = sizing_model or SimpleFloorSizing()

    def run(self, strategy: Strategy) -> BacktestResult:
        # ------------------------------------------------------------------
        # 1. Let the strategy configure itself.
        # ------------------------------------------------------------------
        portfolio = Portfolio(initial_cash=Decimal(1))  # placeholder; set below
        ctx = StrategyContext(portfolio=portfolio)
        strategy.ctx = ctx
        strategy.initialize()

        if strategy.start_date is None or strategy.end_date is None:
            raise RuntimeError("Strategy must call set_start_date and set_end_date in initialize()")
        if not ctx.symbols:
            raise RuntimeError("Strategy must call ctx.add_equity() in initialize() for at least one symbol")

        # Now that initialize has run, reset portfolio cash to the configured amount.
        portfolio.initial_cash = strategy.initial_cash
        portfolio.cash = strategy.initial_cash
        # Wire the sizing policy and the per-order fee it reserves. The fee
        # comes from the fill model so LeanSetHoldingsSizing reserves exactly
        # what the run will charge.
        portfolio.sizing_model = self.sizing_model
        portfolio.order_fee = self.fill_model.commission_per_order

        order_events: list[OrderEvent] = []
        retained_bars: list[TradeBar] = []
        equity_curve: list[EquitySnapshot] = []
        active_brackets: list[_ActiveBracket] = []
        resting_limit_orders: list[Order] = []

        # Bracket evaluation hook — invoked by the consolidator wrapper
        # BEFORE the strategy's own handler runs, so the strategy sees
        # the correct (possibly closed-out) position state when its
        # ``on_bar`` executes for a bar that triggered a TP/SL exit.
        def _evaluate_brackets(fired_bar: TradeBar) -> None:
            if not active_brackets:
                return
            still_active: list[_ActiveBracket] = []
            for bracket in active_brackets:
                # Skip bars at or before the fill — those belong to the
                # entry's own period, not the monitoring window.
                if fired_bar.end_time <= bracket.fill_time:
                    still_active.append(bracket)
                    continue
                if fired_bar.symbol != bracket.symbol:
                    still_active.append(bracket)
                    continue
                resolution = resolve_bracket_pessimistic(
                    fired_bar,
                    bracket.direction,
                    bracket.take_profit_price,
                    bracket.stop_loss_price,
                )
                if resolution.outcome is IntrabarOutcome.NONE:
                    still_active.append(bracket)
                    continue
                assert resolution.fill_price is not None
                # Close the position with a signed quantity of the
                # opposite sign to the entry.
                exit_quantity = -bracket.quantity
                exit_direction = Direction.SHORT if bracket.direction is Direction.LONG else Direction.LONG
                exit_event = OrderEvent(
                    order_id=portfolio._next_id(),
                    symbol=bracket.symbol,
                    time=fired_bar.end_time,
                    fill_price=resolution.fill_price,
                    fill_quantity=exit_quantity,
                    direction=exit_direction,
                    fee=self.fill_model.commission_per_order,
                    tag="TP" if resolution.outcome is IntrabarOutcome.TAKE_PROFIT else "SL",
                )
                portfolio.apply_fill(exit_event)
                order_events.append(exit_event)
                strategy.on_order_event(exit_event)
            active_brackets[:] = still_active

        ctx._pre_handler_hook = _evaluate_brackets

        def _register_bracket_if_needed(order: Order, event: OrderEvent) -> None:
            if order.take_profit_price is None and order.stop_loss_price is None:
                return
            active_brackets.append(
                _ActiveBracket(
                    entry_order_id=event.order_id,
                    symbol=event.symbol,
                    direction=event.direction,
                    quantity=event.fill_quantity,
                    take_profit_price=order.take_profit_price,
                    stop_loss_price=order.stop_loss_price,
                    fill_time=event.time,
                )
            )

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
            raise NotImplementedError("Phase 1 engine supports a single symbol only")
        symbol = ctx.symbols[0]

        # Keep a "previous minute bar" so that a NEXT_BAR_OPEN fill can use
        # the bar immediately after the signal bar.
        pending_fills: list[tuple[object, TradeBar]] = []  # (order, signal_bar)
        # Track which calendar date has already been force-flatted so the
        # barrier fires at most once per session.
        last_force_flat_date: date | None = None

        def _is_entry_order(order: Order) -> bool:
            """True when ``order`` would grow |position| (vs reduce/flip)."""
            pos = portfolio.get_position(order.symbol)
            return abs(pos.quantity + order.quantity) > abs(pos.quantity)

        def _force_flat_close(pos_qty: int, symbol: str, bar: TradeBar) -> OrderEvent:
            """Synthesize a market-close fill at the current minute's
            close. Bypasses ``fill_model.fill_market_order`` so force-
            flat works identically under any configured fill mode
            (NEXT_BAR_OPEN's deferred semantics don't apply — a session
            close is immediate, not signal-driven)."""
            close_qty = -pos_qty  # opposite sign closes the position
            direction = Direction.SHORT if pos_qty > 0 else Direction.LONG
            fill_price = bar.close
            if direction is Direction.LONG:
                fill_price = fill_price + self.fill_model.slippage_per_share
            else:
                fill_price = fill_price - self.fill_model.slippage_per_share
            return OrderEvent(
                order_id=portfolio._next_id(),
                symbol=symbol,
                time=bar.end_time,
                fill_price=fill_price,
                fill_quantity=close_qty,
                direction=direction,
                fee=self.fill_model.commission_per_order,
                tag="ForceFlat",
            )

        previous_minute_bar: TradeBar | None = None
        for minute_bar in self.data_source.iter_bars(symbol, start_date, end_date):
            # Update portfolio reference price with the latest close.
            portfolio.update_reference_price(symbol, minute_bar.close)

            # ----- Session-close force-flat barrier.
            # Fires once per calendar day on the first minute bar whose
            # wall-clock time has crossed ``force_flat_at``. Cancels
            # everything in flight (queued orders, NEXT_BAR_OPEN
            # deferred fills, active TP/SL brackets), closes every open
            # position at this minute's close, and calls the strategy's
            # ``on_force_flat`` hook so strategies can sync their own
            # internal state. Without all three cancellations, an
            # orphaned entry could execute on tomorrow's open and
            # defeat the whole cutoff.
            if (
                self.execution_config.force_flat_at is not None
                and minute_bar.time.time() >= self.execution_config.force_flat_at
                and minute_bar.time.date() != last_force_flat_date
            ):
                portfolio.clear_pending()
                pending_fills.clear()
                active_brackets.clear()
                resting_limit_orders.clear()
                for sym, pos in list(portfolio.positions.items()):
                    if pos.quantity == 0:
                        continue
                    event = _force_flat_close(pos.quantity, sym, minute_bar)
                    portfolio.apply_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)
                strategy.on_force_flat()
                last_force_flat_date = minute_bar.time.date()

            # ----- Fill any deferred orders (NEXT_BAR_OPEN / NEXT_SESSION_OPEN)
            # with this bar as next_bar. DEFERRED_FILL_MODES is the single
            # source of truth shared with Step 5 below.
            if pending_fills and self.fill_model.mode in DEFERRED_FILL_MODES:
                still_pending: list[tuple[object, TradeBar]] = []
                for order, signal_bar in pending_fills:
                    event = self.fill_model.fill_market_order(order, signal_bar, next_bar=minute_bar)
                    if event is None:
                        still_pending.append((order, signal_bar))
                    else:
                        portfolio.apply_fill(event)
                        order_events.append(event)
                        strategy.on_order_event(event)
                        _register_bracket_if_needed(order, event)  # type: ignore[arg-type]
                pending_fills = still_pending

            # ----- Per-minute hook — fires before consolidator dispatch so the
            #       strategy sees every minute bar including the session-close bar
            #       (which a passthrough consolidator would silently drop because
            #       no subsequent bar arrives to flush it).
            strategy.on_minute_bar(minute_bar)

            # ----- Feed consolidators. Consolidators will invoke strategy handlers
            #       when a bar fires. The strategy may submit orders inside those
            #       handlers; we drain those immediately below.
            consolidators = ctx.get_consolidators(symbol)
            for consolidator in consolidators:
                fired = consolidator.update(minute_bar)
                if fired is not None:
                    pass

            # ----- Session entry cutoff: drop any order submitted after
            # the cutoff that would GROW |position|. Exits (reductions
            # and flips) always pass through — the wrapper protects
            # against opening new exposure late, not against closing.
            if self.execution_config.session_entry_cutoff is not None and portfolio.pending_orders:
                cutoff = self.execution_config.session_entry_cutoff
                if minute_bar.time.time() >= cutoff:
                    kept: list[Order] = []
                    for order in portfolio.pending_orders:
                        if _is_entry_order(order):
                            ctx.log(
                                f"[SESSION CUTOFF] Dropped entry order "
                                f"{order.order_id} for {order.symbol} qty={order.quantity} "
                                f"at {minute_bar.time.time()} >= {cutoff}"
                            )
                            continue
                        kept.append(order)
                    portfolio.pending_orders = kept

            # ----- Drain any pending orders the strategy just submitted.
            #       LIMIT orders move to the resting book; MARKET orders
            #       fill now (SIGNAL_BAR_CLOSE) or defer to the next
            #       minute bar (NEXT_BAR_OPEN).
            if portfolio.pending_orders:
                drained = list(portfolio.drain_pending())
                limit_orders = [o for o in drained if o.order_type == OrderType.LIMIT]
                market_orders = [o for o in drained if o.order_type == OrderType.MARKET]
                other_orders = [o for o in drained if o.order_type not in (OrderType.LIMIT, OrderType.MARKET)]
                if other_orders:
                    raise NotImplementedError(f"order types not yet supported: {[o.order_type for o in other_orders]}")
                for order in limit_orders:
                    assert order.limit_price is not None, "LIMIT order requires limit_price"
                    resting_limit_orders.append(order)
                if market_orders:
                    # The "signal bar" for a SIGNAL_BAR_CLOSE fill is the
                    # latest fired consolidated bar. For multi-consolidator
                    # strategies this gets ambiguous, but Phase 1 uses one.
                    assert len(consolidators) == 1
                    signal_bar = self._last_fired(consolidators[0])
                    assert signal_bar is not None, (
                        "Strategy submitted a market order but no consolidated bar was available"
                    )
                    for order in market_orders:
                        if self.fill_model.mode == FillMode.SIGNAL_BAR_CLOSE:
                            event = self.fill_model.fill_market_order(order, signal_bar, next_bar=None)
                            assert event is not None
                            portfolio.apply_fill(event)
                            order_events.append(event)
                            strategy.on_order_event(event)
                            _register_bracket_if_needed(order, event)
                        elif self.fill_model.mode == FillMode.NEXT_SESSION_OPEN:
                            # Defer until a subsequent minute bar whose trading date is
                            # strictly after signal_bar.end_time.date() (NY-local). The
                            # fill_model's eligibility check in fill_market_order enforces
                            # the date condition when pending_fills loop re-tries.
                            # See test_engine_fill_modes.py for the pinned fill-timing
                            # invariant. This semantic mirrors QC's MarketOrder flow:
                            # algorithm fires at END of first session minute, fill lands
                            # at NEXT (second) minute's open.
                            pending_fills.append((order, signal_bar))
                        elif self.fill_model.mode == FillMode.NEXT_BAR_OPEN:
                            # Defer until the next minute bar — protects
                            # single-stream cases where signal_bar IS the current
                            # minute_bar, so filling against it would defeat the
                            # "next bar open" semantic.
                            pending_fills.append((order, signal_bar))
                        else:
                            raise ValueError(f"unknown fill mode: {self.fill_model.mode}")

            # ----- Evaluate resting limit orders against this minute's
            #       [low, high] range. The penetration requirement is
            #       measured against the adverse extreme (low for buy,
            #       high for sell) per the user spec — fills happen at
            #       the limit price exactly, with no slippage.
            if resting_limit_orders:
                penetration = self.execution_config.limit_penetration
                still_resting: list[Order] = []
                for order in resting_limit_orders:
                    assert order.limit_price is not None
                    if order.symbol != minute_bar.symbol:
                        still_resting.append(order)
                        continue
                    if order.direction is Direction.LONG:
                        fills = (order.limit_price - minute_bar.low) >= penetration
                    else:
                        fills = (minute_bar.high - order.limit_price) >= penetration
                    if not fills:
                        still_resting.append(order)
                        continue
                    event = OrderEvent(
                        order_id=order.order_id,
                        symbol=order.symbol,
                        time=minute_bar.end_time,
                        fill_price=order.limit_price,
                        fill_quantity=order.quantity,
                        direction=order.direction,
                        fee=self.fill_model.commission_per_order,
                        tag=order.tag,
                    )
                    portfolio.apply_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)
                    _register_bracket_if_needed(order, event)
                resting_limit_orders[:] = still_resting

            # ----- Score any expired insights against current prices.
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
            previous_minute_bar = minute_bar

        # ------------------------------------------------------------------
        # 3. Finalize.
        # ------------------------------------------------------------------
        # End-of-data consolidator flush. LEAN scans consolidators as the
        # data feed ends, firing the final *complete* consolidated bar — a
        # 15-min bar is complete once its window closes (e.g. 15:45–16:00),
        # even though no later input bar arrives to trigger it. Without this
        # the Engine drops the last consolidated bar of the backtest, an
        # off-by-one vs LEAN's per-bar state/decision stream. scan() only
        # flushes a full period, so a genuinely partial trailing bar is
        # still dropped — matching LEAN, which does not emit partial bars.
        if previous_minute_bar is not None:
            for consolidator in ctx.get_consolidators(symbol):
                consolidator.scan(previous_minute_bar.end_time)
            # A market order submitted from the final consolidated bar's
            # handler fills immediately against that bar in SIGNAL_BAR_CLOSE
            # mode — the same as any in-loop bar, mirroring LEAN's
            # ImmediateFillModel. Deferred fill modes cannot fill (no next
            # bar exists), which is the correct outcome.
            if portfolio.pending_orders and self.fill_model.mode == FillMode.SIGNAL_BAR_CLOSE:
                final_consolidators = ctx.get_consolidators(symbol)
                final_signal_bar = self._last_fired(final_consolidators[0]) if final_consolidators else None
                for order in portfolio.drain_pending():
                    if order.order_type is not OrderType.MARKET:
                        continue
                    assert final_signal_bar is not None, (
                        "market order from the final consolidated bar but no fired bar to fill against"
                    )
                    event = self.fill_model.fill_market_order(order, final_signal_bar, next_bar=None)
                    assert event is not None
                    portfolio.apply_fill(event)
                    order_events.append(event)
                    strategy.on_order_event(event)
                    _register_bracket_if_needed(order, event)

        strategy.on_end_of_algorithm()

        # Score any remaining active insights with the final prices.
        if previous_minute_bar is not None:
            final_prices = {sym: portfolio.reference_price.get(sym, Decimal(0)) for sym in ctx.symbols}
            # Force-expire active insights so they all get scored.
            for insight in ctx.insight_manager.get_active_insights(previous_minute_bar.end_time):
                if not insight.score.is_final_score:
                    insight.reference_value_final = final_prices.get(insight.symbol, Decimal(0))
                    from app.engine.framework.insight_scorer import DefaultInsightScoreFunction

                    DefaultInsightScoreFunction().score(insight)
                    insight.score.finalize(previous_minute_bar.end_time)

        # Build insight summary.
        insight_summary = ctx.insight_manager.get_summary()

        final_equity = portfolio.total_value()
        return BacktestResult(
            initial_cash=portfolio.initial_cash,
            final_equity=final_equity,
            net_profit=final_equity - portfolio.initial_cash,
            total_fees=portfolio.total_fees,
            order_events=order_events,
            log_lines=list(ctx.log_lines),
            bars=retained_bars,
            equity_curve=equity_curve,
            insights=ctx.insight_manager.all_insights,
            insight_summary=insight_summary.to_dict(),
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
