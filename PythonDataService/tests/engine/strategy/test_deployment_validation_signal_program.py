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

Bars are built on the shared calendar-anchored minute grid
(``tests/_helpers/signal_program.indexed_bucket``), so the program computes
its own detection/flatten window from the canonical NYSE calendar exactly as
it does in production. Nothing here hand-sets ``_detection_start_ms`` or
``_stop_and_flatten_ms`` from a wall-clock literal.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentValidationConsecutiveGreen,
    _session_decision_window_ms,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationStage,
    Settlement,
    SignalProgram,
)
from tests._helpers.signal_program import (
    BarBody,
    anchor_session_date,
    indexed_bucket,
    mark_bar_arrival,
    session_open_anchor_ms,
)
from tests.engine.strategy.conftest import build_program

_KEY = "deployment_validation"
_MINUTE_MS = 60_000
_NY = ZoneInfo("America/New_York")
_CLOSE = "100"
"""Every bar carries the same close: this program reads only the bar BODY
(``close > open``) and the decision clock, never the price level."""


def _minute_bar(hour: int, minute: int, body: BarBody) -> TradeBar:
    """The one-minute bar STARTING at ``hour:minute`` ET on the anchor session.

    Expressed as an index on the shared session-open grid so the bar lands
    on the same real session the program resolves from the calendar.
    """
    session_date = anchor_session_date()
    start_ms = int(datetime(session_date.year, session_date.month, session_date.day, hour, minute, tzinfo=_NY).timestamp() * 1000)
    index, remainder = divmod(start_ms - session_open_anchor_ms(), _MINUTE_MS)
    assert remainder == 0 and index >= 0, f"{hour:02d}:{minute:02d} is not a minute inside the anchor session"
    return indexed_bucket("SPY", index, _MINUTE_MS, _CLOSE, body=body)


def _flatten_barrier_bar(body: BarBody, *, clocks_past: int = 0) -> TradeBar:
    """The bar whose close IS the stop/flatten instant (``clocks_past=0``),
    or the ``clocks_past``-th minute bar after it, per the program's own
    calendar-derived window for the anchor session (15:45 ET on a regular
    day, earlier on a half day -- derived, not assumed)."""
    _detection_start_ms, stop_and_flatten_ms = _session_decision_window_ms(anchor_session_date())
    barrier_index = (stop_and_flatten_ms - session_open_anchor_ms()) // _MINUTE_MS - 1
    return indexed_bucket("SPY", barrier_index + clocks_past, _MINUTE_MS, _CLOSE, body=body)


def _prepared_program() -> tuple[SignalProgram, DeploymentValidationConsecutiveGreen, StrategyContext]:
    program, _params, _executor, context = build_program(_KEY)
    strategy = program.strategy
    assert isinstance(strategy, DeploymentValidationConsecutiveGreen)
    return program, strategy, context


def _advance(program: SignalProgram, context: StrategyContext, bar: TradeBar) -> EvaluationStage:
    mark_bar_arrival(context, bar)
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

    first = _advance(program, context, _minute_bar(9, 44, "green"))
    assert first.trace.staged_candidate is None
    program.session.settle(Settlement.COMMIT)

    second = _advance(program, context, _minute_bar(9, 45, "green"))
    assert second.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)
    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 3

    for offset, expected in ((46, "HOLD"), (47, "HOLD"), (48, "EXIT")):
        stage = _advance(program, context, _minute_bar(9, offset, "red"))
        assert stage.trace.staged_candidate == (None if expected == "HOLD" else expected)
        program.session.settle(Settlement.COMMIT)

    assert strategy._in_position is False
    assert strategy._bars_until_exit_signal == 0


def test_registry_factory_is_the_single_public_deployment_validation_program_construction_seam() -> None:
    program, strategy, _context = _prepared_program()

    assert strategy.signal_program is program
    registration = _STRATEGY_REGISTRY[_KEY]
    assert registration.build(registration.param_schema()).signal_program is not None


