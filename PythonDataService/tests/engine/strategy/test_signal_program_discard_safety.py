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

from typing import Any

import pytest

from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import EvaluationMode, Settlement, StageQuarantine
from tests._helpers.signal_program import (
    ROLLBACK_METHODS,
    SEALED_KEYS,
    custody_snapshot,
    custody_surface,
    indexed_bucket,
)
from tests.engine.strategy.conftest import build_program


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_discarded_evaluation_leaves_position_custody_untouched(key: str) -> None:
    # ``build_program`` warms through the session itself rather than
    # BacktestEngine -- see ``bind_strategy_context`` for why that matters
    # for these fixed-backtest-window strategies.
    program, params, _executor, _context = build_program(key)
    strategy = program.strategy
    session = program.session
    width = session.timeframe_ms

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
    surface = custody_surface(strategy)
    assert surface, (
        f"'{key}' declares no custody surface: neither {ROLLBACK_METHODS[0]} nor "
        f"{ROLLBACK_METHODS[1]} assigns any self attribute. Those methods are how a "
        "program states which fields a commit owns; without them this test has "
        "nothing to hold invariant and would pass vacuously."
    )
    before = custody_snapshot(strategy, surface)

    staged_any = False
    for offset in range(120):
        staged = session.advance(
            indexed_bucket(params.symbol, 1 + offset, width, str(100 + (offset % 11))),
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
        current = custody_snapshot(strategy, surface)
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
        stage = session.advance(indexed_bucket(symbol, start + offset, width, close), mode=EvaluationMode.DECIDE)
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
    program, params, _executor, _context = build_program("sma_crossover")
    strategy = program.strategy
    session = program.session
    width = session.timeframe_ms

    # Decline first so the seed bar records "short below long"; a monotonic
    # ramp would seed "above" and never show a *fresh* golden cross.
    falling_in = [str(300 - step * 2) for step in range(60)]
    for offset, close in enumerate(falling_in):
        stage = session.advance(indexed_bucket(params.symbol, offset + 1, width, close), mode=EvaluationMode.DECIDE)
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
        stage = session.advance(indexed_bucket(params.symbol, 400 + offset, width, close), mode=EvaluationMode.DECIDE)
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
        indexed_bucket(params.symbol, 400 + exit_offset + 1, width, falling[exit_offset + 1]),
        mode=EvaluationMode.DECIDE,
    )
    assert not isinstance(stage, StageQuarantine)
    assert any(intent.kind is SignalIntentKind.EXIT for intent in stage.intents), (
        "the discarded EXIT was never re-proposed: the strategy consumed its death cross "
        "while describing the bar, so it now holds exposure it has already decided to close."
    )
