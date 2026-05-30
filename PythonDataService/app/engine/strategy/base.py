"""Strategy base class — the user-facing algorithm API.

Roughly analogous to LEAN's ``QCAlgorithm`` base class. Subclasses override
``initialize`` (to configure indicators, consolidators, dates) and event
callbacks (``on_bar``, ``on_order_event``, ``on_end``).
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import OrderEvent
from app.engine.execution.portfolio import Portfolio
from app.engine.framework.insight import Insight
from app.engine.framework.insight_manager import InsightManager


@dataclass(frozen=True)
class DecisionSnapshot:
    """One per-bar decision-time snapshot a Strategy may publish.

    Optional, observability-only: strategies that opt in stash this on
    ``Strategy.last_decision_snapshot`` after each consolidated bar
    fires; downstream consumers (the live runtime's ``DecisionWriter``,
    in particular) read it to populate ``decisions.parquet`` for
    later three-way reconciliation.

    Strategies that don't care leave ``last_decision_snapshot=None``
    and nothing reads it. Backtest paths and existing tests are
    unaffected — there is no behavior change unless an external reader
    explicitly observes this attribute.

    Schema mirrors ``app.engine.live.artifacts.DECISION_COLUMNS`` so
    the LiveEngine's writer integration can convert one-to-one without
    bookkeeping. ``signal`` is the per-bar action the strategy took:
    ``ENTER`` if it newly entered a position on this bar, ``EXIT`` if
    it newly liquidated, ``HOLD`` for any other state (warmup-skip,
    bars-until-exit countdown, no signal fired). The strategy is
    responsible for computing this — see ``SpyEmaCrossoverAlgorithm``
    for the canonical pattern.
    """

    bar_close_ms: int
    ema5: float
    ema10: float
    rsi: float
    signal: str
    intended_price: float


@dataclass
class LoggedTrade:
    """A completed round-trip trade captured by a strategy.

    This is the shared trade-log shape all strategies populate. Per-strategy
    indicator snapshots (e.g., EMA5/EMA10/RSI for SPY, SMA short/long for the
    SMA crossover) go into the ``indicators`` bag so the router, statistics,
    and tests can work with a single uniform shape without hardcoding which
    indicators a given strategy uses.

    ``pnl_pct`` is required and is what ``statistics.summarize`` reads; all
    other fields are populated identically by every strategy.
    """

    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    quantity: int
    pnl_pts: Decimal
    pnl_pct: Decimal
    result: str  # "WIN" or "LOSS"
    indicators: dict[str, Decimal] = field(default_factory=dict)
    signal_reason: str = ""

    def __getattr__(self, name: str) -> Decimal:
        """Delegate unknown attribute access into the ``indicators`` bag.

        Lets existing SPY validation tests continue to read ``trade.ema5``
        as if it were a dataclass field while newer strategies use arbitrary
        indicator names. ``__getattr__`` is only called when the normal
        attribute lookup has already missed, so this never shadows a real
        field on the dataclass.
        """
        indicators = self.__dict__.get("indicators")
        if indicators is not None and name in indicators:
            return indicators[name]
        raise AttributeError(name)


@dataclass
class StrategyContext:
    """Runtime services exposed to strategies.

    Provided by the BacktestEngine during initialization.
    """

    portfolio: Portfolio
    # Map of symbol -> list of (period, consolidator, handler).
    _consolidators: dict[str, list[tuple[timedelta, TradeBarConsolidator, Callable[[TradeBar], None]]]] = field(
        default_factory=dict
    )
    # Symbols the strategy subscribed to.
    symbols: list[str] = field(default_factory=list)
    # Logged messages for debugging / trade logs.
    log_lines: list[str] = field(default_factory=list)
    current_time: datetime | None = None
    # Consolidated bars captured for charting (one list per consolidator).
    consolidated_bars: list[TradeBar] = field(default_factory=list)
    # Insight manager — tracks structured predictions and scores them.
    insight_manager: InsightManager = field(default_factory=InsightManager)
    # Engine-owned hook invoked on every fired consolidated bar BEFORE the
    # strategy's own handler runs. Used by the BacktestEngine to evaluate
    # active TP/SL brackets intrabar so the strategy sees the correct
    # position state when its ``on_bar`` runs. ``None`` on strategies
    # unit-tested without the engine.
    _pre_handler_hook: Callable[[TradeBar], None] | None = None

    def add_equity(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol not in self.symbols:
            self.symbols.append(symbol)
        return symbol

    def register_consolidator(
        self,
        symbol: str,
        period: timedelta,
        handler: Callable[[TradeBar], None],
    ) -> TradeBarConsolidator:
        consolidator = TradeBarConsolidator(period)

        # Wrap handler so the consolidator's emission also records the
        # strategy's ``current_time`` for convenience and stashes the last
        # fired bar on the consolidator (used by the engine for fills).
        def _on_emit(bar: TradeBar, ctx: StrategyContext = self) -> None:
            ctx.current_time = bar.end_time
            ctx.portfolio.update_reference_price(bar.symbol, bar.close)
            consolidator._last_fired_bar = bar  # type: ignore[attr-defined]
            ctx.consolidated_bars.append(bar)
            if ctx._pre_handler_hook is not None:
                ctx._pre_handler_hook(bar)
            handler(bar)

        consolidator.on_data_consolidated = _on_emit
        consolidator._last_fired_bar = None  # type: ignore[attr-defined]
        self._consolidators.setdefault(symbol.upper(), []).append((period, consolidator, handler))
        return consolidator

    def get_consolidators(self, symbol: str) -> list[TradeBarConsolidator]:
        return [c for _, c, _ in self._consolidators.get(symbol.upper(), [])]

    def log(self, message: str) -> None:
        self.log_lines.append(message)

    # Convenience proxies to portfolio
    def set_holdings(self, symbol: str, fraction: Decimal | float) -> None:
        assert self.current_time is not None
        self.portfolio.set_holdings(symbol.upper(), fraction, self.current_time)

    def liquidate(self, symbol: str) -> None:
        assert self.current_time is not None
        self.portfolio.liquidate(symbol.upper(), self.current_time)

    def market_order(self, symbol: str, quantity: int, tag: str = "") -> None:
        """Submit a fixed-quantity market order (signed: + buy, − sell).

        For strategies that size by a fixed share count rather than a
        portfolio fraction (e.g. the VWAP-reversion port, which mirrors a
        fixed-quantity reference). Delegates to the portfolio's
        ``submit_market_order``."""
        assert self.current_time is not None
        self.portfolio.submit_market_order(symbol.upper(), quantity, self.current_time, tag)

    def emit_insight(self, insight: Insight) -> None:
        """Register a structured prediction.

        Sets the insight's generated_time to the current bar time and
        records the current reference price. Strategies that don't emit
        insights continue to work exactly as before — this is purely
        additive.
        """
        if self.current_time is not None:
            insight.generated_time = self.current_time
            insight.close_time = self.current_time + insight.period
        price = self.portfolio.reference_price.get(insight.symbol, Decimal(0))
        self.insight_manager.add(insight, price)


class Strategy(ABC):
    """Base class for user algorithms.

    Subclasses should override ``initialize`` to configure symbols,
    consolidators, and indicators. Event callbacks default to no-ops.
    """

    def __init__(self) -> None:
        self.ctx: StrategyContext | None = None
        self.start_date: datetime | None = None
        self.end_date: datetime | None = None
        self.initial_cash: Decimal = Decimal(100000)
        # Optional per-bar snapshot subclasses may publish for
        # downstream observers (the live runtime's DecisionWriter).
        # Default None — strategies opt in by setting this from inside
        # their bar handler. See DecisionSnapshot.
        self.last_decision_snapshot: DecisionSnapshot | None = None

    # ------------------------------------------------------------------
    # Declarative configuration (called in initialize)
    # ------------------------------------------------------------------
    def set_start_date(self, year: int, month: int, day: int) -> None:
        from zoneinfo import ZoneInfo

        self.start_date = datetime(year, month, day, tzinfo=ZoneInfo("America/New_York"))

    def set_end_date(self, year: int, month: int, day: int) -> None:
        from zoneinfo import ZoneInfo

        # End of day, inclusive.
        self.end_date = datetime(year, month, day, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))

    def set_cash(self, amount: float | int | Decimal) -> None:
        self.initial_cash = Decimal(str(amount))

    # ------------------------------------------------------------------
    # Lifecycle (overridden by subclasses)
    # ------------------------------------------------------------------
    def initialize(self) -> None:  # pragma: no cover - override in subclass
        """Configure symbols, consolidators, indicators, dates."""

    def on_end_of_algorithm(self) -> None:  # pragma: no cover - override
        """Called once after the final bar."""

    def on_order_event(self, event: OrderEvent) -> None:  # pragma: no cover
        """Called whenever a pending order fills."""

    def on_force_flat(self) -> None:  # pragma: no cover
        """Called by the engine after a session-close force-flat.

        Positions have already been closed and pending / deferred
        orders cancelled by the time this fires. Strategies that keep
        their own internal state (e.g. ``self._entered``,
        ``self.bars_held``) should override this to reset those flags
        so the next session opens with a clean slate. Default is a
        no-op for strategies that don't opt into the session wrapper.
        """

    def on_minute_bar(self, bar: TradeBar) -> None:  # pragma: no cover - override in subclass
        """Called for every minute bar consumed by the engine, before consolidator dispatch.

        LEAN-parity hook for ``OnData`` semantics at minute resolution.
        The engine calls this for every bar emitted by the data source,
        including the last minute bar of each session (which a 1-minute
        passthrough consolidator would silently drop because no subsequent
        bar arrives to flush it). Default no-op; strategies opt in by
        overriding.
        """
