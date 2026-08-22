"""Contract tests for the registry-backed staged Deployment Validation Signal Program.

Golden-trace qualification coverage lives in the cross-program matrix
(``tests/engine/strategy/test_signal_program_qualification_matrix.py``); this
file proves the genuinely program-specific correctness question (issue #1730
Slice 5, last promotion): the custody split (``evaluate_signal_bar`` /
``commit_signal_decision``) must never let a DISCARDed candidate advance the
green-streak detector, the exit hold-counter, or ``_in_position`` — for both
of this program's two distinct exit paths (the fixed 3-clock countdown and
the session stop/flatten barrier) — mirroring the countdown discard/re-emit
proof ``ema_crossover_signal`` already carries for its own fixed-countdown
exit rule.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentValidationConsecutiveGreen,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationStage,
    Settlement,
    SignalProgram,
)

_NY = ZoneInfo("America/New_York")
_DAY = date(2026, 1, 5)  # a regular NYSE Monday session


def _bar(hour: int, minute: int, open_: str, close: str) -> TradeBar:
    start = datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_NY)
    end_ms = int(start.timestamp() * 1000) + 60_000
    o, c = Decimal(open_), Decimal(close)
    return TradeBar(
        symbol="SPY",
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=1_000,
    )


def _prepared_program() -> tuple[SignalProgram, DeploymentValidationConsecutiveGreen, StrategyContext]:
    registration = _STRATEGY_REGISTRY["deployment_validation"]
    assert registration.signal_program_factory is not None
    program = registration.signal_program_factory(registration.param_schema())
    strategy = program.strategy
    assert isinstance(strategy, DeploymentValidationConsecutiveGreen)
    program.activate_for_backtest()
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    return program, strategy, context


def _advance(program: SignalProgram, context: StrategyContext, bar: TradeBar) -> EvaluationStage:
    # commit_signal_decision calls ctx.set_holdings/ctx.liquidate directly
    # (instrument_surface="explicit"), which need current_time_ms set and a
    # reference price recorded -- normally both done by
    # StrategyContext.register_consolidator's own emit wrapper, bypassed
    # here since the test drives the session directly.
    context.current_time_ms = bar.end_ms
    context.portfolio.update_reference_price(bar.symbol, bar.close)
    stage = program.session.advance(bar, mode=EvaluationMode.DECIDE)
    assert isinstance(stage, EvaluationStage)
    return stage


def test_full_entry_to_exit_cycle_completes_without_on_order_event() -> None:
    """Regression: the live adapter (app/services/bot_trade_strategy.py)
    never calls ``on_order_event`` -- there is no fill simulation outside
    ``BacktestEngine``. Before this fix, ``_in_position``/the exit
    countdown were only initialized from a LONG fill inside
    ``on_order_event``, so a live-deployed bot would ENTER once and never
    become eligible to EXIT: every subsequent bar would see
    ``_in_position=False`` forever. Driving a full two-green-bar ENTER
    through three subsequent decision clocks to EXIT -- through
    ``commit_signal_decision`` alone, with ``on_order_event`` never called
    -- proves the countdown is now committed at signal time, not deferred
    to a fill this dispatch path can never produce."""
    program, strategy, context = _prepared_program()

    first = _advance(program, context, _bar(9, 44, "100", "101"))
    assert first.trace.staged_candidate is None
    program.session.settle(Settlement.COMMIT)

    second = _advance(program, context, _bar(9, 45, "101", "102"))
    assert second.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)
    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 3

    for offset, expected in ((46, "HOLD"), (47, "HOLD"), (48, "EXIT")):
        stage = _advance(program, context, _bar(9, offset, "100", "99"))
        assert stage.trace.staged_candidate == (None if expected == "HOLD" else expected)
        program.session.settle(Settlement.COMMIT)

    assert strategy._in_position is False
    assert strategy._bars_until_exit_signal == 0


def test_registry_factory_is_the_single_public_deployment_validation_program_construction_seam() -> None:
    program, strategy, _context = _prepared_program()

    assert strategy.signal_program is program
    registration = _STRATEGY_REGISTRY["deployment_validation"]
    assert registration.build(registration.param_schema()).signal_program is not None


def test_discarded_entry_does_not_advance_the_green_streak_or_entry_pending() -> None:
    """A DISCARDed ENTER must leave both the completed green streak and
    ``_entry_pending`` exactly as ``evaluate_signal_bar`` left them -- unset --
    so the candidate can genuinely re-stage rather than silently losing the
    pattern it just detected."""
    program, strategy, context = _prepared_program()

    first_green = _advance(program, context, _bar(9, 44, "100", "101"))
    assert first_green.trace.staged_candidate is None
    program.session.settle(Settlement.COMMIT)
    assert strategy._green_streak == 1

    second_green = _advance(program, context, _bar(9, 45, "101", "102"))
    assert second_green.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.DISCARD)

    assert strategy._entry_pending is False
    assert strategy._in_position is False
    # The streak was never persisted to self on the trigger branch -- see
    # evaluate_signal_bar's docstring -- so it is still 1, the pre-trigger
    # value, not silently zeroed by the discard.
    assert strategy._green_streak == 1

    # A later bar can still complete a (fresh) two-green pattern and commit.
    third_green = _advance(program, context, _bar(9, 46, "102", "103"))
    assert third_green.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)
    assert strategy._entry_pending is True


def test_discarded_countdown_exit_reemits_on_the_next_eligible_clock() -> None:
    program, strategy, context = _prepared_program()
    strategy._current_date = _DAY
    strategy._detection_start_ms, strategy._stop_and_flatten_ms = (
        int(datetime(_DAY.year, _DAY.month, _DAY.day, 9, 45, tzinfo=_NY).timestamp() * 1000),
        int(datetime(_DAY.year, _DAY.month, _DAY.day, 15, 45, tzinfo=_NY).timestamp() * 1000),
    )
    strategy._in_position = True
    strategy._bars_until_exit_signal = 1

    first = _advance(program, context, _bar(10, 0, "100", "100.5"))
    assert first.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.DISCARD)

    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 1

    second = _advance(program, context, _bar(10, 1, "100.5", "101"))
    assert second.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.COMMIT)

    assert strategy._in_position is False


def test_discarded_barrier_exit_does_not_corrupt_in_position_custody() -> None:
    """Symmetric check on the session stop/flatten barrier's own EXIT path
    (distinct code path from the fixed countdown above): a DISCARDed EXIT
    must leave ``_in_position`` exactly as it was (still True)."""
    program, strategy, context = _prepared_program()
    strategy._current_date = _DAY
    strategy._detection_start_ms, strategy._stop_and_flatten_ms = (
        int(datetime(_DAY.year, _DAY.month, _DAY.day, 9, 45, tzinfo=_NY).timestamp() * 1000),
        int(datetime(_DAY.year, _DAY.month, _DAY.day, 15, 45, tzinfo=_NY).timestamp() * 1000),
    )
    strategy._in_position = True
    strategy._bars_until_exit_signal = 2

    barrier_bar = _bar(15, 44, "100", "99")  # ends exactly at 15:45, the flatten barrier
    stage = _advance(program, context, barrier_bar)
    assert stage.trace.staged_candidate == "EXIT"
    assert stage.trace.reason_evidence["barrier"] == "STOP_AND_FLATTEN"
    program.session.settle(Settlement.DISCARD)

    assert strategy._in_position is True


def test_rollback_blocked_entry_undoes_the_full_committed_entry_state() -> None:
    """commit_signal_decision's ENTER branch commits the full position-
    lifecycle transition at signal time -- ``_in_position`` and the exit
    countdown, not just ``_entry_pending`` -- because the live adapter never
    calls ``on_order_event`` to do it later (see that method's docstring).
    A blocked ENTER must undo all of it, or the strategy believes it holds
    a position -- with an active exit countdown -- that was never actually
    granted."""
    program, strategy, context = _prepared_program()
    _advance(program, context, _bar(9, 44, "100", "101"))
    program.session.settle(Settlement.COMMIT)
    stage = _advance(program, context, _bar(9, 45, "101", "102"))
    assert stage.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)
    assert strategy._entry_pending is True
    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 3

    strategy.rollback_blocked_entry()

    assert strategy._entry_pending is False
    assert strategy._in_position is False
    assert strategy._bars_until_exit_signal == 0
    assert strategy._pending_signal_time_ms is None


def test_rollback_blocked_exit_restores_in_position_and_retriggers_next_bar() -> None:
    program, strategy, context = _prepared_program()
    strategy._current_date = _DAY
    strategy._detection_start_ms, strategy._stop_and_flatten_ms = (
        int(datetime(_DAY.year, _DAY.month, _DAY.day, 9, 45, tzinfo=_NY).timestamp() * 1000),
        int(datetime(_DAY.year, _DAY.month, _DAY.day, 15, 45, tzinfo=_NY).timestamp() * 1000),
    )
    strategy._in_position = True
    strategy._bars_until_exit_signal = 1
    stage = _advance(program, context, _bar(10, 0, "100", "100.5"))
    assert stage.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.COMMIT)
    assert strategy._in_position is False
    assert strategy._bars_until_exit_signal == 0

    strategy.rollback_blocked_exit()

    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 1

    retrigger = _advance(program, context, _bar(10, 1, "100.5", "101"))
    assert retrigger.trace.staged_candidate == "EXIT"
