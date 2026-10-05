"""The EMA Previous Crossover entry rule, one branch per test (#2902).

A cross up opens a cross that stays open while fast stays above slow. Its
first bar where the gap floors and the RSI band pass is its entry bar, and
that bar spends the cross whether or not the entry is taken. A position is
held for exactly ``hold_bars`` decision bars.

Each test scripts the three indicator readings bar by bar and reads what the
Signal Session staged, so the rule is exercised apart from EMA/RSI math.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.decision_explanation import CheckRole
from app.engine.strategy.programs.ema_crossover_signal import (
    EmaCrossoverSignalParams,
    build_ema_crossover_signal_program,
)
from app.engine.strategy.programs.ema_previous_crossover import (
    EmaPreviousCrossoverParams,
    build_ema_previous_crossover_program,
)
from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import (
    EvaluationMode,
    Settlement,
    SignalDecision,
    SignalProgram,
    StageQuarantine,
)
from app.lean_sidecar import trading_calendar
from scripts.generate_signal_program_trace_corpus import _DEFAULT_CELLS_ROOT, _minute_bars
from tests._helpers.signal_program import bind_strategy_context, bucket, indexed_bucket, mark_bar_arrival
from tests.fixtures.golden_support.strategy_replay import staged_decisions

_WIDTH_MS = 15 * 60_000
_SLOW = Decimal("100")
ENTER = SignalIntentKind.ENTER
EXIT = SignalIntentKind.EXIT

# One bar's readings: (fast EMA, slow EMA, RSI). ``None`` is "not ready yet".
Reading = tuple[Decimal | None, Decimal | None, Decimal | None]


def below(rsi: str | None = "60") -> Reading:
    """Fast under slow."""
    return (_SLOW - Decimal("0.10"), _SLOW, None if rsi is None else Decimal(rsi))


def above(gap: str, rsi: str | None = "60") -> Reading:
    """Fast over slow by ``gap``."""
    return (_SLOW + Decimal(gap), _SLOW, None if rsi is None else Decimal(rsi))


GOOD = above("0.25")  # clears the default 0.20 gap and the 50-70 band


@dataclass
class _Scripted:
    """An indicator that reads the next scripted value on each update."""

    values: list[Decimal | None]
    index: int = -1

    def update(self, _timestamp_ms: int, _value: Decimal) -> None:
        self.index += 1

    @property
    def current_value(self) -> Decimal | None:
        return self.values[self.index]

    @property
    def is_ready(self) -> bool:
        return self.index >= 0 and self.values[self.index] is not None


@dataclass
class _Run:
    program: SignalProgram
    decisions: list[SignalDecision] = field(default_factory=list)

    @property
    def staged(self) -> list[SignalIntentKind | None]:
        return [None if d.intent is None else d.intent.kind for d in self.decisions]

    def cross_states(self) -> list[object]:
        return [
            next(check.observed for check in d.explanation.checks if check.check_id == "open_cross")
            for d in self.decisions
            if d.explanation is not None and d.ready
        ]


def _drive(
    tape: list[Reading],
    *,
    discard: frozenset[int] = frozenset(),
    observe_only: frozenset[int] = frozenset(),
    force_flat_before: frozenset[int] = frozenset(),
    bars: list[TradeBar] | None = None,
    **params: object,
) -> _Run:
    """Play ``tape`` through the program's own session, one decision bar per reading."""
    program = build_ema_previous_crossover_program(EmaPreviousCrossoverParams(**params))
    strategy = program.strategy
    context, _executor = bind_strategy_context(strategy)
    strategy._ema_fast = _Scripted([row[0] for row in tape])  # type: ignore[attr-defined]
    strategy._ema_slow = _Scripted([row[1] for row in tape])  # type: ignore[attr-defined]
    strategy._rsi = _Scripted([row[2] for row in tape])  # type: ignore[attr-defined]
    run = _Run(program)
    for index in range(len(tape)):
        if index in force_flat_before:
            strategy.on_force_flat()
        bar = bars[index] if bars is not None else indexed_bucket("SPY", index, _WIDTH_MS, "100")
        mark_bar_arrival(context, bar)
        mode = EvaluationMode.OBSERVE_ONLY if index in observe_only else EvaluationMode.DECIDE
        stage = program.session.advance(bar, mode=mode)
        assert not isinstance(stage, StageQuarantine), stage
        if mode is EvaluationMode.DECIDE:
            program.session.settle(Settlement.DISCARD if index in discard else Settlement.COMMIT)
        run.decisions.append(stage.decision)
    return run


