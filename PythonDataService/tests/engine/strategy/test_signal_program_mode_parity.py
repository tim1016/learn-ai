"""Cross-mode trace parity for sealed Signal Programs (issue #1730 AC).

Acceptance criterion under test (verbatim): "Backtest, Dry Run, and guarded
Paper produce equal traces through the semantic Action Plan request from
the same qualified bars for each program."

Driven off the registry (``_STRATEGY_REGISTRY``), never a hand-listed set
of keys, for the same reason ``test_signal_program_discard_safety.py`` and
``test_signal_program_qualification_matrix.py`` are: a future promotion is
covered the moment it is registered.

What this proves, and why it collapses to two replays, not three
------------------------------------------------------------------
``app/engine/engine.py`` (Backtest) and ``app/services/bot_trade_strategy.py``
(the live runner) both register ``SignalProgram.on_consolidated_bar`` as the
consolidator callback for every sealed program (``Strategy._signal_program_handler``
in ``app/engine/strategy/base.py``), so the two only ever differ along two
axes: how the evaluation *mode* for a bar is supplied, and how the resulting
stage is *settled*.

* Backtest: ``program.activate_for_backtest()`` sets a DECIDE default
  (``engine.py`` ``run()``), and every consolidated bar is committed
  unconditionally at the engine's drain seam,
  ``program.session.commit_if_staged()`` (``engine.py``
  ``_commit_staged_signal_program``). There is no concept of a blocked or
  rejected decision in Backtest at all.
* Dry Run and Paper: ``_build_signal_strategy`` calls
  ``program.activate_for_runner()`` for *both* (``bot_trade_strategy.py``),
  and both callers -- ``run_trade_bot`` (Paper) and ``run_dry_run_bot`` (Dry
  Run) -- stream every sealed program through the identical
  ``strategy_evaluations`` generator (see
  ``test_dry_run_and_paper_share_the_same_evaluation_stream`` below, and the
  ``_STRATEGY_EVALUATION_STREAMS`` table both funnel through). Per closed
  bar, the mode comes from ``_evaluation_mode_for(feed, bar)``, which
  returns ``EvaluationMode.DECIDE`` for any ordinary, unpaused feed -- i.e.
  precisely the "qualified bar" case this criterion names. Settlement is
  ``Settlement.COMMIT`` on a bar with no intent (``NO_ACTION``:
  ``run_trade_bot`` and ``run_dry_run_bot`` both call
  ``_settle_evaluation(evaluation, Settlement.COMMIT)`` for it) and on an
  accepted intent (both callers again). The only settlement branch that
  ever diverges from COMMIT is a downstream custody refusal: Paper's
  market-liveness gate on ENTER (absent from Dry Run entirely) or a
  rejected Clerk effect (both callers, different Clerk objects) -- and a
  "qualified" bar is, by definition, one that clears that gate. So on the
  bars this test drives, Dry Run and Paper are not merely similar, they are
  mechanically the same replay: nothing reachable from the
  ``SignalProgram``/``SignalSession`` public interface can make them
  diverge here.

That is why below there are two replay helpers, not three: driving the
"runner" replay twice (labelled ``dry_run`` and ``paper``) over two
independently constructed program instances is what actually gets tested
three times, and is the strongest claim this file can make honestly.

The gap this file does NOT close
---------------------------------
Paper's market-liveness gate and both paths' real Clerk-rejection branch
live entirely outside the ``SignalProgram``/``SignalSession`` surface --
inside ``run_trade_bot``'s liveness lookup and each Clerk's
``execute_for_instance`` custody call. Proving that an *unqualified* bar
(one the liveness gate or a real broker rejection would refuse) still
leaves Dry Run and Paper's *trace* streams identical would require mocking
the Alpaca Clerk/live-liveness boundary -- a runner-level integration
concern, not something reachable synthetically at the public
``SignalProgram`` interface. That is reported here rather than faked.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from decimal import Decimal
from typing import Any

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationTrace,
    Settlement,
    SignalProgram,
    StageQuarantine,
    trace_root,
)
from app.services.bot_trade_strategy import run_dry_run_bot, run_trade_bot


class _RecordingExecutor:
    """Absorbs committed intents so a program can COMMIT without a broker."""

    def __init__(self) -> None:
        self.intents: list[SignalIntent] = []

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


def _sealed_programs() -> list[tuple[str, Any]]:
    return [(key, reg) for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]


def _bar(symbol: str, index: int, width_ms: int, close: str) -> TradeBar:
    start = index * width_ms
    price = Decimal(close)
    return TradeBar(
        symbol=symbol,
        start_ms=start,
        end_ms=start + width_ms,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=1_000,
    )


def _zigzag(n: int, *, up: float, down: float, period: int, start: float) -> list[float]:
    """A net-directional walk with a pullback every ``period``-th bar.

    A clean monotonic ramp saturates RSI toward 0/100 and starves the
    ADX-gated family (Strategy A/B/C) of the "still gated open" window
    they need; a plain small-amplitude oscillation never sustains ADX
    above its entry threshold at all. This is the shape that gave both
    a chance in practice (see the module docstring's price-path note).
    """
    values: list[float] = []
    level = start
    for index in range(n):
        level += up if index % period != period - 1 else -down
        values.append(level)
    return values


def _qualified_bar_sequence(symbol: str, width_ms: int) -> list[TradeBar]:
    """One shared price path exercising every sealed program at least once.

    Not hand-tuned per program -- this file is parametrized off the
    registry precisely so a promoted program is covered automatically,
    and a bar sequence special-cased per key would silently defeat that.
    Built empirically (see below) as one sequence, then verified to give
    every one of the six currently-sealed programs at least one non-null
    ``action_plan_request`` (``ENTER`` or ``EXIT``) before being fixed
    here, so the per-trace equality assertion in
    ``test_backtest_dry_run_paper_traces_are_equal`` is exercising real
    request payloads, not ``None == None == None``, for every
    parametrization -- enforced by that test's own
    ``saw_a_real_action_plan_request`` assertion, so a future registry
    change that stops exercising some program here fails loudly instead
    of quietly degrading to a vacuous check.

    Four segments, each earning its place against a specific gate read
    out of the sealed strategies' own source:

    1. A decline seeds a real "below trend" relation and an oversold RSI
       instead of each program's ambiguous startup default.
    2. A single sharp one-bar jump, not a smooth ramp: EMA(5)/EMA(10)
       crossover programs (``ema_crossover_signal``) require the fast/slow
       gap to already clear its absolute threshold on the very bar the
       crossover turns fresh, and RSI(14) still to be under 70 that same
       bar. A gradual ramp lets RSI clear 70 well before the slower EMA(10)
       builds enough gap; a sharp jump moves both fast enough to clear the
       gate on the same bar it was oversold.
    3. A sustained continuation gives the crossover family's own
       time-boxed exit (``ema_crossover_signal``: 5 consolidated bars) and
       the moving-average-cross exit (``sma_crossover``, ``rsi_mean_reversion``)
       room to fire, then a decline the same length exercises the death
       cross the other direction.
    4. A longer directional walk-with-pullback (see ``_zigzag``) gives the
       ADX-gated family (Strategy A/B/C: ``ADX > adx_entry_threshold``
       while ``rsi_low_gate <= RSI <= rsi_high_gate``) enough sustained
       trend for ADX to clear its threshold without RSI pinning at an
       extreme, in both directions.

    Every bar is built at the program's own ``timeframe_ms`` with a
    strictly increasing index, so it cannot hit either of
    ``SignalSession.advance``'s two quarantine reasons
    (``TIMEFRAME_MISMATCH``, ``NON_MONOTONIC_DECISION_CLOCK``).
    """
    levels: list[float] = []
    level = 300.0
    for _ in range(60):  # 1. decline -- seed "below trend", oversold RSI
        level -= 2.0
        levels.append(level)
    level += 30.0  # 2. sharp single-bar jump -- fresh crossover, gap clears, RSI still moderate
    levels.append(level)
    for _ in range(40):  # 3a. continuation -- give a time-boxed exit room to fire
        level += 1.0
        levels.append(level)
    for _ in range(60):  # 3b. decline the same way -- the other crossover direction
        level -= 3.0
        levels.append(level)
    levels += _zigzag(300, up=2.0, down=1.0, period=4, start=levels[-1])  # 4a. ADX up-trend
    levels += _zigzag(300, up=-2.0, down=-1.0, period=4, start=levels[-1])  # 4b. ADX down-trend
    return [_bar(symbol, index + 1, width_ms, f"{level:.2f}") for index, level in enumerate(levels)]


def _prepare(program: SignalProgram) -> None:
    strategy = program.strategy
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.initialize()
    strategy.ctx.set_signal_intent_executor(_RecordingExecutor())


def _replay_backtest(program: SignalProgram, bars: list[TradeBar]) -> list[EvaluationTrace]:
    """Mirror ``engine.py``'s own wiring: DECIDE always, COMMIT at the drain seam."""
    _prepare(program)
    program.activate_for_backtest()
    session = program.session
    for bar in bars:
        stage = session.advance(bar, mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine), (
            f"backtest replay quarantined a qualified bar at end_ms={bar.end_ms}: "
            f"{stage.reason if isinstance(stage, StageQuarantine) else ''}"
        )
        session.commit_if_staged()
    return list(session.traces)


def _replay_runner(program: SignalProgram, bars: list[TradeBar]) -> list[EvaluationTrace]:
    """Mirror the runner adapter both Dry Run and Paper share for a qualified bar.

    ``_evaluation_mode_for`` supplies ``EvaluationMode.DECIDE`` for any
    ordinary, unpaused feed, and both ``run_trade_bot`` and
    ``run_dry_run_bot`` settle ``Settlement.COMMIT`` on every closed bar
    that clears the liveness/Clerk custody boundary -- see the module
    docstring for the full citation trail. That is reproduced directly
    against ``SignalSession`` here, exactly as
    ``test_signal_program_discard_safety.py`` drives the session rather
    than the full runner, so a real ``StageQuarantine`` return value can be
    asserted against instead of silently absorbed the way
    ``SignalProgram.on_consolidated_bar`` absorbs one into a log line.
    """
    _prepare(program)
    program.activate_for_runner()
    session = program.session
    for bar in bars:
        stage = session.advance(bar, mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine), (
            f"runner replay quarantined a qualified bar at end_ms={bar.end_ms}: "
            f"{stage.reason if isinstance(stage, StageQuarantine) else ''}"
        )
        session.settle(Settlement.COMMIT)
    return list(session.traces)


@pytest.mark.parametrize("key", [k for k, _ in _sealed_programs()])
def test_backtest_dry_run_paper_traces_are_equal(key: str) -> None:
    registration = _STRATEGY_REGISTRY[key]
    params = registration.param_schema()
    symbol = params.symbol  # type: ignore[attr-defined]

    backtest_program = registration.signal_program_factory(params)
    width_ms = backtest_program.session.timeframe_ms
    bars = _qualified_bar_sequence(symbol, width_ms)

    backtest_traces = _replay_backtest(backtest_program, bars)
    dry_run_traces = _replay_runner(registration.signal_program_factory(params), bars)
    paper_traces = _replay_runner(registration.signal_program_factory(params), bars)

    assert len(backtest_traces) == len(bars), f"'{key}' did not stage every qualified bar in Backtest"
    assert len(dry_run_traces) == len(bars), f"'{key}' did not stage every qualified bar in Dry Run"
    assert len(paper_traces) == len(bars), f"'{key}' did not stage every qualified bar in Paper"

    assert trace_root(backtest_traces) == trace_root(dry_run_traces) == trace_root(paper_traces), (
        f"'{key}' produced different trace roots across modes on the same qualified bars"
    )

    # The criterion names the semantic Action Plan request specifically --
    # assert it directly, not only folded into the whole-sequence hash, so
    # a mismatch here points straight at the field the AC is about.
    saw_a_real_action_plan_request = False
    for offset, (bt, dr, pp) in enumerate(
        zip(backtest_traces, dry_run_traces, paper_traces, strict=True)
    ):
        assert bt.action_plan_request == dr.action_plan_request == pp.action_plan_request, (
            f"'{key}' bar {offset}: action_plan_request diverged across modes -- "
            f"backtest={bt.action_plan_request!r} dry_run={dr.action_plan_request!r} "
            f"paper={pp.action_plan_request!r}"
        )
        if bt.action_plan_request is not None:
            saw_a_real_action_plan_request = True

    # Guard the guard: if this ever goes back to False for some program, the
    # equality check above has quietly degenerated into comparing None to
    # None to None -- true, but not evidence of anything. See
    # _qualified_bar_sequence's docstring for how this sequence was built to
    # avoid exactly that for every currently-sealed program.
    assert saw_a_real_action_plan_request, (
        f"'{key}' never proposed a real action_plan_request across the whole qualified "
        "sequence -- the per-trace equality assertion above only compared None to itself"
    )


def _calls_strategy_evaluations(func: Any) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "strategy_evaluations"
        for node in ast.walk(tree)
    )


def test_dry_run_and_paper_share_the_same_evaluation_stream() -> None:
    """The architectural fact ``_replay_runner`` above relies on: Dry Run
    (``run_dry_run_bot``) and Paper (``run_trade_bot``) must both funnel a
    sealed program's closed-bar stream through the identical
    ``strategy_evaluations`` generator rather than each running its own
    bespoke replay. If a future change gives either caller its own copy,
    this is the one assertion left in this file that would notice --
    everything above would otherwise keep passing for the wrong reason
    (two runner replays that happen to still agree, not because they are
    mechanically the same code).
    """
    assert _calls_strategy_evaluations(run_trade_bot), (
        "run_trade_bot no longer calls strategy_evaluations(); the 'Paper' replay "
        "this file drives is no longer proven to represent the real Paper path"
    )
    assert _calls_strategy_evaluations(run_dry_run_bot), (
        "run_dry_run_bot no longer calls strategy_evaluations(); the 'Dry Run' replay "
        "this file drives is no longer proven to represent the real Dry Run path"
    )
