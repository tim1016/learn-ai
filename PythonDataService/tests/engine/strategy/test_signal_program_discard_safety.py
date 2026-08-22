"""A DISCARDED evaluation must never leave position custody advanced.

``SignalSession`` stages a decision and only applies its effect when the
runner settles ``Settlement.COMMIT``; ``Settlement.DISCARD`` means the
candidate was never acted on. If a strategy mutates its own position state
inside ``evaluate_signal_bar`` rather than ``commit_signal_decision``, a
discarded candidate leaves the strategy believing it holds a position it
never opened -- and every later decision clock reads that corrupted flag.
The bot keeps running and simply decides wrongly, which is the hardest
shape of failure to notice in production.

This is driven off the registry rather than a hand-listed set of keys.
``sma_crossover`` was promoted with no coverage for this bug class at all
(an injected violation left all 294 of its tests passing), while its five
siblings were protected only incidentally, by tests written for other
reasons. A per-program copy would have kept that coverage a matter of who
remembered; deriving the program list means the next promotion is covered
the moment it is registered.
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
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import EvaluationMode, Settlement, StageQuarantine

# The custody surface is DERIVED, not hand-listed. ``rollback_blocked_entry``
# and ``rollback_blocked_exit`` exist for exactly one purpose -- to undo the
# custody a commit applied -- so the attributes they assign are that
# program's authoritative statement of what "position custody" means for it.
#
# An earlier version of this file hard-coded
# ``("_in_position", "_pending_entry", "_open_trade", "_bars_until_exit")``
# and read it with ``hasattr``, so any program whose real attribute names
# differed was silently only partly checked -- the test still reported green.
# ``deployment_validation`` names its own fields ``_entry_pending`` and
# ``_bars_until_exit_signal``; both were invisible to that tuple, and
# injected corruption of either PASSED. Deriving the names from the
# program's own rollback methods makes that class of miss impossible instead
# of fixing the two instances.
_ROLLBACK_METHODS = ("rollback_blocked_entry", "rollback_blocked_exit")

# ...minus whatever the describe phase itself maintains. ``evaluate_signal_bar``
# legitimately tracks *market relation* state on every bar it reads -- SMA's
# ``_prev_short_above_long``, EMA's ``_prev_ema5_above_ema10`` -- and a program
# may also restore that relation on discard, which puts the same name in both
# sets. Holding such a name invariant across a run of discarded bars would be
# asserting that the market never moved, so this test would fail on correct
# code. Custody is the narrower thing only a commit may advance, so the
# describe-maintained names are subtracted out.
#
# This does not silently drop coverage of the subtracted names: EMA's
# ``_bars_until_exit`` preservation is pinned directly by
# ``test_ema_signal_program.py``, and SMA's discarded-EXIT relation restore by
# ``test_sma_discarded_exit_stays_reproposable`` below. Both are *behavioral*
# assertions, which is the right shape for state that is supposed to move.
_DESCRIBE_METHOD = "evaluate_signal_bar"


def _assigned_self_attrs(strategy: object, method_name: str) -> frozenset[str]:
    """Return the ``self.<attr>`` names one method of ``strategy`` assigns."""
    method = getattr(type(strategy), method_name, None)
    if method is None:
        return frozenset()
    names: set[str] = set()
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)
    return frozenset(names)


def _custody_surface(strategy: object) -> frozenset[str]:
    """Names the rollback methods restore that the describe phase does not maintain."""
    restored: set[str] = set()
    for method_name in _ROLLBACK_METHODS:
        restored |= _assigned_self_attrs(strategy, method_name)
    return frozenset(restored) - _assigned_self_attrs(strategy, _DESCRIBE_METHOD)


class _RecordingExecutor:
    """Absorbs committed intents so warmup can COMMIT without a broker."""

    def __init__(self) -> None:
        self.intents: list[SignalIntent] = []

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


def _sealed_programs() -> list[tuple[str, Any]]:
    return [(key, reg) for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]


def _custody_snapshot(strategy: object, surface: frozenset[str]) -> dict[str, object]:
    # getattr without a default on purpose: every derived name must really
    # exist. A rollback method assigning an attribute the instance does not
    # have is itself a defect, and silently skipping it is how the previous
    # version of this test lost coverage.
    return {name: repr(getattr(strategy, name)) for name in sorted(surface)}


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


@pytest.mark.parametrize("key", [k for k, _ in _sealed_programs()])
def test_discarded_evaluation_leaves_position_custody_untouched(key: str) -> None:
    registration = _STRATEGY_REGISTRY[key]
    params = registration.param_schema()
    program = registration.signal_program_factory(params)
    strategy = program.strategy
    width = program.session.timeframe_ms

    # Warm through the session itself rather than BacktestEngine: these
    # strategies declare a fixed backtest window, so synthetic bars outside
    # it would be filtered out and the indicators would never advance --
    # setup that looks like warmup while doing nothing. Driving the session
    # directly warms real indicator state and exercises the same staged
    # advance/settle path the runner uses.
    session = program.session
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.initialize()
    strategy.ctx.set_signal_intent_executor(_RecordingExecutor())

    # Snapshot while the strategy is still flat, then drive a long run of
    # evaluations that are ALL discarded. The invariant is that no sequence
    # of discarded candidates may advance custody -- so custody must still
    # equal this initial flat state at the end.
    #
    # Snapshotting after the warmup instead would be vacuous: a strategy
    # that wrongly sets its custody flag inside evaluate_signal_bar would
    # have corrupted it during warmup too, and the comparison would match
    # a corrupted value against itself. Both weaker formulations were tried
    # and confirmed to pass with the violation injected.
    surface = _custody_surface(strategy)
    assert surface, (
        f"'{key}' declares no custody surface: neither {_ROLLBACK_METHODS[0]} nor "
        f"{_ROLLBACK_METHODS[1]} assigns any self attribute. Those methods are how a "
        "program states which fields a commit owns; without them this test has "
        "nothing to hold invariant and would pass vacuously."
    )
    before = _custody_snapshot(strategy, surface)

    staged_any = False
    for offset in range(120):
        staged = session.advance(
            _bar(params.symbol, 1 + offset, width, str(100 + (offset % 11))),
            mode=EvaluationMode.DECIDE,
        )
        # Fail rather than skip. A quarantine here means this test stopped
        # exercising the program while still reporting green -- the silent
        # coverage loss this file exists to prevent. Bars are built at the
        # session's own timeframe_ms, so a quarantine is a real signal.
        assert not isinstance(staged, StageQuarantine), (
            f"'{key}' quarantined a synthetic bar at offset {offset} "
            f"({staged.reason if isinstance(staged, StageQuarantine) else ''}). "
            "Give this program a bar shape it accepts; do not let it drop out."
        )
        staged_any = True
        session.settle(Settlement.DISCARD)

        # Compare after EVERY discard, not only at the end. An endpoint-only
        # check is blind to any violation that self-cancels over the loop:
        # a strategy toggling _in_position on each evaluation is wrong after
        # 60 of these 120 bars, yet nets back to its initial value and the
        # old formulation passed. Verified against exactly that injection.
        current = _custody_snapshot(strategy, surface)
        assert current == before, (
            f"'{key}' advanced position custody during an evaluation that was "
            f"DISCARDED (bar {offset} of 120): "
            f"{ {k: (before[k], current[k]) for k in before if before[k] != current[k]} }. "
            "Custody state belongs in commit_signal_decision, never evaluate_signal_bar."
        )

    assert staged_any, f"'{key}' never staged an evaluation; nothing was discarded"


def _drive_until(
    session: Any,
    symbol: str,
    width: int,
    closes: list[str],
    start: int,
    kind: SignalIntentKind,
) -> int:
    """COMMIT bars until ``kind`` is proposed and committed; return its offset."""
    for offset, close in enumerate(closes):
        stage = session.advance(_bar(symbol, start + offset, width, close), mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine), f"quarantined at offset {offset}"
        session.settle(Settlement.COMMIT)
        if any(intent.kind is kind for intent in stage.intents):
            return offset
    raise AssertionError(f"setup failed: {kind} was never proposed across {len(closes)} bars")


def test_sma_discarded_exit_stays_reproposable() -> None:
    """A DISCARDED death-cross EXIT must be re-proposed while the cross holds.

    ``evaluate_signal_bar`` advances ``_prev_short_above_long`` to describe
    the bar it just read. Before ``discard_signal_decision`` restored it, that
    advance consumed the death cross even when the EXIT was never acted on:
    the next clock computed ``fresh_death_cross = False`` and the strategy
    kept its position -- real broker exposure -- until an entire
    golden-cross/death-cross cycle completed. Pausing a bot across a death
    cross therefore silently turned a decided exit into a held position.

    This asserts the behaviour, not the attribute: the attribute is *supposed*
    to move on every ordinary bar, so only its value across a discarded EXIT
    is invariant.
    """
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    params = registration.param_schema()
    program = registration.signal_program_factory(params)
    strategy = program.strategy
    session = program.session
    width = session.timeframe_ms

    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.initialize()
    strategy.ctx.set_signal_intent_executor(_RecordingExecutor())

    # Decline first so the seed bar records "short below long"; a monotonic
    # ramp would seed "above" and never show a *fresh* golden cross.
    falling_in = [str(300 - step * 2) for step in range(60)]
    for offset, close in enumerate(falling_in):
        stage = session.advance(_bar(params.symbol, offset + 1, width, close), mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine)
        session.settle(Settlement.COMMIT)
    assert strategy._prev_short_above_long is False, "setup failed: decline did not seed short-below-long"

    # Ramp up into a fresh golden cross and take the entry.
    rising = [str(180 + step * 4) for step in range(80)]
    _drive_until(session, params.symbol, width, rising, 200, SignalIntentKind.ENTER)
    assert strategy._in_position, "setup failed: strategy is not holding a position"

    # Fall until the death cross fires, and DISCARD that EXIT.
    falling = [str(500 - step * 6) for step in range(80)]
    exit_offset = None
    for offset, close in enumerate(falling):
        stage = session.advance(_bar(params.symbol, 400 + offset, width, close), mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine)
        if any(intent.kind is SignalIntentKind.EXIT for intent in stage.intents):
            session.settle(Settlement.DISCARD)
            exit_offset = offset
            break
        session.settle(Settlement.COMMIT)
    assert exit_offset is not None, "setup failed: the decline never produced an EXIT to discard"
    assert strategy._in_position, "a DISCARDED exit must leave the position held"

    # The very next bar keeps short below long, so the exit is still owed.
    stage = session.advance(
        _bar(params.symbol, 400 + exit_offset + 1, width, falling[exit_offset + 1]),
        mode=EvaluationMode.DECIDE,
    )
    assert not isinstance(stage, StageQuarantine)
    assert any(intent.kind is SignalIntentKind.EXIT for intent in stage.intents), (
        "the discarded EXIT was never re-proposed: the strategy consumed its death cross "
        "while describing the bar, so it now holds exposure it has already decided to close."
    )