def test_cross_bar_that_already_clears_the_gates_is_the_entry_bar() -> None:
    assert _drive([below(), GOOD]).staged == [None, ENTER]


def test_entry_waits_for_the_gap_to_widen_after_the_cross() -> None:
    run = _drive([below(), above("0.05"), above("0.15"), above("0.25")])

    assert run.staged == [None, None, None, ENTER]


def test_entry_waits_for_rsi_to_come_back_inside_the_band() -> None:
    run = _drive([below(), above("0.25", "75"), above("0.25", "72"), above("0.25", "65")])

    assert run.staged == [None, None, None, ENTER]


def test_gap_and_rsi_must_pass_on_the_same_bar() -> None:
    """The owner's worked example: the gap passes on bar 3 and RSI on bar 4, but only bar 5 has both."""
    run = _drive(
        [
            below(),
            above("0.05", "48"),
            above("0.15", "55"),
            above("0.25", "72"),
            above("0.18", "65"),
            above("0.22", "60"),
        ]
    )

    assert run.staged == [None, None, None, None, None, ENTER]
    assert run.cross_states() == ["not_above", "crossed_up", "waiting", "waiting", "waiting", "waiting"]


@pytest.mark.parametrize(
    ("reading", "params"),
    [
        (above("0.20", "70"), {}),
        (above("0.20", "50"), {}),
        (above("0.20"), {"gap": 0.0, "gap_bps": 20.0}),
    ],
    ids=["gap-floor-and-rsi-upper", "rsi-lower", "normalized-gap-floor"],
)
def test_gap_floors_and_rsi_band_are_inclusive(reading: Reading, params: dict[str, float]) -> None:
    assert _drive([below(), reading], **params).staged == [None, ENTER]


def test_normalized_gap_floor_applies_on_the_entry_bar() -> None:
    # 20 bps of a 100 slow EMA is 0.20.
    run = _drive([below(), above("0.10"), above("0.25")], gap=0.0, gap_bps=20.0)

    assert run.staged == [None, None, ENTER]


def test_position_is_held_for_exactly_hold_bars_and_the_cross_gives_no_second_entry() -> None:
    run = _drive([below(), GOOD, GOOD, GOOD, GOOD, GOOD], hold_bars=2)

    assert run.staged == [None, ENTER, None, EXIT, None, None]
    assert run.cross_states()[-2:] == ["spent", "spent"]


def test_reversed_cross_during_the_hold_does_not_end_the_trade() -> None:
    run = _drive([below(), GOOD, below(), below(), below()], hold_bars=3)

    assert run.staged == [None, ENTER, None, None, EXIT]


def test_fast_touching_slow_closes_the_cross_and_the_next_cross_enters() -> None:
    touching: Reading = (_SLOW, _SLOW, Decimal("60"))
    run = _drive([below(), GOOD, GOOD, GOOD, touching, GOOD], hold_bars=1)

    assert run.staged == [None, ENTER, EXIT, None, None, ENTER]


def test_cross_whose_first_good_bar_falls_inside_a_hold_is_spent() -> None:
    run = _drive([below(), GOOD, below(), GOOD, GOOD, GOOD, GOOD], hold_bars=4)

    assert run.staged == [None, ENTER, None, None, None, EXIT, None]


def test_cross_opened_during_a_hold_enters_when_its_first_good_bar_comes_after_the_exit() -> None:
    run = _drive([below(), GOOD, below(), above("0.05"), GOOD], hold_bars=2)

    assert run.staged == [None, ENTER, None, EXIT, ENTER]


def test_discarded_entry_is_not_proposed_again_on_the_same_cross() -> None:
    run = _drive([below(), GOOD, GOOD, GOOD], discard=frozenset({1}))

    assert run.staged == [None, ENTER, None, None]


def test_observe_only_entry_bar_stays_missed() -> None:
    run = _drive([below(), GOOD, GOOD], observe_only=frozenset({1}))

    assert run.staged == [None, ENTER, None]


def test_discarded_exit_is_proposed_again_on_the_next_bar() -> None:
    run = _drive([below(), GOOD, GOOD, GOOD], hold_bars=1, discard=frozenset({2}))

    assert run.staged == [None, ENTER, EXIT, EXIT]


