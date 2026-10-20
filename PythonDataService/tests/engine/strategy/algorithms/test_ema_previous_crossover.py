"""The EMA Previous Crossover entry rule, one branch per test case (#2902).

A cross up is remembered by its bar's close time while fast stays above slow.
While flat, a bar enters when it closes at most ``cross_max_age_bars``
15-minute bars of clock time after the cross bar and the gap floors and the
RSI band pass on that same bar. Nothing else is remembered. A position is
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
TOUCHING: Reading = (_SLOW, _SLOW, Decimal("60"))  # fast touches slow


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
            next(check.observed for check in d.explanation.checks if check.check_id == "recent_cross")
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


@pytest.mark.parametrize(
    ("tape", "options", "expected"),
    [
        pytest.param((below(), GOOD), {}, (None, ENTER), id="cross-bar-that-already-clears-the-gates-enters"),
        pytest.param(
            (below(), above("0.25", "75"), above("0.25", "65")),
            {},
            (None, None, ENTER),
            id="bar-after-the-cross-enters-once-rsi-is-back-inside-the-band",
        ),
        # The gap passes on the cross bar and RSI on the next, but no bar has both.
        pytest.param(
            (below(), above("0.25", "72"), above("0.18", "65")),
            {},
            (None, None, None),
            id="gap-and-rsi-must-pass-on-the-same-bar",
        ),
        pytest.param(
            (below(), above("0.05"), above("0.15"), GOOD, GOOD),
            {"cross_max_age_bars": 2},
            (None, None, None, ENTER, None),
            id="cross-age-limit-is-inclusive-and-configurable-at-the-limit",
        ),
        pytest.param(
            (below(), above("0.05"), above("0.15"), GOOD, GOOD),
            {"cross_max_age_bars": 3},
            (None, None, None, ENTER, None),
            id="cross-age-limit-is-inclusive-and-configurable-inside-the-limit",
        ),
        pytest.param(
            (below(), GOOD),
            {"cross_max_age_bars": 0},
            (None, ENTER),
            id="cross-age-limit-of-zero-allows-the-cross-bar-only-the-cross-bar",
        ),
        pytest.param(
            (below(), above("0.05"), GOOD),
            {"cross_max_age_bars": 0},
            (None, None, None),
            id="cross-age-limit-of-zero-allows-the-cross-bar-only-not-the-bar-after",
        ),
        pytest.param(
            (below(), above("0.20", "70")), {}, (None, ENTER), id="gap-floors-and-rsi-band-are-inclusive-gap-floor-and-rsi-upper"
        ),
        pytest.param((below(), above("0.20", "50")), {}, (None, ENTER), id="gap-floors-and-rsi-band-are-inclusive-rsi-lower"),
        pytest.param(
            (below(), above("0.20")),
            {"gap": 0.0, "gap_bps": 20.0},
            (None, ENTER),
            id="gap-floors-and-rsi-band-are-inclusive-normalized-gap-floor",
        ),
        # 20 bps of a 100 slow EMA is 0.20.
        pytest.param(
            (below(), above("0.10"), above("0.25")),
            {"gap": 0.0, "gap_bps": 20.0},
            (None, None, ENTER),
            id="normalized-gap-floor-applies-on-the-entry-bar",
        ),
        # With no gap floor, only the ended cross keeps the touching bar out.
        pytest.param(
            (below(), above("0.05", "75"), TOUCHING),
            {"gap": 0.0},
            (None, None, None),
            id="fast-touching-slow-ends-a-cross-that-was-still-recent",
        ),
        pytest.param(
            (below(None), above("0.25", None), above("0.25", None), GOOD),
            {},
            (None, None, None, None),
            id="cross-seen-while-rsi-warms-up-is-stale-if-rsi-takes-too-long",
        ),
    ],
)
def test_entry_rule_stages_the_expected_intents(
    tape: tuple[Reading, ...], options: dict[str, object], expected: tuple[SignalIntentKind | None, ...]
) -> None:
    assert _drive(list(tape), **options).staged == list(expected), "staged intents, bar by bar"


@pytest.mark.parametrize(
    ("tape", "options", "expected"),
    [
        # Only a limit longer than the hold leaves a cross recent once its trade is over.
        pytest.param(
            (below(), GOOD, GOOD, GOOD),
            {"hold_bars": 1, "cross_max_age_bars": 2},
            (None, ENTER, EXIT, ENTER),
            id="cross-still-recent-after-the-exit-enters-again",
        ),
        pytest.param(
            (below(), GOOD, below(), below(), below()),
            {"hold_bars": 3},
            (None, ENTER, None, None, EXIT),
            id="reversed-cross-during-the-hold-does-not-end-the-trade",
        ),
        pytest.param(
            (below(), GOOD, GOOD, GOOD, TOUCHING, GOOD),
            {"hold_bars": 1},
            (None, ENTER, EXIT, None, None, ENTER),
            id="fast-touching-slow-ends-the-cross-and-the-next-cross-enters",
        ),
        pytest.param(
            (below(), GOOD, below(), GOOD, GOOD, GOOD, GOOD),
            {"hold_bars": 4},
            (None, ENTER, None, None, None, EXIT, None),
            id="cross-during-a-hold-is-stale-by-the-time-the-trade-exits",
        ),
        # Nothing is remembered: the cross bar passed the gates while the trade was ending, and the bar after is judged on its own.
        pytest.param(
            (below(), GOOD, below(), below(), GOOD, GOOD),
            {"hold_bars": 3},
            (None, ENTER, None, None, EXIT, ENTER),
            id="cross-on-the-exit-bar-enters-on-the-next-bar",
        ),
        pytest.param(
            (below(), GOOD, GOOD, GOOD),
            {"discard": frozenset({1, 2})},
            (None, ENTER, ENTER, None),
            id="discarded-entry-is-proposed-again-while-the-cross-is-recent",
        ),
        pytest.param(
            (below(), GOOD, GOOD),
            {"observe_only": frozenset({1})},
            (None, ENTER, ENTER),
            id="observe-only-entry-bar-leaves-the-next-bar-free-to-enter",
        ),
        pytest.param(
            (below(), GOOD, GOOD, GOOD),
            {"hold_bars": 1, "discard": frozenset({2})},
            (None, ENTER, EXIT, EXIT),
            id="discarded-exit-is-proposed-again-on-the-next-bar",
        ),
        # A bot starts flat: the bar after the cross is judged on its own, whatever the replay did on the cross bar.
        pytest.param(
            (below(), GOOD, GOOD, GOOD),
            {"force_flat_before": frozenset({2})},
            (None, ENTER, ENTER, None),
            id="force-flat-drops-the-position-and-a-still-recent-cross-enters",
        ),
    ],
)
def test_position_lifecycle_stages_the_expected_intents(
    tape: tuple[Reading, ...], options: dict[str, object], expected: tuple[SignalIntentKind | None, ...]
) -> None:
    assert _drive(list(tape), **options).staged == list(expected), "staged intents, bar by bar"


@pytest.mark.parametrize(
    ("tape", "expected_staged", "expected_cross_states"),
    [
        pytest.param(
            (below(), above("0.05"), above("0.25")),
            (None, None, ENTER),
            ("not_above", "crossed_up", "recent"),
            id="bar-after-the-cross-enters-once-the-gap-has-widened",
        ),
        # The owner's rule: the cross bar closed at most 15 minutes before the evaluated bar.
        pytest.param(
            (below(), above("0.05"), above("0.15"), GOOD, GOOD),
            (None, None, None, None, None),
            ("not_above", "crossed_up", "recent", "stale", "stale"),
            id="cross-older-than-the-limit-does-not-enter",
        ),
        pytest.param(
            (GOOD, GOOD, GOOD),
            (None, None, None),
            ("unseen", "unseen", "unseen"),
            id="fast-already-above-slow-when-first-known-is-never-an-entry",
        ),
    ],
)
def test_entry_rule_reports_the_cross_state_it_staged_on(
    tape: tuple[Reading, ...],
    expected_staged: tuple[SignalIntentKind | None, ...],
    expected_cross_states: tuple[str, ...],
) -> None:
    run = _drive(list(tape))

    assert run.staged == list(expected_staged), "staged intents, bar by bar"
    assert run.cross_states() == list(expected_cross_states), "cross state read by each ready bar"


def test_position_is_held_for_exactly_hold_bars_and_a_stale_cross_gives_no_second_entry() -> None:
    run = _drive([below(), GOOD, GOOD, GOOD, GOOD, GOOD], hold_bars=2)

    assert run.staged == [None, ENTER, None, EXIT, None, None]
    assert run.cross_states()[-2:] == ["stale", "stale"]


def test_cross_seen_while_rsi_warms_up_enters_once_rsi_is_ready() -> None:
    run = _drive([below(None), above("0.25", None), GOOD])

    assert [d.ready for d in run.decisions] == [False, False, True]
    assert run.staged == [None, None, ENTER]


@pytest.mark.parametrize("limit", [1, 8], ids=["default", "longest"])
def test_cross_on_a_session_s_last_bar_is_stale_on_the_next_session_s_first_bar(limit: int) -> None:
    """The age is clock time: the next bar received closes hours after the cross, not 15 minutes."""
    first, second = _two_consecutive_sessions()
    bars = [*_session_bars(first)[-2:], _session_bars(second)[0]]

    run = _drive([below(), above("0.05"), GOOD], bars=bars, cross_max_age_bars=limit)

    assert run.staged == [None, None, None]
    assert run.cross_states() == ["not_above", "crossed_up", "stale"]


@pytest.mark.parametrize(("limit", "last"), [(1, None), (2, ENTER)], ids=["past-the-limit", "within-the-limit"])
def test_bar_missing_after_the_cross_ages_it_by_the_clock(limit: int, last: SignalIntentKind | None) -> None:
    """The bar after a missing one closes 30 minutes after the cross."""
    bars = [indexed_bucket("SPY", index, _WIDTH_MS, "100") for index in (0, 1, 3)]

    run = _drive([below(), above("0.05"), GOOD], bars=bars, cross_max_age_bars=limit)

    assert run.staged == [None, None, last]


def test_hold_counts_decision_bars_across_the_session_close() -> None:
    first, second = _two_consecutive_sessions()
    last_two = _session_bars(first)[-2:]
    next_day = _session_bars(second)[:5]

    run = _drive([below(), GOOD, GOOD, GOOD, GOOD, GOOD, GOOD], bars=[*last_two, *next_day], hold_bars=5)

    assert run.staged == [None, ENTER, None, None, None, None, EXIT]
    exit_intent = run.decisions[-1].intent
    assert exit_intent is not None
    assert exit_intent.bar_close_ms == next_day[4].end_ms


def _two_consecutive_sessions() -> tuple[date, date]:
    first, second = [
        window.session_date
        for window in trading_calendar.session_windows_ms_utc(date(2024, 1, 2), date(2024, 1, 12))
    ][:2]
    return first, second


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
    assert evaluation_id(cross_max_age_bars=1) != evaluation_id(cross_max_age_bars=2)


@pytest.mark.parametrize("value", [-1, 9, True, 1.5], ids=["negative", "past-two-hours", "boolean", "fractional"])
def test_cross_age_limit_must_be_a_whole_number_of_bars_up_to_two_hours(value: object) -> None:
    with pytest.raises(ValueError):
        EmaPreviousCrossoverParams(cross_max_age_bars=value)


# ── Tie-back to EMA Crossover Signal ─────────────────────────────────────────

_TIE_BACK_CELL = "SPY_W3mo_2026-02-02_to_2026-04-30"


def _entry_gates_pass(decision: SignalDecision) -> bool:
    explanation = decision.explanation
    assert explanation is not None
    entry = [check for check in explanation.checks if check.role is CheckRole.ENTRY]
    return decision.ready and bool(entry) and all(check.passed for check in entry)


@pytest.mark.parametrize(("gap", "rsi_min"), [(0.20, 50.0), (0.0, 30.0)], ids=["lean-parity-point", "no-gap-floor"])
def test_every_ema_crossover_signal_entry_bar_is_a_cross_bar_entry_here(gap: float, rsi_min: float) -> None:
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
        and any(c.check_id == "recent_cross" and c.observed == "crossed_up" for c in decision.explanation.checks)
    }

    assert reference_entry_bars, "the cell exercises the reference rule"
    assert candidate_cross_bar_entries == reference_entry_bars
