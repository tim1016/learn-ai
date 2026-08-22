"""SmaCrossoverAlgorithm — new-engine port of the legacy SMA crossover strategy.

Formula: Long-only golden cross / death cross. Enter long when short SMA crosses above long SMA; exit when short SMA crosses below long SMA. Default periods follow `_rsi_range_base.py` conventions (50/200 for the divergence-research s3 variant; new engine accepts arbitrary).
Reference: Internal strategy retained from the retired pandas-ta service implementation. LEAN inspiration but no line-for-line port.
Canonical implementation: this file. Parity-pinned secondary: `app/engine/strategy/spec/evaluator.py::SpecAlgorithm` driven by `spec/fixtures/sma_crossover.spec.json` reproduces the hand-coded twin trade-by-trade. Divergence-research-only parallel: `app/research/divergence/strategies/s3_sma_crossover.py` (vectorized pandas).
Validated against: PythonDataService/tests/test_strategy_engine.py; spec ↔ hand-coded parity at `app/engine/strategy/spec/tests/test_spec_sma_parity.py`; `tests/engine/strategy/test_sma_signal_program.py::test_validated_sma_settings_corpus_has_a_pinned_trace_root`.

Golden-cross / death-cross rule lifted from
the retired pandas-ta service implementation:

    * Enter long when the short SMA crosses **above** the long SMA.
    * Exit when the short SMA crosses **below** the long SMA.

Unlike the legacy pandas-ta version which runs bar-by-bar over a pre-computed
DataFrame, this port plugs into the LEAN-compatible engine: minute bars stream
in, a ``TradeBarConsolidator`` produces bars at ``resolution_minutes``, and both
SMAs update on each consolidated close. The strategy emits instrument-free
ENTER/EXIT intents; the selected execution adapter binds those decisions to an
instrument and sizing policy. Backtests still route those intents through the
portfolio fill model, so trade logs remain fill-driven.

The strategy is configurable via constructor kwargs so the registry can build
it with user-supplied parameters; defaults mirror the legacy strategy's defaults
(10/30 windows) but with a 15-minute resolution to match the rest of the Phase 1
data flow. Parity against the legacy strategy is exercised by
``test_sma_crossover_parity`` using a synthetic bar stream — the contract is
"same set of winning vs losing trades on the same input data", not bit-exact
prices.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.sma import SimpleMovingAverage
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.utils.timestamps import display_time


@dataclass
class _PendingEntry:
    sma_short: Decimal
    sma_long: Decimal


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    sma_short: Decimal
    sma_long: Decimal


class SmaCrossoverAlgorithm(Strategy):
    """Simple-moving-average crossover, long-only.

    Parameters
    ----------
    symbol:
        Ticker to trade. Uppercased on assignment.
    short_window:
        Period of the fast SMA. Must be >= 2.
    long_window:
        Period of the slow SMA. Must be strictly greater than ``short_window``.
    resolution_minutes:
        Consolidated bar size in minutes. Defaults to 15 to match the SPY
        strategy's resolution and reuse the same minute-bar data files.
    """

    def __init__(
        self,
        symbol: str = "SPY",
        short_window: int = 10,
        long_window: int = 30,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__()
        if short_window < 2:
            raise ValueError("short_window must be >= 2")
        if long_window <= short_window:
            raise ValueError("long_window must be strictly greater than short_window")
        if resolution_minutes <= 0:
            raise ValueError("resolution_minutes must be > 0")

        self._symbol_name = symbol.upper()
        self._short_window = short_window
        self._long_window = long_window
        self._resolution_minutes = resolution_minutes
        self._resolution = timedelta(minutes=resolution_minutes)

        self._symbol: str = ""
        self._sma_short: SimpleMovingAverage | None = None
        self._sma_long: SimpleMovingAverage | None = None

        # Previous-bar crossover state — starts unknown so the first bar that
        # produces two ready SMAs only seeds the state, it does not trigger an
        # entry.
        self._prev_short_above_long: bool | None = None

        self._in_position: bool = False
        self._pending_entry: _PendingEntry | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []
        # Set only by the registry's Signal Program factory. Direct
        # construction stays a compatibility surface for historical tests and
        # ledgers; public Backtest construction goes through this program.
        self.signal_program: SignalProgram | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        # Sensible defaults for a standalone run — the router will override
        # these via the request body when invoked from the API.
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        self._sma_short = SimpleMovingAverage(f"SMA{self._short_window}", self._short_window)
        self._sma_long = SimpleMovingAverage(f"SMA{self._long_window}", self._long_window)

        self._prev_short_above_long = None
        self._in_position = False
        self._pending_entry = None
        self._open_trade = None

        self.ctx.register_consolidator(
            self._symbol,
            self._resolution,
            self._signal_program_handler(),
        )

    def _signal_program_handler(self) -> Callable[[TradeBar], None]:
        """Use the registered staged program when one owns this strategy."""
        if self.signal_program is None or not self.signal_program.active:
            return self._on_consolidated_bar
        return self.signal_program.on_consolidated_bar

    def signal_program_settings(self) -> dict[str, str]:
        """Stable SMA settings which participate in evaluation identity."""
        return {
            "symbol": self._symbol_name,
            "short_window": str(self._short_window),
            "long_window": str(self._long_window),
            "resolution_minutes": str(self._resolution_minutes),
        }

    # ------------------------------------------------------------------
    # Signal-decision boundary — the bar handler split before custody
    # effects, mirroring EmaCrossoverSignalAlgorithm's
    # evaluate_signal_bar / commit_signal_decision seam.
    # ------------------------------------------------------------------
    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        """Compatibility callback for direct, non-program constructions."""
        decision = self.evaluate_signal_bar(bar)
        if decision.intent is not None:
            self.commit_signal_decision(bar, decision.intent)

    def evaluate_signal_bar(self, bar: TradeBar) -> SignalDecision:
        """Advance indicator math and describe one possible semantic action.

        This method never emits an intent or changes position custody state.
        The registered ``SignalSession`` owns the subsequent commit/discard,
        which makes an ``OBSERVE_ONLY`` bar permanently non-actionable even
        when an operator presses Continue immediately afterward.
        """
        assert self._sma_short is not None
        assert self._sma_long is not None

        self._sma_short.update(bar.end_ms, bar.close)
        self._sma_long.update(bar.end_ms, bar.close)

        timeframe = f"{self._resolution_minutes}m"

        # Until both SMAs have enough samples, we can't make a decision.
        if not (self._sma_short.is_ready and self._sma_long.is_ready):
            return SignalDecision(
                intent=None,
                ready=False,
                relation_facts={"short_above_long": False, "was_in_position": self._in_position},
                signal_facts={"decision": "HOLD", "timeframe": timeframe},
                reason_evidence={"sma_short": "UNREADY", "sma_long": "UNREADY"},
                action_plan_request=None,
            )

        assert self._sma_short.current_value is not None
        assert self._sma_long.current_value is not None

        short_val = self._sma_short.current_value
        long_val = self._sma_long.current_value
        current_above = short_val > long_val
        prior_in_position = self._in_position

        if self._prev_short_above_long is None:
            # First ready bar — seed the crossover state only, no trade, so
            # the first crossover we observe is a fresh one rather than
            # whatever historical state happens to sit there.
            self._prev_short_above_long = current_above
            return SignalDecision(
                intent=None,
                ready=True,
                relation_facts={"short_above_long": current_above, "was_in_position": prior_in_position},
                signal_facts={"decision": "HOLD", "timeframe": timeframe},
                reason_evidence={"sma_short": str(short_val), "sma_long": str(long_val)},
                action_plan_request=None,
            )

        fresh_golden_cross = current_above and not self._prev_short_above_long
        fresh_death_cross = (not current_above) and self._prev_short_above_long

        bar_signal = "HOLD"
        intent: SignalIntent | None = None

        if self._in_position:
            if fresh_death_cross:
                intent = SignalIntent(kind=SignalIntentKind.EXIT, bar_close_ms=bar.end_ms, intended_price=bar.close)
                bar_signal = "EXIT"
        else:
            if fresh_golden_cross:
                intent = SignalIntent(kind=SignalIntentKind.ENTER, bar_close_ms=bar.end_ms, intended_price=bar.close)
                bar_signal = "ENTER"

        # Update the crossover state for the next bar.
        self._prev_short_above_long = current_above

        return SignalDecision(
            intent=intent,
            ready=True,
            relation_facts={"short_above_long": current_above, "was_in_position": prior_in_position},
            signal_facts={"decision": bar_signal, "timeframe": timeframe},
            reason_evidence={"sma_short": str(short_val), "sma_long": str(long_val)},
            action_plan_request=(
                {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
            ),
        )

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None:
        """Apply one session-committed signal through the bound executor."""
        assert self.ctx is not None
        assert self._sma_short is not None and self._sma_short.current_value is not None
        assert self._sma_long is not None and self._sma_long.current_value is not None
        short_val = self._sma_short.current_value
        long_val = self._sma_long.current_value

        if intent.kind is SignalIntentKind.EXIT:
            self.ctx.emit_signal_intent(intent)
            self.ctx.log(
                f"EXIT SIGNAL: {display_time(bar.end_ms)} "
                f"Close={bar.close:.2f} "
                f"SMA{self._short_window}={short_val:.4f} "
                f"SMA{self._long_window}={long_val:.4f}"
            )
            self._in_position = False
            return

        assert intent.kind is SignalIntentKind.ENTER
        self._pending_entry = _PendingEntry(sma_short=short_val, sma_long=long_val)
        self.ctx.emit_signal_intent(intent)
        self._in_position = True
        self.ctx.log(
            f"ENTRY SIGNAL: {display_time(bar.end_ms)} "
            f"Close={bar.close:.2f} "
            f"SMA{self._short_window}={short_val:.4f} "
            f"SMA{self._long_window}={long_val:.4f}"
        )

    def discard_signal_decision(self, _bar: TradeBar, _intent: SignalIntent | None) -> None:
        """A staged candidate has no speculative lifecycle state to unwind."""
        return

    def rollback_blocked_entry(self) -> None:
        """Undo lifecycle state when the live liveness gate blocks ENTER."""
        self._in_position = False
        self._pending_entry = None

    def rollback_blocked_exit(self) -> None:
        """Undo the EXIT-time state committed by ``commit_signal_decision``
        when the caller refuses to act on this signal (e.g. a Clerk
        admission rejection). Without this, a rejected EXIT leaves the
        strategy believing it is flat while the broker still holds the
        position."""
        self._in_position = True

    def on_force_flat(self) -> None:
        """Reset lifecycle bookkeeping to a clean flat slate.

        Called after live-adapter warmup replay (#1708 review finding 3):
        indicator state (SMA) is meant to carry forward from the replay,
        but any position the replay itself would have opened is not real
        and must not leak into the live decision loop."""
        self._in_position = False
        self._pending_entry = None
        self._open_trade = None

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping (same shape as SpyEmaCrossoverAlgorithm).
    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                if self.ctx is not None:
                    self.ctx.log(f"WARN: LONG fill at {display_time(event.filled_at_ms)} with no pending entry")
                return
            self._open_trade = _OpenTrade(
                entry_time_ms=event.filled_at_ms,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                sma_short=self._pending_entry.sma_short,
                sma_long=self._pending_entry.sma_long,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {display_time(event.filled_at_ms)} "
                    f"Price={event.fill_price:.2f} "
                    f"SMA{self._short_window}={self._open_trade.sma_short:.4f} "
                    f"SMA{self._long_window}={self._open_trade.sma_long:.4f}"
                )
        else:
            if self._open_trade is None:
                return
            entry = self._open_trade
            pnl_pts = event.fill_price - entry.entry_price
            pnl_pct = pnl_pts / entry.entry_price
            result = "WIN" if pnl_pts >= 0 else "LOSS"
            self.trade_log.append(
                LoggedTrade(
                    entry_time_ms=entry.entry_time_ms,
                    entry_price=entry.entry_price,
                    exit_time_ms=event.filled_at_ms,
                    exit_price=event.fill_price,
                    quantity=entry.quantity,
                    pnl_pts=pnl_pts,
                    pnl_pct=pnl_pct,
                    result=result,
                    indicators={
                        f"sma_{self._short_window}": entry.sma_short,
                        f"sma_{self._long_window}": entry.sma_long,
                    },
                    signal_reason=(f"SMA({self._short_window}) crossed below SMA({self._long_window})"),
                )
            )
            if self.ctx is not None:
                self.ctx.log(
                    f"EXIT: {display_time(event.filled_at_ms)} "
                    f"Price={event.fill_price:.2f} PnL={pnl_pts:.2f} "
                    f"({pnl_pct * 100:.2f}%) {result}"
                )
            self._open_trade = None

    def on_end_of_algorithm(self) -> None:
        if self._in_position:
            assert self.ctx is not None
            if self.ctx.current_time_ms is None:
                raise RuntimeError("SMA crossover exit requires a current bar time")
            self.ctx.emit_signal_intent(
                SignalIntent(
                    kind=SignalIntentKind.EXIT,
                    bar_close_ms=self.ctx.current_time_ms,
                    intended_price=self.ctx.portfolio.reference_price.get(self._symbol, Decimal(0)),
                )
            )
            self._in_position = False