def test_cross_seen_while_rsi_warms_up_enters_once_rsi_is_ready() -> None:
    run = _drive([below(None), above("0.25", None), GOOD])

    assert [d.ready for d in run.decisions] == [False, False, True]
    assert run.staged == [None, None, ENTER]


def test_fast_already_above_slow_when_first_known_is_never_an_entry() -> None:
    run = _drive([GOOD, GOOD, GOOD])

    assert run.staged == [None, None, None]
    assert run.cross_states() == ["unseen", "unseen", "unseen"]


def test_force_flat_drops_the_position_and_keeps_the_cross_spent() -> None:
    """Bar 2 stays out because the cross is spent; bar 4 enters because the position is gone."""
    run = _drive([below(), GOOD, GOOD, below(), GOOD], force_flat_before=frozenset({2}))

    assert run.staged == [None, ENTER, None, None, ENTER]


def test_hold_counts_decision_bars_across_the_session_close() -> None:
    first, second = [
        window.session_date
        for window in trading_calendar.session_windows_ms_utc(date(2024, 1, 2), date(2024, 1, 12))
    ][:2]
    last_two = _session_bars(first)[-2:]
    next_day = _session_bars(second)[:5]

    run = _drive([below(), GOOD, GOOD, GOOD, GOOD, GOOD, GOOD], bars=[*last_two, *next_day], hold_bars=5)

    assert run.staged == [None, ENTER, None, None, None, None, EXIT]
    exit_intent = run.decisions[-1].intent
    assert exit_intent is not None
    assert exit_intent.bar_close_ms == next_day[4].end_ms


def _session_bars(day: date) -> list[TradeBar]:
    """Every decision bar of one scheduled session, bounds from the canonical calendar."""
    open_ms = trading_calendar.session_open_ms_utc(day)
    close_ms = trading_calendar.session_close_ms_utc(day)
    return [bucket("SPY", start, start + _WIDTH_MS, "100") for start in range(open_ms, close_ms, _WIDTH_MS)]


def test_two_holds_are_two_evaluation_identities() -> None:
    def evaluation_id(**params: object) -> str:
        return _drive([below()], **params).program.session.traces[0].evaluation_id

    assert evaluation_id(hold_bars=5) != evaluation_id(hold_bars=6)
    assert evaluation_id(fast_period=5, slow_period=10) != evaluation_id(fast_period=4, slow_period=10)


# ── Tie-back to EMA Crossover Signal ─────────────────────────────────────────

_TIE_BACK_CELL = "SPY_W3mo_2026-02-02_to_2026-04-30"


def _entry_gates_pass(decision: SignalDecision) -> bool:
    explanation = decision.explanation
    assert explanation is not None
    entry = [check for check in explanation.checks if check.role is CheckRole.ENTRY]
    return decision.ready and bool(entry) and all(check.passed for check in entry)


@pytest.mark.parametrize(("gap", "rsi_min"), [(0.20, 50.0), (0.0, 30.0)], ids=["lean-parity-point", "no-gap-floor"])
def test_every_ema_crossover_signal_entry_bar_is_an_entry_bar_of_the_same_cross(gap: float, rsi_min: float) -> None:
    """On shared bars, the bars where EMA Crossover Signal's entry rule passes are
    exactly this program's entry bars that fall on the cross bar itself.

    Both sides are read from the gates, not from the trades, so the statement
    is about prices alone and holds whatever either strategy was holding.
    """
    minute_bars = _minute_bars(Path(_DEFAULT_CELLS_ROOT) / _TIE_BACK_CELL, "SPY")
    settings = {"symbol": "SPY", "gap": gap, "rsi_min": rsi_min}
    reference = staged_decisions(
        build_ema_crossover_signal_program(EmaCrossoverSignalParams(**settings)).strategy, minute_bars
    )
    candidate = staged_decisions(
        build_ema_previous_crossover_program(EmaPreviousCrossoverParams(**settings)).strategy, minute_bars
    )
    assert [bar.end_ms for bar, _ in reference] == [bar.end_ms for bar, _ in candidate]

    reference_entry_bars = {bar.end_ms for bar, decision in reference if _entry_gates_pass(decision)}
    candidate_cross_bar_entries = {
        bar.end_ms
        for bar, decision in candidate
        if _entry_gates_pass(decision)
        and any(c.check_id == "open_cross" and c.observed == "crossed_up" for c in decision.explanation.checks)
    }

    assert reference_entry_bars, "the cell exercises the reference rule"
    assert candidate_cross_bar_entries == reference_entry_bars
