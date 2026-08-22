"""Shared base class for the RSI-range strategy family (A / B / C).

Formula: RSI(14) range filter [rsi_low_gate, rsi_high_gate] + ADX(14) exit gate (ADX < exit_threshold); subclass-specific entry gates added via template-method hooks.
Reference: Internal — no external port reference; RSI indicator per LEAN Wilder's method (see app/engine/indicators/rsi.py), ADX per Wilder (1978) (see app/engine/indicators/adx.py).
Canonical implementation: app/engine/strategy/algorithms/_rsi_range_base.py (base); SpyStrategyA/B/C are the leaf canonicals.
Validated against: app/engine/tests/ (exercised transitively via SpyStrategyA/B/C tests);
golden fixture ENG-008 (tests/fixtures/test_strategy_parity_fixtures.py) pins this base's
trade_log as the pre-port receipt for the #1700 intent port — the S3 port that made this
base emit ENTER/EXIT SignalIntents instead of calling set_holdings/liquidate directly. The
fixture proves the port is numerically neutral: Engine Lab's SignalSymbolExecutor maps
ENTER/EXIT back to identical set_holdings(symbol, 1)/liquidate(symbol) calls, synchronously.

All three share the same skeleton:

* 15-min consolidator (minute data → 15-min bars).
* Wilders RSI(14) + ADX(14) as the always-on shared indicators.
* Simple RSI *range* filter on entry — no state machine.
* Shared exit: ``ADX < exit_threshold``.
* Long-only. Emits instrument-free ENTER/EXIT ``SignalIntent``s; the execution
  boundary binds them to an instrument and sizing policy (100 % equity via
  ``set_holdings`` in Engine Lab; an Action Plan stock leg when deployed live).
* LEAN-style two-stage trade bookkeeping
  (pending-entry on signal → open-trade on fill → trade_log on exit fill).

On each bar while flat, all gates are evaluated; if they all pass, a
market order submits for next-bar-open fill. ``pyramiding=1`` is enforced
by the ``_in_position`` flag — the strategy will not resubmit while
holding.

Subclasses override the small extension surface:

    _init_extra_indicators()
    _extra_indicators_ready() -> bool
    _update_extra_indicators(bar)
    _entry_extra_gate_passes(bar) -> bool
    _indicator_snapshot(bar) -> dict[str, Decimal]
    _extra_signal_program_settings() -> dict[str, str]
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.adx import AverageDirectionalIndex
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.utils.timestamps import display_time


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    indicators: dict[str, Decimal]


class RsiRangeStrategy(Strategy):
    """Template-method base for the RSI-range family."""

    def __init__(
        self,
        symbol: str = "SPY",
        rsi_period: int = 14,
        rsi_low_gate: Decimal | float | int = 38,
        rsi_high_gate: Decimal | float | int = 70,
        adx_period: int = 14,
        adx_exit_threshold: Decimal | float | int = 15,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__()
        self._symbol_name = symbol.upper()
        self._symbol: str = ""
        self._resolution_minutes = resolution_minutes
        self._resolution = timedelta(minutes=resolution_minutes)

        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.rsi_low_gate = Decimal(str(rsi_low_gate))
        self.rsi_high_gate = Decimal(str(rsi_high_gate))
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError(f"rsi_low_gate ({rsi_low_gate}) must be < rsi_high_gate ({rsi_high_gate})")
        self.adx_exit_threshold = Decimal(str(adx_exit_threshold))

        self._rsi: RelativeStrengthIndex | None = None
        self._adx: AverageDirectionalIndex | None = None

        self._in_position: bool = False
        self._pending_entry: dict[str, Decimal] | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []
        # Set only by the registry's Signal Program factory. Direct
        # construction stays a compatibility surface for historical tests and
        # ledgers; public Backtest construction goes through this program.
        self.signal_program: SignalProgram | None = None

    # ------------------------------------------------------------------
    def initialize(self) -> None:
        # Sensible defaults for a standalone run — the router will override
        # these via the request body when invoked from the API, and
        # ``golden_support.strategy_replay.run_strategy_over_bars`` overrides
        # them again from its own bar series after calling this method. Same
        # convention as sma_crossover.py/ema_crossover_signal.py. Without
        # this, ``BacktestEngine.run()`` raises before ever reaching a bar
        # for any caller that (like the Signal Program corpus generator)
        # runs the strategy through its own, self-sufficient ``initialize()``
        # rather than one of the two patched-date callers above.
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)
        self._rsi = RelativeStrengthIndex(f"{self.__class__.__name__}_RSI", self.rsi_period)
        self._adx = AverageDirectionalIndex(f"{self.__class__.__name__}_ADX", self.adx_period)
        self._init_extra_indicators()
        self.ctx.register_consolidator(self._symbol, self._resolution, self._signal_program_handler())

    def _signal_program_handler(self) -> Callable[[TradeBar], None]:
        """Use the registered staged program when one owns this strategy."""
        if self.signal_program is None or not self.signal_program.active:
            return self._on_consolidated_bar
        return self.signal_program.on_consolidated_bar

    def signal_program_settings(self) -> dict[str, str]:
        """Stable RSI-range settings which participate in evaluation identity."""
        settings = {
            "symbol": self._symbol_name,
            "rsi_period": str(self.rsi_period),
            "rsi_low_gate": str(self.rsi_low_gate),
            "rsi_high_gate": str(self.rsi_high_gate),
            "adx_period": str(self.adx_period),
            "adx_exit_threshold": str(self.adx_exit_threshold),
            "resolution_minutes": str(self._resolution_minutes),
        }
        settings.update(self._extra_signal_program_settings())
        return settings

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
        assert self._adx is not None

        self._rsi.update(bar.end_ms, bar.close)
        self._adx.update(bar)
        self._update_extra_indicators(bar)

        timeframe = f"{self._resolution_minutes}m"
        prior_in_position = self._in_position
        reason_evidence = {name: str(value) for name, value in self._indicator_snapshot(bar).items()}

        # --- Exit: any existing position exits on ADX < threshold.
        if prior_in_position:
            adx_val = self._adx.current_value
            below_exit = adx_val is not None and adx_val < self.adx_exit_threshold
            intent = (
                SignalIntent(kind=SignalIntentKind.EXIT, bar_close_ms=bar.end_ms, intended_price=bar.close)
                if below_exit
                else None
            )
            return SignalDecision(
                intent=intent,
                ready=adx_val is not None,
                relation_facts={"adx_below_exit_threshold": below_exit, "was_in_position": True},
                signal_facts={"decision": "EXIT" if intent is not None else "HOLD", "timeframe": timeframe},
                reason_evidence=reason_evidence,
                action_plan_request=(
                    {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
                ),
            )

        # --- Entry: all indicators ready?
        indicators_ready = self._rsi.is_ready and self._adx.is_ready and self._extra_indicators_ready()
        rsi_val = self._rsi.current_value
        if not indicators_ready or rsi_val is None:
            return SignalDecision(
                intent=None,
                ready=False,
                relation_facts={"rsi_in_range": False, "extra_gate_passes": False, "was_in_position": False},
                signal_facts={"decision": "HOLD", "timeframe": timeframe},
                reason_evidence=reason_evidence,
                action_plan_request=None,
            )

        # --- RSI range filter + strategy-specific gates.
        rsi_in_range = self.rsi_low_gate <= rsi_val <= self.rsi_high_gate
        extra_gate_passes = self._entry_extra_gate_passes(bar)
        intent = (
            SignalIntent(kind=SignalIntentKind.ENTER, bar_close_ms=bar.end_ms, intended_price=bar.close)
            if rsi_in_range and extra_gate_passes
            else None
        )

        return SignalDecision(
            intent=intent,
            ready=True,
            relation_facts={
                "rsi_in_range": rsi_in_range,
                "extra_gate_passes": extra_gate_passes,
                "was_in_position": False,
            },
            signal_facts={"decision": "ENTER" if intent is not None else "HOLD", "timeframe": timeframe},
            reason_evidence=reason_evidence,
            action_plan_request=(
                {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
            ),
        )

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None:
        """Apply one session-committed signal through the bound executor."""
        assert self.ctx is not None
        assert self._adx is not None

        if intent.kind is SignalIntentKind.EXIT:
            assert self._adx.current_value is not None
            self.ctx.emit_signal_intent(intent)
            self.ctx.log(
                f"EXIT SIGNAL: {display_time(bar.end_ms)} "
                f"adx={float(self._adx.current_value):.2f} "
                f"< {float(self.adx_exit_threshold):.2f}"
            )
            self._in_position = False
            return

        assert intent.kind is SignalIntentKind.ENTER
        assert self._rsi is not None and self._rsi.current_value is not None
        self._pending_entry = self._indicator_snapshot(bar)
        self.ctx.emit_signal_intent(intent)
        self._in_position = True
        self.ctx.log(
            f"ENTRY SIGNAL: {display_time(bar.end_ms)} close={bar.close:.2f} "
            f"rsi={float(self._rsi.current_value):.2f}"
        )

    def discard_signal_decision(self, _bar: TradeBar, _intent: SignalIntent | None) -> None:
        """A staged candidate has no speculative lifecycle state to unwind."""
        return

    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                return
            self._open_trade = _OpenTrade(
                entry_time_ms=event.filled_at_ms,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                indicators=self._pending_entry,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(f"ENTRY: {display_time(event.filled_at_ms)} price={event.fill_price:.2f}")
            return

        # Exit fill.
        if self._open_trade is None:
            return
        entry = self._open_trade
        pnl_pts = event.fill_price - entry.entry_price
        pnl_pct = pnl_pts / entry.entry_price
        self.trade_log.append(
            LoggedTrade(
                entry_time_ms=entry.entry_time_ms,
                entry_price=entry.entry_price,
                exit_time_ms=event.filled_at_ms,
                exit_price=event.fill_price,
                quantity=entry.quantity,
                pnl_pts=pnl_pts,
                pnl_pct=pnl_pct,
                result="WIN" if pnl_pts >= 0 else "LOSS",
                indicators=entry.indicators,
            )
        )
        self._open_trade = None
        if self.ctx is not None:
            self.ctx.log(
                f"EXIT: {display_time(event.filled_at_ms)} price={event.fill_price:.2f} "
                f"pnl={pnl_pts:.2f} ({float(pnl_pct) * 100:.2f}%)"
            )

    def on_force_flat(self) -> None:
        self._in_position = False
        self._pending_entry = None
        self._open_trade = None

    def rollback_blocked_entry(self) -> None:
        """Undo lifecycle state when the live liveness gate blocks ENTER."""
        self._in_position = False
        self._pending_entry = None

    def rollback_blocked_exit(self) -> None:
        """Undo the EXIT-time state committed by ``commit_signal_decision``
        when the caller refuses to act on this signal (e.g. a Clerk admission rejection).
        ``_in_position`` flips to ``False`` at EXIT *emission*, before the
        caller knows whether the exit will actually be admitted — without
        this, a rejected EXIT leaves the strategy believing it is flat while
        the broker still holds the position, and the next eligible bar can
        emit a fresh ENTER that overwrites ``_open_trade`` for a position
        that was never actually closed."""
        self._in_position = True

    def on_end_of_algorithm(self) -> None:
        if self._in_position:
            assert self.ctx is not None
            if self.ctx.current_time_ms is None:
                raise RuntimeError(f"{self.__class__.__name__} exit requires a current bar time")
            self.ctx.emit_signal_intent(
                SignalIntent(
                    kind=SignalIntentKind.EXIT,
                    bar_close_ms=self.ctx.current_time_ms,
                    intended_price=self.ctx.portfolio.reference_price.get(self._symbol, Decimal(0)),
                )
            )
            self._in_position = False

    # ------------------------------------------------------------------
    # Subclass extension points.
    # ------------------------------------------------------------------
    def _init_extra_indicators(self) -> None: ...

    def _extra_indicators_ready(self) -> bool:
        return True

    def _update_extra_indicators(self, bar: TradeBar) -> None: ...

    def _entry_extra_gate_passes(self, bar: TradeBar) -> bool:
        return True

    def _indicator_snapshot(self, bar: TradeBar) -> dict[str, Decimal]:
        assert self._rsi is not None
        assert self._adx is not None
        snap: dict[str, Decimal] = {}
        if self._rsi.current_value is not None:
            snap["rsi"] = self._rsi.current_value
        if self._adx.current_value is not None:
            snap["adx"] = self._adx.current_value
        return snap

    def _extra_signal_program_settings(self) -> dict[str, str]:
        """Strategy-specific settings which extend ``signal_program_settings()``."""
        return {}

