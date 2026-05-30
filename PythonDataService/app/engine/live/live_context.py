"""StrategyContext-compatible live runtime services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.trade_bar import TradeBar
from app.engine.framework.insight import Insight
from app.engine.framework.insight_manager import InsightManager
from app.engine.live.live_portfolio import LivePortfolio

if TYPE_CHECKING:
    from app.engine.live.indicator_state import HydratePolicy


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

    # ---- Indicator-state persistence ----
    hydrate_policy: HydratePolicy | None = None
    run_dir: Path | None = None
    artifacts_root: Path | None = None
    session_start_ms: int | None = None

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

    def market_order(self, symbol: str, quantity: int, tag: str = "") -> None:
        """Submit a fixed-quantity market order (signed: + buy, − sell)."""
        if self.current_time is None:
            raise RuntimeError("market_order requires a current live bar time")
        self.portfolio.submit_market_order(symbol.upper(), quantity, self.current_time, tag)

    def emit_insight(self, insight: Insight) -> None:
        if self.current_time is not None:
            insight.generated_time = self.current_time
            insight.close_time = self.current_time + insight.period
        price = self.portfolio.reference_price.get(insight.symbol, Decimal(0))
        self.insight_manager.add(insight, price)

    def hydrate_indicator_state(self, strategy: object) -> None:
        """Implement the §4.1 validation ladder + receipt-write contract.

        Caller (LiveEngine.run) invokes immediately after strategy.initialize().
        Behavior depends on self.hydrate_policy:
          REQUIRE  — failure -> write receipt + raise IndicatorStateHydrationError
          OPTIONAL — failure -> write receipt + return (cold-start)
          DISABLED — never reads sidecar; writes a 'disabled_by_operator' receipt and returns
          None     — persistence disabled at the engine level (replay tests); return
        """
        from app.engine.live.indicator_state import hydrate

        if self.hydrate_policy is None:
            return
        if self.run_dir is None or self.artifacts_root is None or self.session_start_ms is None:
            raise RuntimeError(
                "hydrate_indicator_state requires run_dir, artifacts_root, session_start_ms on LiveContext"
            )
        hydrate(
            strategy=strategy,
            policy=self.hydrate_policy,
            artifacts_root=self.artifacts_root,
            run_dir=self.run_dir,
            session_start_ms=self.session_start_ms,
        )

    def maybe_write_indicator_state(
        self,
        strategy: object,
        reason: str,
        *,
        code_sha: str,
        strategy_spec_sha: str,
        last_consolidated_bar_end_ms: int,
    ) -> None:
        """Force-flat or graceful-shutdown checkpoint write.

        No-op when persistence is disabled (artifacts_root is None).
        On force_flat: write if strategy reports a non-None payload.
        On shutdown:   write only if strictly newer than on-disk.
        """
        from app.engine.live.indicator_state import maybe_write

        if self.artifacts_root is None:
            return
        maybe_write(
            strategy=strategy,
            artifacts_root=self.artifacts_root,
            reason=reason,
            code_sha=code_sha,
            strategy_spec_sha=strategy_spec_sha,
            last_consolidated_bar_end_ms=last_consolidated_bar_end_ms,
        )
