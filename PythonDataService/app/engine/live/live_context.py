"""StrategyContext-compatible live runtime services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.trade_bar import TradeBar
from app.engine.framework.insight import Insight
from app.engine.framework.insight_manager import InsightManager
from app.engine.live.live_portfolio import LivePortfolio


@dataclass
class LiveContext:
    """Runtime services exposed to strategies by the live engine."""

    portfolio: LivePortfolio
    _consolidators: dict[str, list[tuple[timedelta, TradeBarConsolidator, Callable[[TradeBar], None]]]] = field(
        default_factory=dict
    )
    symbols: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    current_time: datetime | None = None
    consolidated_bars: list[TradeBar] = field(default_factory=list)
    insight_manager: InsightManager = field(default_factory=InsightManager)
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

        def _on_emit(bar: TradeBar, ctx: LiveContext = self) -> None:
            ctx.current_time = bar.end_time
            ctx.portfolio.update_reference_price(bar.symbol, bar.close)
            consolidator._last_fired_bar = bar
            ctx.consolidated_bars.append(bar)
            if ctx._pre_handler_hook is not None:
                ctx._pre_handler_hook(bar)
            handler(bar)

        consolidator.on_data_consolidated = _on_emit
        consolidator._last_fired_bar = None
        self._consolidators.setdefault(symbol.upper(), []).append((period, consolidator, handler))
        return consolidator

    def get_consolidators(self, symbol: str) -> list[TradeBarConsolidator]:
        return [c for _, c, _ in self._consolidators.get(symbol.upper(), [])]

    def log(self, message: str) -> None:
        self.log_lines.append(message)

    def set_holdings(self, symbol: str, fraction: Decimal | float) -> None:
        if self.current_time is None:
            raise RuntimeError("set_holdings requires a current live bar time")
        self.portfolio.set_holdings(symbol.upper(), fraction, self.current_time)

    def liquidate(self, symbol: str) -> None:
        if self.current_time is None:
            raise RuntimeError("liquidate requires a current live bar time")
        self.portfolio.liquidate(symbol.upper(), self.current_time)

    def emit_insight(self, insight: Insight) -> None:
        if self.current_time is not None:
            insight.generated_time = self.current_time
            insight.close_time = self.current_time + insight.period
        price = self.portfolio.reference_price.get(insight.symbol, Decimal(0))
        self.insight_manager.add(insight, price)
