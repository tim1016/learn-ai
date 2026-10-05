"""EMA Previous Crossover — enter on the first good bar after a cross, not only on the cross bar.

Formula: Long-only EMA(fast)/EMA(slow) crossover on 15-minute signal bars with an RSI(14) filter; fast/slow default to 5/10. A cross up (EMA(fast) > EMA(slow) on a bar whose previous bar had EMA(fast) <= EMA(slow)) opens a cross, which stays open while EMA(fast) > EMA(slow). Entry bar: the first bar of an open cross where (EMA(fast) - EMA(slow)) >= gap (default 0.20) AND the normalized gap >= gap_bps (default 0) AND rsi_min <= RSI <= rsi_max (default 50–70). That bar spends the cross whether or not the entry is taken. Exit: hold_bars consolidated decision bars after entry (default 5, i.e. 75 minutes inside one session).
Reference: The rule is the owner's (#2902, 2026-10-05) and has no outside source. The port reference is LEAN's execution of the twin `app/lean_sidecar/trusted_samples/ema_previous_crossover.py`, which supplies the consolidation, the EMAs, the RSI and the fills.
Canonical implementation: this file.
Validated against: `tests/engine/strategy/algorithms/test_ema_previous_crossover.py`; `tests/integration/reconciliation/test_ema_previous_crossover_lean_golden.py`.

Strategy rules:
  * 15-minute signal bars consolidated from minute data.
  * Indicators: EMA(fast), EMA(slow) and Wilders RSI(14), each updated with
    the consolidated bar's close at its end time.
  * A cross up opens a cross. It stays open while fast stays strictly above
    slow; a bar with fast at or below slow closes it. There is no limit on
    how long it may stay open.
  * The first bar of an open cross on which both gap floors and the RSI band
    pass is that cross's entry bar. The strategy emits ENTER there when it is
    flat. Each cross has exactly one entry bar: it is spent on that bar even
    when the strategy is holding an earlier trade, or the candidate is later
    discarded, so the entry bar of every cross depends only on the bars.
  * Position lifecycle: EXIT after exactly ``hold_bars`` consolidated
    decision bars. The countdown advances only on decision bars, so a hold
    that outlasts the session resumes on the next session's bars. Nothing
    else ends a trade. The execution boundary selects and sizes the traded
    asset.

Warmup: an EMA is ready from bar index ``period - 1`` (0-indexed), seeded
with the SMA of its first ``period`` closes; RSI(14) is ready from bar index
14. A cross counts only against a relation the previous bar established, so
fast found above slow on the first bar both EMAs are ready is not a cross.
A cross seen while RSI is still warming up is open and can enter once RSI is
ready. No intent is emitted before all three indicators are ready.

Divergence from the LEAN twin: none in the decisions, which read prices
alone. The parity test states how any difference in the two engines' cash
accounting is handled before it compares order sizes and equity.

Trade logging: trades are logged in ``on_order_event`` from actual fill
prices and times. The indicator snapshot is captured at signal time, because
it describes the decision that triggered the entry, not the state at fill
time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.framework.insight import Insight, InsightDirection
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.decision_explanation import CheckRole, Comparison, DecisionExplanation, ExplainedCheck
from app.engine.strategy.normalized_gap import difference_bps
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.utils.timestamps import display_time

DEFAULT_FAST_PERIOD = 5
DEFAULT_SLOW_PERIOD = 10
DEFAULT_HOLD_BARS = 5
RSI_PERIOD = 14


class _Cross(StrEnum):
    """Where the current fast-above-slow stretch stands."""

    NONE = "none"
    OPEN = "open"
    SPENT = "spent"


@dataclass
class _PendingEntry:
    """Indicator snapshot captured at entry signal time, consumed by the entry fill."""

    ema_fast: Decimal
    ema_slow: Decimal
    rsi: Decimal


@dataclass
class _OpenTrade:
    """An entry that has filled but not yet exited."""

    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    ema_fast: Decimal
    ema_slow: Decimal
    rsi: Decimal


def _finite(name: str, value: Decimal | float) -> Decimal:
    """Coerce a tunable to Decimal, refusing NaN and infinity.

    Every comparison against NaN is False, so a NaN threshold would not
    raise: it would silently disable the gate it configures.
    """
    coerced = Decimal(str(value))
    if not coerced.is_finite():
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return coerced


def _decision_bar_count(name: str, value: int) -> int:
    """Refuse a length or hold that is not a positive integer (``True`` is not one)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {value!r}")
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