def test_discarded_entry_does_not_advance_the_green_streak_or_entry_pending() -> None:
    """A DISCARDed ENTER must leave both the completed green streak and
    ``_entry_pending`` exactly as ``evaluate_signal_bar`` left them -- unset --
    so the candidate can genuinely re-stage rather than silently losing the
    pattern it just detected."""
    program, strategy, context = _prepared_program()

    first_green = _advance(program, context, _minute_bar(9, 44, "green"))
    assert first_green.trace.staged_candidate is None
    program.session.settle(Settlement.COMMIT)
    assert strategy._green_streak == 1

    second_green = _advance(program, context, _minute_bar(9, 45, "green"))
    assert second_green.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.DISCARD)

    assert strategy._entry_pending is False
    assert strategy._in_position is False
    # The streak was never persisted to self on the trigger branch -- see
    # evaluate_signal_bar's docstring -- so it is still 1, the pre-trigger
    # value, not silently zeroed by the discard.
    assert strategy._green_streak == 1

    # A later bar can still complete a (fresh) two-green pattern and commit.
    third_green = _advance(program, context, _minute_bar(9, 46, "green"))
    assert third_green.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)
    assert strategy._entry_pending is True


def _enter_in_position(program: SignalProgram, strategy: DeploymentValidationConsecutiveGreen, context: StrategyContext, *, bars_until_exit: int) -> None:
    """Put the program in position through its own committed ENTER, then set
    the countdown the scenario needs. Going through the real ENTER (rather
    than poking ``_in_position`` alone) keeps the ``_entry_pending``/
    ``_in_position`` invariant the strategy asserts on."""
    _advance(program, context, _minute_bar(9, 44, "green"))
    program.session.settle(Settlement.COMMIT)
    stage = _advance(program, context, _minute_bar(9, 45, "green"))
    assert stage.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)
    assert strategy._in_position is True
    strategy._bars_until_exit_signal = bars_until_exit


def test_discarded_countdown_exit_reemits_on_the_next_eligible_clock() -> None:
    program, strategy, context = _prepared_program()
    _enter_in_position(program, strategy, context, bars_until_exit=1)

    first = _advance(program, context, _minute_bar(10, 0, "green"))
    assert first.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.DISCARD)

    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 1

    second = _advance(program, context, _minute_bar(10, 1, "green"))
    assert second.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.COMMIT)

    assert strategy._in_position is False


def test_discarded_barrier_exit_keeps_custody_and_reproposes_on_the_next_clock() -> None:
    """The session stop/flatten barrier is this program's SECOND exit path,
    distinct from the countdown and not expressible in
    ``ExitEligibilityContract``'s two rules (``fixed_bar_count_countdown``,
    ``level_true``) -- the sealed contract declares only the countdown.
    This test is the executable statement of what the barrier actually does
    so that gap is a documented vocabulary gap rather than an unverified
    assumption: a DISCARDed barrier EXIT leaves ``_in_position`` untouched,
    and the very next decision clock re-proposes it, because the barrier
    reads ``_in_position`` as a level (``prior_in_position``) on every bar
    at or past the flatten instant.
    """
    program, strategy, context = _prepared_program()
    _enter_in_position(program, strategy, context, bars_until_exit=2)

    stage = _advance(program, context, _flatten_barrier_bar("red"))
    assert stage.trace.staged_candidate == "EXIT"
    assert stage.trace.reason_evidence["barrier"] == "STOP_AND_FLATTEN"
    program.session.settle(Settlement.DISCARD)
    assert strategy._in_position is True

    reproposal = _advance(program, context, _flatten_barrier_bar("green", clocks_past=1))
    assert reproposal.trace.staged_candidate == "EXIT"
    assert reproposal.trace.reason_evidence["barrier"] == "STOP_AND_FLATTEN"


def test_rollback_blocked_entry_undoes_the_full_committed_entry_state() -> None:
    """commit_signal_decision's ENTER branch commits the full position-
    lifecycle transition at signal time -- ``_in_position`` and the exit
    countdown, not just ``_entry_pending`` -- because the live adapter never
    calls ``on_order_event`` to do it later (see that method's docstring).
    A blocked ENTER must undo all of it, or the strategy believes it holds
    a position -- with an active exit countdown -- that was never actually
    granted."""
    program, strategy, context = _prepared_program()
    _advance(program, context, _minute_bar(9, 44, "green"))
    program.session.settle(Settlement.COMMIT)
    stage = _advance(program, context, _minute_bar(9, 45, "green"))
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
    _enter_in_position(program, strategy, context, bars_until_exit=1)
    stage = _advance(program, context, _minute_bar(10, 0, "green"))
    assert stage.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.COMMIT)
    assert strategy._in_position is False
    assert strategy._bars_until_exit_signal == 0

    strategy.rollback_blocked_exit()

    assert strategy._in_position is True
    assert strategy._bars_until_exit_signal == 1

    retrigger = _advance(program, context, _minute_bar(10, 1, "green"))
    assert retrigger.trace.staged_candidate == "EXIT"
