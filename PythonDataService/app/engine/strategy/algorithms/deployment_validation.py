"""DeploymentValidationConsecutiveGreen — minute-bar lifecycle validation strategy.

Formula: Long-only deployment-validation strategy on 1-minute signal bars.
Detection starts 15 minutes after the session's scheduled open and the
stop/flatten barrier sits 15 minutes before the session's scheduled close —
09:45/15:45 ET on a regular session, both shifted on an NYSE early close so
the barrier is always reachable (see #1672; a fixed 15:45 ET literal is never
reached on a 13:00 ET half-day close, so the flatten safety net silently
never fires). Detect two consecutive green minute bars (close > open). After
the second green bar, submit an entry order for the configured trade symbol
intended to fill with Engine Lab's ``next_bar_open`` mode on the third bar.
Hold through the third, fourth, and fifth signal-bar closes, then submit
``Liquidate`` on the fifth bar. Reset detection state after each exit cycle.
At the stop/flatten barrier, stop detecting new entries and liquidate any
open position.
Reference: Internal strategy specification from user session 2026-06-02;
half-day cutoff contract decided in #1672 — see
``docs/references/deployment-validation-consecutive-green.md``.
Canonical implementation: this file. LEAN companion:
``app/lean_sidecar/trusted_samples/deployment_validation.py``.
Validated against: ``tests/engine/test_deployment_validation_strategy.py``,
``tests/lean_sidecar/test_deployment_validation_template.py``, and
``tests/engine/strategy/test_signal_program_qualification_matrix.py::
test_validated_settings_corpus_has_a_pinned_trace_root[deployment_validation]``.
No external golden fixture because this is an internal deployment-validation
primitive, not an alpha port.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision, SignalProgram
from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.utils.timestamps import ny_datetime

_DETECTION_START_OFFSET_MS = 15 * 60 * 1000
_STOP_AND_FLATTEN_OFFSET_MS = 15 * 60 * 1000
_BARS_FROM_ENTRY_FILL_TO_EXIT_SIGNAL = 3


def _session_decision_window_ms(session_date: date) -> tuple[int, int]:
    """Return ``(detection_start_ms, stop_and_flatten_ms)`` for ``session_date``.

    Anchored to the day's actual scheduled open/close via the canonical NYSE
    calendar (``app.lean_sidecar.trading_calendar``), not a fixed wall-clock
    literal — so an early-close session gets a real, reachable stop/flatten
    barrier instead of one that can silently never fire (#1672).
    """
    return (
        session_open_ms_utc(session_date) + _DETECTION_START_OFFSET_MS,
        session_close_ms_utc(session_date) - _STOP_AND_FLATTEN_OFFSET_MS,
    )


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    signal_time_ms: int


@dataclass(frozen=True)
class _DeploymentDecisionSnapshot:
    bar_close_ms: int
    signal: str
    intended_price: float


class DeploymentValidationConsecutiveGreen(Strategy):
    """Deterministic minute-bar strategy for validating deployment plumbing."""

    STRATEGY_KEY = "deployment_validation"
    CONSOLIDATOR_PERIOD_MIN = 1

    def __init__(self, symbol: str = "SPY", trade_symbol: str | None = None) -> None:
        super().__init__()
        self._signal_symbol_name = symbol.upper()
        self._trade_symbol_name = (trade_symbol or symbol).upper()
        self._signal_symbol: str = ""
        self._trade_symbol: str = ""

        self._current_date = None
        self._green_streak = 0
        self._entry_pending = False
        self._in_position = False
        self._stopped_for_day = False
        self._bars_until_exit_signal = 0
        self._pending_signal_time_ms: int | None = None
        self._open_trade: _OpenTrade | None = None
        self._detection_start_ms: int | None = None
        self._stop_and_flatten_ms: int | None = None

        self.trade_log: list[LoggedTrade] = []
        # Set only by the registry's Signal Program factory. Direct
        # construction stays a compatibility surface for historical tests and
        # ledgers; public Backtest construction goes through this program.
        self.signal_program: SignalProgram | None = None

    def _publish_decision(self, bar: TradeBar, signal: str) -> None:
        self.last_decision_snapshot = _DeploymentDecisionSnapshot(
            bar_close_ms=bar.end_ms,
            signal=signal,
            intended_price=float(bar.close),
        )

    def signal_program_settings(self) -> dict[str, str]:
        """Stable signal/trade-symbol settings which participate in evaluation identity."""
        return {
            "symbol": self._signal_symbol_name,
            "trade_symbol": self._trade_symbol_name,
        }

    def initialize(self) -> None:
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 4, 15)
        self.set_cash(100000)

        assert self.ctx is not None
        self._signal_symbol = self.ctx.add_equity(self._signal_symbol_name)
        self._trade_symbol = self._trade_symbol_name
        # A passthrough 1-minute consolidator keeps this strategy on the same
        # charting/order-drain path as other Engine Lab strategies. Decisions
        # are made in on_minute_bar so next_bar_open fills land on the third
        # raw minute bar after the two green confirmation bars.
        self.ctx.register_consolidator(self._signal_symbol, timedelta(minutes=1), self._on_one_minute_bar)

    def _reset_detection(self) -> None:
        self._green_streak = 0
        self._entry_pending = False
        self._pending_signal_time_ms = None

    def _reset_day(self) -> None:
        self._reset_detection()
        self._stopped_for_day = False

    def _on_one_minute_bar(self, _bar: TradeBar) -> None:
        # Decisions are intentionally driven by on_minute_bar; this handler
        # exists to retain consolidated chart bars and satisfy engine order
        # draining for market orders.
        return

    # ------------------------------------------------------------------
    # Signal-decision boundary — the bar handler split before custody
    # effects, mirroring SmaCrossoverAlgorithm's evaluate_signal_bar /
    # commit_signal_decision seam. Unlike the consolidator-driven programs,
    # this program's decision clock IS the engine's on_minute_bar hook
    # itself (a fixed 1-minute cadence) rather than a registered
    # TradeBarConsolidator callback — the 1-minute passthrough consolidator
    # registered in initialize() exists only to retain chart bars, per
    # _on_one_minute_bar's docstring above, and is untouched by this split.
    # ------------------------------------------------------------------
    def on_minute_bar(self, bar: TradeBar) -> None:
        self._signal_program_handler()(bar)

    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        """Compatibility callback for direct, non-program constructions."""
        decision = self.evaluate_signal_bar(bar)
        if decision.intent is not None:
            self.commit_signal_decision(bar, decision.intent)

    def evaluate_signal_bar(self, bar: TradeBar) -> SignalDecision:
        """Advance the day/detector/hold state and describe one possible action.

        This method never emits an intent or changes position custody state.
        The registered ``SignalSession`` owns the subsequent commit/discard,
        which makes an ``OBSERVE_ONLY`` bar permanently non-actionable even
        when an operator presses Continue immediately afterward.

        Position custody (``_in_position``, ``_entry_pending``,
        ``_pending_signal_time_ms``) is only ever mutated from
        ``commit_signal_decision``: a discarded evaluation must not advance
        it as though it had been committed.
        ``_bars_until_exit_signal`` is the deliberate exception, and the
        distinction matters because this sentence is the stated
        justification for discard safety. Its *terminal* value is never
        persisted here -- only the non-terminal countdown advance is, on the
        branch that proposes nothing -- so a discarded EXIT re-evaluates
        from the same prior countdown rather than one bar closer. The
        green-streak detector and the exit hold-counter follow
        ``EmaCrossoverSignalAlgorithm.evaluate_signal_bar``'s countdown
        pattern exactly — the terminal (trigger) value is computed locally
        and only persisted to ``self`` on the non-terminal branch, so a
        discarded ENTER/EXIT candidate re-evaluates from the same prior
        value on the next bar instead of silently advancing.
        """
        assert self.ctx is not None

        end_time = ny_datetime(bar.end_ms)
        bar_date = end_time.date()
        if self._current_date is None or bar_date != self._current_date:
            self._current_date = bar_date
            self._reset_day()
            self._detection_start_ms, self._stop_and_flatten_ms = _session_decision_window_ms(bar_date)
        assert self._detection_start_ms is not None
        assert self._stop_and_flatten_ms is not None

        prior_in_position = self._in_position
        timeframe = "1m"

        if bar.end_ms >= self._stop_and_flatten_ms:
            self._stopped_for_day = True
            self._green_streak = 0
            # Reading only ``prior_in_position`` is complete, not a
            # preserved bug. ``commit_signal_decision`` sets
            # ``_entry_pending`` and ``_in_position`` together and every
            # site that clears one clears the other, so
            # ``_entry_pending`` implies ``_in_position``: the case an
            # ``or self._entry_pending`` would add -- an order submitted
            # but not yet filled when the barrier fires -- is already
            # caught here. An earlier note called that half a preserved
            # pre-existing defect; after the promotion it describes a
            # state combination that cannot occur.
            if prior_in_position:
                intent: SignalIntent | None = SignalIntent(
                    kind=SignalIntentKind.EXIT, bar_close_ms=bar.end_ms, intended_price=bar.close
                )
                bar_signal = "EXIT"
            else:
                intent = None
                bar_signal = "HOLD"
            self._publish_decision(bar, bar_signal)
            return SignalDecision(
                intent=intent,
                ready=True,
                relation_facts={"green_streak_met": False, "was_in_position": prior_in_position},
                signal_facts={"decision": bar_signal, "timeframe": timeframe},
                reason_evidence={"barrier": "STOP_AND_FLATTEN", "prior_in_position": prior_in_position},
                action_plan_request=(
                    {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
                ),
            )

        if self._stopped_for_day or bar.end_ms < self._detection_start_ms:
            self._green_streak = 0
            self._publish_decision(bar, "HOLD")
            return SignalDecision(
                intent=None,
                ready=True,
                relation_facts={"green_streak_met": False, "was_in_position": prior_in_position},
                signal_facts={"decision": "HOLD", "timeframe": timeframe},
                reason_evidence={
                    "barrier": "OUTSIDE_DETECTION_WINDOW",
                    "stopped_for_day": self._stopped_for_day,
                },
                action_plan_request=None,
            )

        if prior_in_position:
            prior_countdown = self._bars_until_exit_signal
            next_countdown = prior_countdown - 1
            if next_countdown <= 0:
                intent = SignalIntent(kind=SignalIntentKind.EXIT, bar_close_ms=bar.end_ms, intended_price=bar.close)
                bar_signal = "EXIT"
            else:
                # Preserve the last eligible countdown until a stage
                # commits. A discarded exit therefore re-evaluates only
                # from a later bar; it never leaks a paused candidate
                # through Continue.
                self._bars_until_exit_signal = next_countdown
                intent = None
                bar_signal = "HOLD"
            self._publish_decision(bar, bar_signal)
            return SignalDecision(
                intent=intent,
                ready=True,
                relation_facts={"green_streak_met": False, "was_in_position": True},
                signal_facts={"decision": bar_signal, "timeframe": timeframe},
                reason_evidence={"prior_countdown": prior_countdown},
                action_plan_request=(
                    {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
                ),
            )

        # Not a branch: unreachable. ``_entry_pending`` implies
        # ``_in_position`` (see the barrier note above), so the
        # ``prior_in_position`` return above always wins first and this
        # decision's ``reason_evidence={"entry_pending": True}`` could never
        # be emitted. Asserted rather than deleted silently so the invariant
        # the deletion relies on fails loudly if a future edit breaks it,
        # instead of decaying into another comment that no longer holds.
        assert not self._entry_pending, "entry_pending without in_position"

        prior_streak = self._green_streak
        candidate_streak = prior_streak + 1 if bar.close > bar.open else 0

        if candidate_streak >= 2:
            # Preserve the completed streak until a stage commits, mirroring
            # the exit countdown above: a discarded ENTER re-evaluates from
            # the same completed streak on a later bar instead of losing
            # the pattern it just detected.
            intent = SignalIntent(kind=SignalIntentKind.ENTER, bar_close_ms=bar.end_ms, intended_price=bar.close)
            bar_signal = "ENTER"
        else:
            self._green_streak = candidate_streak
            intent = None
            bar_signal = "HOLD"

        self._publish_decision(bar, bar_signal)
        return SignalDecision(
            intent=intent,
            ready=True,
            relation_facts={"green_streak_met": candidate_streak >= 2, "was_in_position": False},
            signal_facts={"decision": bar_signal, "timeframe": timeframe},
            reason_evidence={
                "green_streak": candidate_streak,
                "bar_open": str(bar.open),
                "bar_close": str(bar.close),
            },
            action_plan_request=(
                {"contract": "single_long_stock", "intent": intent.kind.value} if intent is not None else None
            ),
        )

    def commit_signal_decision(self, bar: TradeBar, intent: SignalIntent) -> None:
        """Apply one session-committed signal through direct custody calls.

        Unlike ema_crossover_signal/sma_crossover (``instrument_surface=
        "policy"``), this strategy self-selects its traded instrument
        (``instrument_surface="explicit"``, the registry default), so it
        calls ``ctx.set_holdings``/``ctx.liquidate`` directly rather than
        ``ctx.emit_signal_intent``.

        Position-lifecycle custody (``_in_position``, the
        ``_bars_until_exit_signal`` countdown) is fully owned here, not by
        ``on_order_event``, mirroring every other Signal Program's
        commit-time transition (e.g. ``EmaCrossoverSignalAlgorithm.
        commit_signal_decision`` sets ``_in_position``/``_bars_until_exit``
        directly, never waiting for a fill). The pre-promotion, backtest-only
        version of this method deferred that transition to the LONG-fill
        branch of ``on_order_event`` -- harmless in a backtest, where
        ``next_bar_open`` fills always land exactly one bar after the ENTER
        decision and therefore before that next bar's own
        ``evaluate_signal_bar`` runs (verified: moving the transition here
        changes no bar's decision in the golden corpus). The live adapter
        (``app/services/bot_trade_strategy.py``) never calls
        ``on_order_event`` at all -- there is no fill simulation outside
        ``BacktestEngine`` -- so deferring the transition to a fill left a
        live-deployed bot's ``_in_position`` permanently ``False`` and its
        exit countdown never initialized: it would enter once and never
        exit. Committing the transition here fixes that for both dispatch
        paths uniformly.
        """
        assert self.ctx is not None
        end_time = ny_datetime(bar.end_ms)

        if intent.kind is SignalIntentKind.EXIT:
            self.ctx.liquidate(self._trade_symbol)
            if self._stopped_for_day:
                self.ctx.log(f"SESSION FLATTEN SIGNAL: {end_time.strftime('%Y-%m-%d %H:%M')}")
            else:
                self.ctx.log(f"EXIT SIGNAL: {end_time.strftime('%Y-%m-%d %H:%M')} Close={bar.close:.2f}")
            self._in_position = False
            self._entry_pending = False
            self._bars_until_exit_signal = 0
            self._pending_signal_time_ms = None
            return

        assert intent.kind is SignalIntentKind.ENTER
        self._pending_signal_time_ms = bar.end_ms
        # Cross-asset ``trade_symbol`` is legacy deployment metadata. Engine
        # Lab hides/rejects it because the backtest engine has one price
        # stream; the retired IBKR runner was its only execution consumer.
        self.ctx.set_holdings(self._trade_symbol, Decimal(1))
        self._entry_pending = True
        self._in_position = True
        self._bars_until_exit_signal = _BARS_FROM_ENTRY_FILL_TO_EXIT_SIGNAL
        self._green_streak = 0
        self.ctx.log(f"ENTRY SIGNAL: {end_time.strftime('%Y-%m-%d %H:%M')} Close={bar.close:.2f}")

    def rollback_blocked_entry(self) -> None:
        """Undo the ENTER-time state committed by ``commit_signal_decision``
        when the caller refuses to act on this signal (e.g. a liveness
        gate). Without this, the strategy would believe it holds a position
        -- with an active exit countdown -- that was never actually
        granted, and never re-evaluate a fresh green-bar pattern either."""
        self._entry_pending = False
        self._in_position = False
        self._bars_until_exit_signal = 0
        self._pending_signal_time_ms = None

    def rollback_blocked_exit(self) -> None:
        """Undo the EXIT-time state committed by ``commit_signal_decision``
        when the caller refuses to act on this signal (e.g. a Clerk
        admission rejection). ``_in_position`` flips to ``False`` and
        ``_bars_until_exit_signal`` is consumed at EXIT *emission* -- without
        this, a rejected EXIT leaves the strategy believing it is flat while
        the broker still holds the position. Restoring
        ``_bars_until_exit_signal`` to the pre-decrement value means the next
        bar re-fires EXIT rather than silently dropping the retry.

        The restore assigns rather than increments (issue #1736). An EXIT is
        proposed only once ``prior_countdown - 1 <= 0``, so the value being
        restored is always ``1``, and ``commit_signal_decision`` zeroed the
        field immediately before. ``+= 1`` reached the same number by relying
        on both of those facts at once; a second invocation for one refused
        EXIT would have silently restored ``2`` and delayed the retry by a
        bar. Assignment states the intended countdown directly and is
        idempotent, which is the property a custody-restore path needs."""
        self._in_position = True
        self._bars_until_exit_signal = 1

    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            # Fill-time bookkeeping only: position-lifecycle custody
            # (_in_position, the exit countdown) is already committed by
            # commit_signal_decision at signal time, not deferred to this
            # fill -- see that method's docstring for why. This callback is
            # also never invoked outside BacktestEngine, so custody state
            # cannot depend on it running.
            signal_time_ms = self._pending_signal_time_ms or event.filled_at_ms
            self._open_trade = _OpenTrade(
                entry_time_ms=event.filled_at_ms,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                signal_time_ms=signal_time_ms,
            )
            self._entry_pending = False
            if self.ctx is not None:
                self.ctx.log(f"ENTRY FILL: {ny_datetime(event.filled_at_ms):%Y-%m-%d %H:%M} Price={event.fill_price:.2f}")
            return

        if self._open_trade is None:
            return

        entry = self._open_trade
        exit_price = event.fill_price
        pnl_pts = exit_price - entry.entry_price
        pnl_pct = pnl_pts / entry.entry_price
        result = "WIN" if pnl_pts >= 0 else "LOSS"
        self.trade_log.append(
            LoggedTrade(
                entry_time_ms=entry.entry_time_ms,
                entry_price=entry.entry_price,
                exit_time_ms=event.filled_at_ms,
                exit_price=exit_price,
                quantity=entry.quantity,
                pnl_pts=pnl_pts,
                pnl_pct=pnl_pct,
                result=result,
                indicators={"signal_time_ms": Decimal(entry.signal_time_ms)},
                signal_reason="two_consecutive_green_minute_bars",
            )
        )
        if self.ctx is not None:
            self.ctx.log(f"EXIT FILL: {ny_datetime(event.filled_at_ms):%Y-%m-%d %H:%M} Price={event.fill_price:.2f}")
        self._open_trade = None
        self._in_position = False
        self._bars_until_exit_signal = 0
        self._reset_detection()

    def on_force_flat(self) -> None:
        self._open_trade = None
        self._in_position = False
        self._bars_until_exit_signal = 0
        self._reset_detection()

    def on_end_of_algorithm(self) -> None:
        if self._in_position or self._entry_pending:
            assert self.ctx is not None
            self.ctx.liquidate(self._trade_symbol)
            self._in_position = False
            self._entry_pending = False
