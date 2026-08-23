"""Cross-program evidence for issue #1730 acceptance criteria (A) and (B).

(A) "A suppress-one-EXIT fixture classifies each exit as level/countdown-
    derivable or ineligible; inspection alone cannot make a program Paper- or
    carryover-eligible."

    For every sealed Signal Program, this drives a real ENTER through the
    public ``SignalSession.advance``/``settle`` seam, gets the program to
    stage an EXIT, DISCARDs it, then drives one more decision clock with the
    exit condition still true. The declared ``exit_eligibility.rule`` on the
    registry's ``SignalProgramContract`` (``app.engine.strategy.registry``)
    is executably checked against whether the EXIT actually re-proposes --
    the contract's claim is proven, not trusted. A mismatch is reported as a
    real defect (see the module-level note below), never quietly absorbed by
    loosening the assertion.

    ``ema_crossover_signal``'s countdown and ``sma_crossover``'s
    fresh-death-cross level relation already have dedicated single-program
    tests (``test_ema_signal_program.py::
    test_discarded_countdown_exit_reemits_on_the_next_eligible_clock``,
    ``test_signal_program_discard_safety.py::
    test_sma_discarded_exit_stays_reproposable``). This file does not
    reproduce those -- it generalizes the same *idea* across every sealed
    program, driven off the registry so a future promotion is covered
    automatically, per ``test_signal_program_discard_safety.py``'s own
    rationale for deriving its program list.

(B) "Public-interface parity proves whether fill-event callbacks affect
    decisions; any fill-dependent program remains refused under a typed
    unsupported-feedback reason."

    ``Strategy.on_order_event`` (``app/engine/strategy/base.py``) is the
    strategy base class's sole fill-callback entry point ("Called whenever a
    pending order fills"). Every sealed program's ``on_order_event``
    override only writes ``_pending_entry`` / ``_open_trade`` / ``trade_log``
    -- fields ``evaluate_signal_bar``, ``commit_signal_decision``, and
    ``discard_signal_decision`` never read (confirmed by reading each
    program's source directly, not by inspection alone: this file executes
    the same driven bar sequence twice, once delivering a synthetic
    ``OrderEvent`` through ``on_order_event`` after every commit and once
    never calling it, and asserts the two ``EvaluationTrace`` sequences are
    equal). Equal traces are the proof of fill-independence; a program whose
    traces differ is reported as fill-dependent, per the criterion, rather
    than silently accepted.

Both tests parametrize over the registry-derived sealed-program list
(``[(k, r) for k, r in _STRATEGY_REGISTRY.items() if
r.signal_program_factory is not None]``) and fail closed -- via an explicit
assertion, not a skip -- if a newly promoted program has no driving rig
registered in ``_RIGS`` below. Per-program indicator-double setup (how to
reach ENTER/EXIT for each program's own math) is unavoidably hand-listed;
what is NOT hand-listed is the eligibility expectation itself, which is read
straight from each program's own sealed contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import count

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.strategy.algorithms.deployment_validation import _session_decision_window_ms
from app.engine.strategy.base import Strategy, StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationStage,
    EvaluationTrace,
    Settlement,
    SignalProgram,
    StageQuarantine,
)
from tests._helpers.signal_program import (
    SEALED_KEYS,
    BarBody,
    anchor_session_date,
    indexed_bucket,
    mark_bar_arrival,
    session_open_anchor_ms,
)
from tests.engine.strategy.conftest import build_program

_FLAT_CLOSE = "500"
"""Every bar this file drives carries the same close: entry and exit
conditions here are produced entirely by the installed indicator doubles
below -- or, for the one program with no indicators at all, by the bar
BODY (``_ProgramRig.bar_body``) -- never by price movement."""


# ---------------------------------------------------------------------------
# Indicator test doubles -- same pattern already used by
# test_ema_signal_program.py's ``_ReadyIndicator`` and
# test_spy_strategy_c_signal_program.py's ``_SteppingAdx``/``_ReadyRsi``: they
# isolate the custody/discard and fill-callback *plumbing* under test from
# real indicator arithmetic, which is proven elsewhere (each indicator's own
# golden fixture under app/engine/tests/fixtures/golden/). Every double is
# mutable so a single instance can be re-armed between decision clocks
# without reconstructing the strategy.
# ---------------------------------------------------------------------------


@dataclass
class _ReadyIndicator:
    """Double for single-value series (EMA/SMA/RSI): ``.update(ts_ms, value)``."""

    current_value: Decimal
    is_ready: bool = True

    def update(self, _timestamp_ms: int, _value: Decimal) -> None:
        return None


@dataclass
class _ReadyBarIndicator:
    """Double for ``BarIndicator``-shaped series (ADX): ``.update(bar)``."""

    current_value: Decimal | None
    previous_value: Decimal | None = None
    is_ready: bool = True

    def update(self, _bar: TradeBar) -> None:
        return None


@dataclass
class _ReadyMacd:
    """Double for MACD: exposes ``.macd``, not ``.current_value``."""

    macd: Decimal
    is_ready: bool = True

    def update(self, _timestamp_ms: int, _value: Decimal) -> None:
        return None


@dataclass
class _ReadySupertrend:
    """Double for Supertrend: exposes ``.is_long``, not a numeric gate."""

    is_long: bool
    current_value: Decimal | None = None
    is_ready: bool = True

    def update(self, _bar: TradeBar) -> None:
        return None


# ---------------------------------------------------------------------------
# Per-program driving rigs.
#
# ``arm_seed``/``arm_entry``/``arm_exit`` install or mutate indicator doubles
# on an already-``initialize()``d strategy. ``pre_entry_hold_bars`` is the
# number of HOLD decision clocks needed before the entry-triggering bar
# (``sma_crossover`` needs one, to seed its ``_prev_short_above_long``
# crossover-relation flag so the entry bar is a *fresh* cross rather than the
# very first observation; ``deployment_validation`` needs a derived run of
# them -- see ``_deployment_validation_pre_entry_hold_bars``).
# ``post_entry_hold_bars`` is the number of HOLD clocks needed after ENTER
# commits before the exit condition is checked (the countdown programs --
# ``ema_crossover_signal``'s fixed 5 clocks, ``deployment_validation``'s
# fixed 3 -- need these; every ``level_true`` program's exit is checked
# starting the very next clock). ``bar_body`` shapes the driven bars
# themselves, for the one program whose entry condition IS the bar body and
# which therefore has no indicator to double.
# ---------------------------------------------------------------------------


@dataclass
class _ProgramRig:
    arm_entry: Callable[[Strategy], None]
    arm_exit: Callable[[Strategy], None]
    arm_seed: Callable[[Strategy], None] = lambda _strategy: None
    pre_entry_hold_bars: int = 0
    post_entry_hold_bars: int = 0
    bar_body: BarBody = "flat"


def _arm_entry_ema(strategy: Strategy) -> None:
    strategy._ema5 = _ReadyIndicator(Decimal("500.50"))  # type: ignore[attr-defined]
    strategy._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[attr-defined]
    strategy._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[attr-defined]
    strategy._prev_ema5_above_ema10 = False  # type: ignore[attr-defined]


def _arm_exit_ema(_strategy: Strategy) -> None:
    """No-op: the fixed 5-clock countdown reaches zero on its own once the
    post-entry hold bars have run -- there is no relation to flip."""


def _arm_seed_sma(strategy: Strategy) -> None:
    strategy._sma_short = _ReadyIndicator(Decimal("99"))  # type: ignore[attr-defined]
    strategy._sma_long = _ReadyIndicator(Decimal("100"))  # type: ignore[attr-defined]


def _arm_entry_sma(strategy: Strategy) -> None:
    strategy._sma_short.current_value = Decimal("101")  # type: ignore[attr-defined]


def _arm_exit_sma(strategy: Strategy) -> None:
    strategy._sma_short.current_value = Decimal("99")  # type: ignore[attr-defined]


def _arm_entry_rsi_mean_reversion(strategy: Strategy) -> None:
    strategy._rsi = _ReadyIndicator(Decimal("20"))  # type: ignore[attr-defined]  # strictly below oversold(30)


def _arm_exit_rsi_mean_reversion(strategy: Strategy) -> None:
    strategy._rsi.current_value = Decimal("80")  # type: ignore[attr-defined]  # strictly above overbought(70)


def _arm_entry_rsi_range_common(strategy: Strategy) -> None:
    """Shared RSI-range family setup: RSI parked mid-range, ADX ready and high.

    Exit for every A/B/C member is ``ADX < adx_exit_threshold`` (15 or 20);
    entry is the subclass-specific extension below.
    """
    strategy._rsi = _ReadyIndicator(Decimal("50"))  # type: ignore[attr-defined]  # inside every [38, 70] default range
    strategy._adx = _ReadyBarIndicator(current_value=Decimal("25"))  # type: ignore[attr-defined]


def _arm_exit_rsi_range_common(strategy: Strategy) -> None:
    strategy._adx.current_value = Decimal("5")  # type: ignore[attr-defined]  # below every family's exit threshold


def _arm_entry_strategy_a(strategy: Strategy) -> None:
    _arm_entry_rsi_range_common(strategy)
    strategy._ema_fast = _ReadyIndicator(Decimal("101"))  # type: ignore[attr-defined]
    strategy._ema_slow = _ReadyIndicator(Decimal("100"))  # type: ignore[attr-defined]  # gap 1 > 0.5 threshold
    strategy._macd = _ReadyMacd(Decimal("1"))  # type: ignore[attr-defined]


def _arm_entry_strategy_b(strategy: Strategy) -> None:
    _arm_entry_rsi_range_common(strategy)
    strategy._supertrend = _ReadySupertrend(is_long=True)  # type: ignore[attr-defined]
    strategy._macd = _ReadyMacd(Decimal("1"))  # type: ignore[attr-defined]


def _arm_entry_strategy_c(strategy: Strategy) -> None:
    _arm_entry_rsi_range_common(strategy)
    strategy._adx.previous_value = Decimal("20")  # type: ignore[attr-defined]  # current(25) > previous -> rising


def _arm_noop_deployment_validation(_strategy: Strategy) -> None:
    """No-op: ``deployment_validation`` owns no indicator to double.

    Its entry gate is the bar body itself (two consecutive green minute
    bars, ``close > open``) and its exit gate is a fixed countdown, so both
    are driven entirely by ``_ProgramRig.bar_body`` and by the number of
    decision clocks the driver runs -- there is no relation to flip on the
    strategy object.
    """


def _deployment_validation_pre_entry_hold_bars() -> int:
    """HOLD decision clocks ``deployment_validation`` needs before an ENTER.

    Both terms are derived, never hardcoded:

    * the buckets whose close falls before the program's own detection
      start. That instant comes from the algorithm's own
      ``_session_decision_window_ms`` -- the same canonical-calendar-anchored
      helper the strategy calls -- evaluated on the anchor session
      ``indexed_bucket`` builds against, so an early close or a DST shift
      moves the test with the code instead of against it. Every such bucket
      is a forced HOLD (``barrier="OUTSIDE_DETECTION_WINDOW"``).
    * one further in-window green bucket, because two *consecutive* green
      bars are required and only the second stages an ENTER.
    """
    registration = _STRATEGY_REGISTRY["deployment_validation"]
    width_ms = registration.signal_program_factory(registration.param_schema()).session.timeframe_ms
    anchor_ms = session_open_anchor_ms()
    detection_start_ms, _stop_and_flatten_ms = _session_decision_window_ms(anchor_session_date())
    # Bucket ``i`` closes at ``anchor + (i + 1) * width`` and is refused by
    # the detection gate while that close is strictly before the start, so
    # the count of refused buckets is ceil(offset / width) - 1.
    detection_offset_ms = detection_start_ms - anchor_ms
    buckets_before_detection = -(-detection_offset_ms // width_ms) - 1
    return buckets_before_detection + 1


_RIGS: dict[str, _ProgramRig] = {
    "ema_crossover_signal": _ProgramRig(
        arm_entry=_arm_entry_ema,
        arm_exit=_arm_exit_ema,
        post_entry_hold_bars=4,
    ),
    "sma_crossover": _ProgramRig(
        arm_seed=_arm_seed_sma,
        arm_entry=_arm_entry_sma,
        arm_exit=_arm_exit_sma,
        pre_entry_hold_bars=1,
    ),
    "rsi_mean_reversion": _ProgramRig(
        arm_entry=_arm_entry_rsi_mean_reversion,
        arm_exit=_arm_exit_rsi_mean_reversion,
    ),
    "spy_strategy_a": _ProgramRig(arm_entry=_arm_entry_strategy_a, arm_exit=_arm_exit_rsi_range_common),
    "spy_strategy_b": _ProgramRig(arm_entry=_arm_entry_strategy_b, arm_exit=_arm_exit_rsi_range_common),
    "spy_strategy_c": _ProgramRig(arm_entry=_arm_entry_strategy_c, arm_exit=_arm_exit_rsi_range_common),
    "deployment_validation": _ProgramRig(
        arm_entry=_arm_noop_deployment_validation,
        arm_exit=_arm_noop_deployment_validation,
        pre_entry_hold_bars=_deployment_validation_pre_entry_hold_bars(),
        # `commit_signal_decision` sets `_bars_until_exit_signal` to 3 at the
        # ENTER clock; `evaluate_signal_bar` decrements it and only stages
        # EXIT when it reaches 0. So clocks 1 and 2 after ENTER are HOLDs and
        # clock 3 is the exit bar the driver advances itself -- which is
        # exactly what this program's sealed contract declares
        # (`exit_eligibility.countdown_decision_clocks=3`).
        post_entry_hold_bars=2,
        # Every driven bar is green, which is what gets the streak detector
        # to two consecutive greens. Greenness is ignored on the bars where
        # the program is already in position, so one setting covers the
        # whole drive.
        bar_body="green",
    ),
}


@dataclass(frozen=True)
class _DriveResult:
    stage: EvaluationStage
    next_offset: int


def _driven_bucket(rig: _ProgramRig, symbol: str, offset: int, width_ms: int) -> TradeBar:
    """One bar of a rig's drive, shaped by that rig's declared body."""
    return indexed_bucket(symbol, offset, width_ms, _FLAT_CLOSE, body=rig.bar_body)


def _drive_to_first_exit(
    program: SignalProgram,
    context: StrategyContext,
    rig: _ProgramRig,
    symbol: str,
    *,
    on_commit: Callable[[EvaluationStage], None] | None = None,
) -> _DriveResult:
    """Arm doubles, commit a real ENTER, then drive to the first staged EXIT.

    The EXIT stage is returned un-settled -- the caller decides whether to
    DISCARD it (criterion A) or COMMIT it (criterion B).

    ``symbol`` is the registration's own ``param_schema().symbol`` rather
    than a private attribute read off the strategy: the sealed programs do
    NOT share one such attribute (``deployment_validation`` names its two
    ``_signal_symbol_name``/``_trade_symbol_name``, because it is the one
    program that can trade a different instrument than it signals on), so
    reaching for ``strategy._symbol_name`` broke the moment a seventh
    program was promoted.
    """
    strategy = program.strategy
    session = program.session
    width = session.timeframe_ms
    offset = 0

    def _advance_commit(*, expect_candidate: str | None, label: str) -> EvaluationStage:
        nonlocal offset
        bar = _driven_bucket(rig, symbol, offset, width)
        mark_bar_arrival(context, bar)
        stage = session.advance(bar, mode=EvaluationMode.DECIDE)
        assert not isinstance(stage, StageQuarantine), f"{label}: quarantined ({stage.reason})"
        assert stage.trace.staged_candidate == expect_candidate, (
            f"{label}: expected staged_candidate={expect_candidate!r}, got "
            f"{stage.trace.staged_candidate!r} ({stage.trace.reason_evidence})"
        )
        session.settle(Settlement.COMMIT)
        offset += 1
        if on_commit is not None:
            on_commit(stage)
        return stage

    rig.arm_seed(strategy)
    for _ in range(rig.pre_entry_hold_bars):
        _advance_commit(expect_candidate=None, label="pre-entry seed bar")

    rig.arm_entry(strategy)
    _advance_commit(expect_candidate="ENTER", label="entry bar")
    assert strategy._in_position is True, "setup failed: ENTER committed but strategy is not in position"  # type: ignore[attr-defined]

    for _ in range(rig.post_entry_hold_bars):
        _advance_commit(expect_candidate=None, label="post-entry hold bar")

    rig.arm_exit(strategy)
    exit_offset = offset
    exit_bar = _driven_bucket(rig, symbol, exit_offset, width)
    mark_bar_arrival(context, exit_bar)
    stage = session.advance(exit_bar, mode=EvaluationMode.DECIDE)
    assert not isinstance(stage, StageQuarantine), f"exit bar: quarantined ({stage.reason})"
    assert stage.trace.staged_candidate == "EXIT", (
        f"exit bar: expected staged_candidate='EXIT', got {stage.trace.staged_candidate!r} "
        f"({stage.trace.reason_evidence})"
    )
    return _DriveResult(stage=stage, next_offset=exit_offset + 1)


# ---------------------------------------------------------------------------
# (A) Suppress-one-EXIT: does the declared exit_eligibility match reality?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_suppressed_exit_reproposal_matches_declared_exit_eligibility(key: str) -> None:
    registration = _STRATEGY_REGISTRY[key]
    contract = registration.signal_program_contract
    assert contract is not None, f"'{key}' has a signal_program_factory but no signal_program_contract"
    rig = _RIGS.get(key)
    assert rig is not None, (
        f"'{key}' is a sealed Signal Program with no suppress-one-EXIT rig registered in "
        "_RIGS above. A new promotion must add one and prove its declared exit_eligibility "
        "executably before this test can pass for it -- silently skipping an unknown program "
        "would be exactly the coverage loss issue #1730 exists to prevent."
    )

    program, params, _executor, context = build_program(key)
    symbol = params.symbol  # type: ignore[attr-defined]  # every param schema declares one
    session = program.session

    drive = _drive_to_first_exit(program, context, rig, symbol)
    session.settle(Settlement.DISCARD)

    reproposal_bar = _driven_bucket(rig, symbol, drive.next_offset, session.timeframe_ms)
    mark_bar_arrival(context, reproposal_bar)
    reproposal = session.advance(reproposal_bar, mode=EvaluationMode.DECIDE)
    assert not isinstance(reproposal, StageQuarantine), f"reproposal bar quarantined: {reproposal.reason}"

    declared_rule = contract.exit_eligibility.rule
    assert declared_rule in ("fixed_bar_count_countdown", "level_true"), (
        f"'{key}' declares exit_eligibility.rule={declared_rule!r}, a shape this test does not "
        "recognize as level/countdown-derivable. Extend this test before trusting the program's "
        "declaration -- do not assume a new rule value is automatically eligible."
    )
    reproposed = reproposal.trace.staged_candidate == "EXIT"
    assert reproposed, (
        f"'{key}' declares exit_eligibility.rule={declared_rule!r} (claims level/countdown-"
        "derivable, i.e. eligible for Paper/carryover) but a DISCARDed EXIT was NOT re-proposed "
        "on the next decision clock while the exit condition remained true. The declaration and "
        "the executable behavior disagree -- this is a real defect in the program or its "
        "contract, not a test bug. Do not loosen this assertion to match the code."
    )


# ---------------------------------------------------------------------------
# (B) Fill-event callback independence.
# ---------------------------------------------------------------------------


def _synthetic_fill(symbol: str, stage: EvaluationStage, intent: SignalIntent, order_id: int) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        symbol=symbol,
        fill_price=stage.bar.close,
        fill_quantity=1,
        direction=Direction.LONG if intent.kind is SignalIntentKind.ENTER else Direction.FLAT,
        fee=Decimal("0"),
        filled_at_ms=stage.bar.end_ms,
    )


def _traces_for_one_enter_exit_cycle(key: str, *, apply_fills: bool) -> list[EvaluationTrace]:
    """Run ENTER -> (holds) -> EXIT -> one post-exit observation bar, all
    COMMITted, once with fills delivered through ``Strategy.on_order_event``
    after every commit and once without, and return the resulting
    ``EvaluationTrace`` sequence for comparison.

    The trailing observation bar matters: without it, an EXIT fill's effects
    would be delivered only *after* the last trace has already been built
    (``advance()`` builds the trace before ``settle()`` runs, and the cycle
    would otherwise stop right there), which would silently leave the EXIT
    fill's own decision-independence unproven. Re-arming ``arm_entry`` and
    advancing once more gives an EXIT-fill-injected corruption one further
    decision clock to surface in, on whichever candidate (ENTER or HOLD) the
    program itself computes -- this test does not assert which.
    """
    rig = _RIGS[key]
    program, params, _executor, context = build_program(key)
    symbol = params.symbol  # type: ignore[attr-defined]  # every param schema declares one
    strategy = program.strategy
    session = program.session
    order_ids = count(1)

    def _deliver_fill(stage: EvaluationStage) -> None:
        if not apply_fills:
            return
        for intent in stage.intents:
            strategy.on_order_event(_synthetic_fill(symbol, stage, intent, next(order_ids)))

    drive = _drive_to_first_exit(program, context, rig, symbol, on_commit=_deliver_fill)
    session.settle(Settlement.COMMIT)
    _deliver_fill(drive.stage)

    rig.arm_entry(strategy)
    post_exit_bar = _driven_bucket(rig, symbol, drive.next_offset, session.timeframe_ms)
    mark_bar_arrival(context, post_exit_bar)
    post_exit_stage = session.advance(post_exit_bar, mode=EvaluationMode.DECIDE)
    assert not isinstance(post_exit_stage, StageQuarantine), (
        f"'{key}': post-exit observation bar quarantined ({post_exit_stage.reason})"
    )
    session.settle(Settlement.COMMIT)
    _deliver_fill(post_exit_stage)

    return list(session.traces)


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_fill_event_callback_does_not_change_evaluation_traces(key: str) -> None:
    rig = _RIGS.get(key)
    assert rig is not None, (
        f"'{key}' is a sealed Signal Program with no fill-independence rig registered in "
        "_RIGS above. A new promotion must add one and prove its fill-independence executably "
        "before this test can pass for it."
    )

    with_fills = _traces_for_one_enter_exit_cycle(key, apply_fills=True)
    without_fills = _traces_for_one_enter_exit_cycle(key, apply_fills=False)

    assert len(with_fills) >= 3, (
        f"'{key}': setup produced fewer than 3 traces (ENTER + EXIT + post-exit observation); "
        "nothing meaningful was compared"
    )
    assert with_fills == without_fills, (
        f"'{key}' produced a different EvaluationTrace sequence depending on whether "
        "on_order_event fill callbacks were delivered during the run. Per issue #1730 "
        "criterion (B), a fill-dependent program must remain refused under a typed "
        "unsupported-feedback reason, not promoted as fill-independent."
    )
