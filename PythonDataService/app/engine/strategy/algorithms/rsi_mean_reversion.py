"""RsiMeanReversionAlgorithm — new-engine port of the legacy RSI mean-reversion strategy.

Formula: Long-only RSI mean reversion. Entry: RSI(window) drops strictly below `oversold` threshold (typically 30). Exit: RSI(window) rises strictly above `overbought` threshold (typically 70). End-of-run: any open position closed on `on_end_of_algorithm`.
Reference: Internal strategy retained from the retired pandas-ta service implementation. LEAN inspiration but no line-for-line port.
Canonical implementation: this file. Parity-pinned secondary: `app/engine/strategy/spec/evaluator.py::SpecAlgorithm` driven by `spec/fixtures/rsi_mean_reversion.spec.json` reproduces the hand-coded twin trade-by-trade. Divergence-research-only parallel: `app/research/divergence/strategies/s2_rsi_mean_reversion.py` (vectorized pandas).
Validated against: PythonDataService/tests/test_strategy_engine.py; spec ↔ hand-coded parity at `app/engine/strategy/spec/tests/test_spec_rsi_mean_reversion_parity.py`; engine-level test `test_rsi_mean_reversion_parity.py` validates trade-set contract against legacy module; `tests/engine/strategy/test_rsi_signal_program.py::test_validated_rsi_mean_reversion_settings_corpus_has_a_pinned_trace_root`.

Historical source: retired pandas-ta service implementation

Rule set (unchanged from the pandas-ta reference):
    * **Entry (long)** — when RSI(window) drops **strictly below** ``oversold``.
    * **Exit**          — when RSI(window) rises **strictly above** ``overbought``.
    * **End of run**    — any open position is closed on ``on_end_of_algorithm``.

The legacy strategy consumes a pre-computed DataFrame and enters/exits at the
bar's close. The new engine emits instrument-free ENTER/EXIT intents from a
``TradeBarConsolidator`` and binds them to the configured execution adapter.
The trade-set contract — same entries, exits, and WIN/LOSS verdicts on the same
input data — is validated by ``test_rsi_mean_reversion_parity``.

Parameters are constructor kwargs so the router's strategy registry can build
instances from a user-supplied ``RsiMeanReversionParams`` Pydantic model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.utils.timestamps import display_time


@dataclass
class _PendingEntry:
    rsi: Decimal


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    entry_rsi: Decimal


class RsiMeanReversionAlgorithm(Strategy):
    """RSI-threshold mean reversion, long-only.

    Parameters
    ----------
    symbol:
        Ticker to trade. Uppercased on assignment.
    window:
        RSI period. Must be >= 2.
    oversold:
        Entry threshold. The strategy goes long when RSI drops strictly
        below this value. Legacy default: 30.
    overbought:
        Exit threshold. The strategy exits when RSI rises strictly above
        this value. Legacy default: 70.
    resolution_minutes:
        Consolidated bar size in minutes. Defaults to 15 to match the rest
        of the Phase 1 data flow.
    """

    def __init__(
        self,
        symbol: str = "SPY",
        window: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__()
        if window < 2:
            raise ValueError("window must be >= 2")
        if not 0 < oversold < overbought < 100:
            raise ValueError(
                f"require 0 < oversold < overbought < 100 (got oversold={oversold}, overbought={overbought})"
            )
        if resolution_minutes <= 0:
            raise ValueError("resolution_minutes must be > 0")

        self._symbol_name = symbol.upper()
        self._window = window
        self._oversold = Decimal(str(oversold))
        self._overbought = Decimal(str(overbought))
        self._resolution_minutes = resolution_minutes
        self._resolution = timedelta(minutes=resolution_minutes)

        self._symbol: str = ""
        self._rsi: RelativeStrengthIndex | None = None

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
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        self._rsi = RelativeStrengthIndex(f"RSI{self._window}", self._window)

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
        """Stable RSI settings which participate in evaluation identity."""
        return {
            "symbol": self._symbol_name,
            "window": str(self._window),
            "oversold": str(self._oversold),
            "overbought": str(self._overbought),
            "resolution_minutes": str(self._resolution_minutes),
        }

    # ------------------------------------------------------------------
    # Signal-decision boundary — the bar handler split before custody
    # effects, mirroring SmaCrossoverAlgorithm's evaluate_signal_bar /
    # commit_signal_decision seam.
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
        assert self._rsi is not None

        self._rsi.update(bar.end_ms, bar.close)

        timeframe = f"{self._resolution_minutes}m"

        if not self._rsi.is_ready:
            return SignalDecision(
                intent=None,
                ready=False,
                relation_facts={
                    "rsi_below_oversold": False,
                    "rsi_above_overbought": False,
                    "was_in_position": self._in_position,
                },
                signal_facts={"decision": "HOLD", "timeframe": timeframe},
                reason_evidence={"rsi": "UNREADY"},
                action_plan_request=None,
            )

        rsi_val = self._rsi.current_value
        assert rsi_val is not None

        prior_in_position = self._in_position
        below_oversold = rsi_val < self._oversold
        above_overbought = rsi_val > self._overbought

        bar_signal = "HOLD"
        intent: SignalIntent | None = None

        if self._in_position:
            if above_overbought:
                intent = SignalIntent(kind=SignalIntentKind.EXIT, bar_close_ms=bar.end_ms, intended_price=bar.close)
                bar_signal = "EXIT"
        else:
            if below_oversold:
                intent = SignalIntent(kind=SignalIntentKind.ENTER, bar_close_ms=bar.end_ms, intended_price=bar.close)
                bar_signal = "ENTER"

        return SignalDecision(
            intent=intent,
            ready=True,
            relation_facts={
                "rsi_below_oversold": below_oversold,
                "rsi_above_overbought": above_overbought,
                "was_in_position": prior_in_position,
            },
            signal_facts={"decision": bar_signal, "timeframe": timeframe},
            reason_evidence={"rsi": str(rsi_val)},
            action_plan_request=(
                {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
            ),
        )

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None:
        """Apply one session-committed signal through the bound executor."""
        assert self.ctx is not None
        assert self._rsi is not None and self._rsi.current_value is not None
        rsi_val = self._rsi.current_value

        if intent.kind is SignalIntentKind.EXIT:
            self.ctx.emit_signal_intent(intent)
            self.ctx.log(
                f"EXIT SIGNAL: {display_time(bar.end_ms)} "
                f"Close={bar.close:.2f} RSI{self._window}={rsi_val:.2f} "
                f"> overbought({self._overbought})"
            )
            self._in_position = False
            return

        assert intent.kind is SignalIntentKind.ENTER
        self._pending_entry = _PendingEntry(rsi=rsi_val)
        self.ctx.emit_signal_intent(intent)
        self._in_position = True
        self.ctx.log(
            f"ENTRY SIGNAL: {display_time(bar.end_ms)} "
            f"Close={bar.close:.2f} RSI{self._window}={rsi_val:.2f} "
            f"< oversold({self._oversold})"
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
        indicator state (RSI) is meant to carry forward from the replay,
        but any position the replay itself would have opened is not real
        and must not leak into the live decision loop."""
        self._in_position = False
        self._pending_entry = None
        self._open_trade = None

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping
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
                entry_rsi=self._pending_entry.rsi,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {display_time(event.filled_at_ms)} "
                    f"Price={event.fill_price:.2f} "
                    f"RSI{self._window}={self._open_trade.entry_rsi:.2f}"
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
                        f"rsi_{self._window}": entry.entry_rsi,
                    },
                    signal_reason=(f"RSI({self._window}) crossed above overbought({self._overbought})"),
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
                raise RuntimeError("RSI mean-reversion exit requires a current bar time")
            self.ctx.emit_signal_intent(
                SignalIntent(
                    kind=SignalIntentKind.EXIT,
                    bar_close_ms=self.ctx.current_time_ms,
                    intended_price=self.ctx.portfolio.reference_price.get(self._symbol, Decimal(0)),
                )
            )
            self._in_position = False