class EmaPreviousCrossoverAlgorithm(Strategy):
    """Generate EMA previous-crossover decisions without selecting the traded asset."""

    def __init__(
        self,
        symbol: str = "SPY",
        gap: Decimal | float = Decimal("0.20"),
        rsi_min: Decimal | float = Decimal(50),
        rsi_max: Decimal | float = Decimal(70),
        gap_bps: Decimal | float = Decimal("0"),
        fast_period: int = DEFAULT_FAST_PERIOD,
        slow_period: int = DEFAULT_SLOW_PERIOD,
        hold_bars: int = DEFAULT_HOLD_BARS,
    ) -> None:
        super().__init__()
        # The signal stream, not an execution target: the Action Plan selects
        # the traded asset for live deployments.
        self._symbol_name = symbol.upper()
        # Coerced through str() so a float from the JSON param layer lands as
        # an exact Decimal. Range bounds live in the params model.
        self._gap = _finite("gap", gap)
        self._gap_bps = _finite("gap_bps", gap_bps)
        self._rsi_min = _finite("rsi_min", rsi_min)
        self._rsi_max = _finite("rsi_max", rsi_max)
        self._fast_period = _decision_bar_count("fast_period", fast_period)
        self._slow_period = _decision_bar_count("slow_period", slow_period)
        if self._fast_period >= self._slow_period:
            raise ValueError(f"fast_period must be less than slow_period, got {fast_period} and {slow_period}")
        self._hold_bars = _decision_bar_count("hold_bars", hold_bars)

        self._symbol: str = ""
        self._ema_fast: ExponentialMovingAverage | None = None
        self._ema_slow: ExponentialMovingAverage | None = None
        self._rsi: RelativeStrengthIndex | None = None

        # Decision math, advanced on every evaluated bar and rebuilt by
        # replaying bars. Never part of position custody.
        self._fast_was_above_slow: bool = False
        self._cross: _Cross = _Cross.NONE

        # Position lifecycle. The position opens and closes only when a
        # decision is committed; the countdown also steps on each held bar.
        self._in_position: bool = False
        self._bars_until_exit: int = 0
        self._pending_entry: _PendingEntry | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []
        # Set only by the registry's Signal Program factory.
        self.signal_program: SignalProgram | None = None

    def initialize(self) -> None:
        # Placeholder window: the router's overrides replace it with the
        # request's dates and cash right after initialize().
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        self._ema_fast = ExponentialMovingAverage(f"EMA{self._fast_period}", self._fast_period)
        self._ema_slow = ExponentialMovingAverage(f"EMA{self._slow_period}", self._slow_period)
        self._rsi = RelativeStrengthIndex(f"RSI{RSI_PERIOD}", RSI_PERIOD)

        self._fast_was_above_slow = False
        self._cross = _Cross.NONE
        self._in_position = False

        self.ctx.register_consolidator(
            self._symbol,
            timedelta(minutes=15),
            self._signal_program_handler(),
        )

    def signal_program_settings(self) -> dict[str, str]:
        """Every tunable that can change a decision; all take part in evaluation identity."""
        return {
            "symbol": self._symbol_name,
            "gap": str(self._gap),
            "gap_bps": str(self._gap_bps),
            "rsi_min": str(self._rsi_min),
            "rsi_max": str(self._rsi_max),
            "fast_period": str(self._fast_period),
            "slow_period": str(self._slow_period),
            "hold_bars": str(self._hold_bars),
        }

    def _gap_checks(self, ema_fast: Decimal, ema_slow: Decimal) -> tuple[ExplainedCheck, ...]:
        """Both entry floors: the absolute price gap and the normalized gap.

        Both are minimums, so ``0`` imposes no floor. The normalized floor is
        reported (and applied) only when it is set. EMA(slow) averages
        positive prices, so it is never zero; a zero one would fail the floor
        rather than divide.
        """
        spread = ema_fast - ema_slow
        absolute = ExplainedCheck(
            check_id="gap",
            role=CheckRole.ENTRY,
            comparison=Comparison.GE,
            passed=spread >= self._gap,
            observed=spread,
            threshold=self._gap,
        )
        if self._gap_bps <= 0:
            return (absolute,)
        spread_bps = None if ema_slow == 0 else difference_bps(ema_fast, ema_slow)
        normalized = ExplainedCheck(
            check_id="gap_bps",
            role=CheckRole.ENTRY,
            comparison=Comparison.GE,
            passed=spread_bps is not None and spread_bps >= self._gap_bps,
            observed=spread_bps,
            threshold=self._gap_bps,
        )
        return (absolute, normalized)

    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        """Compatibility callback for direct, non-program constructions."""
        decision = self.evaluate_signal_bar(bar)
        if decision.intent is not None:
            self.commit_signal_decision(bar, decision.intent)

    def _unready_decision(self) -> SignalDecision:
        assert self._ema_fast is not None and self._ema_slow is not None and self._rsi is not None
        return SignalDecision(
            intent=None,
            ready=False,
            relation_facts={
                "ema_fast_above_slow": False,
                "cross_open": False,
                "was_in_position": self._in_position,
            },
            signal_facts={"decision": "HOLD", "timeframe": "15m"},
            reason_evidence={
                "prior_countdown": self._bars_until_exit,
                "ema_fast": "UNREADY",
                "ema_slow": "UNREADY",
                "rsi": "UNREADY",
            },
            action_plan_request=None,
            explanation=DecisionExplanation(
                values={
                    "ema_fast": self._ema_fast.current_value if self._ema_fast.is_ready else None,
                    "ema_slow": self._ema_slow.current_value if self._ema_slow.is_ready else None,
                    "rsi": self._rsi.current_value if self._rsi.is_ready else None,
                },
                holding=self._in_position,
            ),
        )

    def evaluate_signal_bar(self, bar: TradeBar) -> SignalDecision:
        """Advance the indicators and the cross, and describe one possible action.

        Never emits an intent, and never opens or closes the position. The
        cross is advanced here, on every bar and whatever the position, so
        it is a function of the bars alone: a discarded or observe-only
        entry bar still spends its cross.
        """
        assert self._ema_fast is not None
        assert self._ema_slow is not None
        assert self._rsi is not None

        # Read before this bar's update: a cross is only a cross against a
        # relation the previous bar established.
        relation_was_known = self._ema_fast.is_ready and self._ema_slow.is_ready

        self._ema_fast.update(bar.end_ms, bar.close)
        self._ema_slow.update(bar.end_ms, bar.close)
        self._rsi.update(bar.end_ms, bar.close)

        if not (self._ema_fast.is_ready and self._ema_slow.is_ready):
            self._fast_was_above_slow = False
            self._cross = _Cross.NONE
            return self._unready_decision()

        assert self._ema_fast.current_value is not None
        assert self._ema_slow.current_value is not None
        ema_fast = self._ema_fast.current_value
        ema_slow = self._ema_slow.current_value

        fast_above_slow = ema_fast > ema_slow
        crossed_up = fast_above_slow and relation_was_known and not self._fast_was_above_slow
        if not fast_above_slow:
            self._cross = _Cross.NONE
        elif crossed_up:
            self._cross = _Cross.OPEN
        self._fast_was_above_slow = fast_above_slow

        if not self._rsi.is_ready:
            return self._unready_decision()

        assert self._rsi.current_value is not None
        rsi = self._rsi.current_value
        prior_in_position = self._in_position
        prior_countdown = self._bars_until_exit
        cross_open = self._cross is _Cross.OPEN

        if not fast_above_slow:
            cross_state = "not_above"
        elif cross_open:
            cross_state = "crossed_up" if crossed_up else "waiting"
        elif self._cross is _Cross.SPENT:
            cross_state = "spent"
        else:
            cross_state = "unseen"
        entry_checks = (
            ExplainedCheck(
                check_id="open_cross",
                role=CheckRole.ENTRY,
                comparison=Comparison.STATE,
                passed=cross_open,
                observed=cross_state,
            ),
            *self._gap_checks(ema_fast, ema_slow),
            ExplainedCheck(
                check_id="rsi_band",
                role=CheckRole.ENTRY,
                comparison=Comparison.BAND,
                passed=self._rsi_min <= rsi <= self._rsi_max,
                observed=rsi,
                threshold=(self._rsi_min, self._rsi_max),
            ),
        )
        is_entry_bar = all(check.passed for check in entry_checks)
        if is_entry_bar:
            self._cross = _Cross.SPENT

        bar_signal = "HOLD"
        intent: SignalIntent | None = None
        exit_checks: tuple[ExplainedCheck, ...] = ()
        if self._in_position:
            next_countdown = self._bars_until_exit - 1
            countdown = ExplainedCheck(
                check_id="exit_countdown",
                role=CheckRole.EXIT,
                comparison=Comparison.LE,
                passed=next_countdown <= 0,
                observed=next_countdown,
                threshold=0,
            )
            exit_checks = (countdown,)
            if countdown.passed:
                # The countdown is left where it is until the exit commits, so
                # a discarded exit is proposed again on the next bar.
                intent = SignalIntent(
                    kind=SignalIntentKind.EXIT,
                    bar_close_ms=bar.end_ms,
                    intended_price=bar.close,
                )
                bar_signal = "EXIT"
            else:
                self._bars_until_exit = next_countdown
        elif is_entry_bar:
            intent = SignalIntent(
                kind=SignalIntentKind.ENTER,
                bar_close_ms=bar.end_ms,
                intended_price=bar.close,
            )
            bar_signal = "ENTER"

        return SignalDecision(
            intent=intent,
            ready=True,
            relation_facts={
                "ema_fast_above_slow": fast_above_slow,
                "cross_open": cross_open,
                "was_in_position": prior_in_position,
            },
            signal_facts={"decision": bar_signal, "timeframe": "15m"},
            reason_evidence={
                "prior_countdown": prior_countdown,
                "ema_fast": str(ema_fast),
                "ema_slow": str(ema_slow),
                "rsi": str(rsi),
            },
            action_plan_request=(
                {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
            ),
            explanation=DecisionExplanation(
                values={"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi},
                checks=(*entry_checks, *exit_checks),
                holding=prior_in_position,
            ),
        )

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None:
        """Apply one session-committed signal through the bound executor."""
        assert self.ctx is not None
        if intent.kind is SignalIntentKind.EXIT:
            self.ctx.emit_signal_intent(intent)
            self._in_position = False
            self._bars_until_exit = 0
            self.ctx.log(f"EXIT SIGNAL: {display_time(bar.end_ms)} Close={bar.close:.2f}")
            return

        assert intent.kind is SignalIntentKind.ENTER
        assert self._ema_fast is not None and self._ema_fast.current_value is not None
        assert self._ema_slow is not None and self._ema_slow.current_value is not None
        assert self._rsi is not None and self._rsi.current_value is not None
        ema_fast = self._ema_fast.current_value
        ema_slow = self._ema_slow.current_value
        rsi = self._rsi.current_value
        self._pending_entry = _PendingEntry(ema_fast=ema_fast, ema_slow=ema_slow, rsi=rsi)
        self.ctx.emit_signal_intent(intent)
        self._in_position = True
        self._bars_until_exit = self._hold_bars

        rsi_position = (float(rsi) - float(self._rsi_min)) / (float(self._rsi_max) - float(self._rsi_min))
        confidence = 0.5 + 0.3 * (1.0 - abs(rsi_position - 0.5))
        ema_gap = ema_fast - ema_slow
        fast_label = f"EMA{self._fast_period}"
        slow_label = f"EMA{self._slow_period}"
        self.ctx.emit_insight(
            Insight.price(
                symbol=self._symbol,
                direction=InsightDirection.UP,
                period=timedelta(minutes=15 * self._hold_bars),
                magnitude=float(ema_gap / bar.close),
                confidence=round(confidence, 4),
                source_model=f"EmaPrevCross_{self._fast_period}_{self._slow_period}_RSI{RSI_PERIOD}",
                tag=f"{fast_label}={ema_fast:.4f} {slow_label}={ema_slow:.4f} RSI={rsi:.2f} Gap={ema_gap:.4f}",
            )
        )
        self.ctx.log(
            f"ENTRY SIGNAL: {display_time(bar.end_ms)} Close={bar.close:.2f} "
            f"{fast_label}={ema_fast:.4f} {slow_label}={ema_slow:.4f} Gap={ema_gap:.4f} RSI={rsi:.2f}"
        )

    def rollback_blocked_entry(self) -> None:
        """What an entry commits: the position, its countdown and its snapshot. The cross is not custody."""
        self._in_position = False
        self._bars_until_exit = 0
        self._pending_entry = None

    def rollback_blocked_exit(self) -> None:
        """What an exit commits: restoring it re-fires EXIT on the next bar."""
        self._in_position = True
        self._bars_until_exit += 1

    def on_force_flat(self) -> None:
        """Start flat after a warmup replay.

        Indicators and the cross carry forward: a cross spent during the
        replay stays spent, so a bot started mid-move does not take an entry
        its backtest never shows.
        """
        self._in_position = False
        self._bars_until_exit = 0
        self._pending_entry = None
        self._open_trade = None

    def on_order_event(self, event: OrderEvent) -> None:
        """Turn fills into trade log entries."""
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                if self.ctx is not None:
                    self.ctx.log(f"WARN: LONG fill at {display_time(event.filled_at_ms)} with no pending entry")
                return
            self._open_trade = _OpenTrade(
                entry_time_ms=event.filled_at_ms,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                ema_fast=self._pending_entry.ema_fast,
                ema_slow=self._pending_entry.ema_slow,
                rsi=self._pending_entry.rsi,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {display_time(event.filled_at_ms)} "
                    f"Price={event.fill_price:.2f} "
                    f"EMA{self._fast_period}={self._open_trade.ema_fast:.4f} "
                    f"EMA{self._slow_period}={self._open_trade.ema_slow:.4f} "
                    f"RSI={self._open_trade.rsi:.2f}"
                )
            return

        if self._open_trade is None:
            # A liquidate with no open trade; nothing to record.
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
                indicators={"ema_fast": entry.ema_fast, "ema_slow": entry.ema_slow, "rsi": entry.rsi},
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
        if not self._in_position:
            return
        assert self.ctx is not None
        if self.ctx.current_time_ms is None:
            raise RuntimeError("EMA previous-crossover exit requires a current bar time")
        self.ctx.emit_signal_intent(
            SignalIntent(
                kind=SignalIntentKind.EXIT,
                bar_close_ms=self.ctx.current_time_ms,
                intended_price=self.ctx.portfolio.reference_price.get(self._symbol, Decimal(0)),
            )
        )
        self._in_position = False
