"""The EMA lengths and the hold as parameters (#2696).

The LEAN reference hardcodes EMA(5)/EMA(10) and a five-bar hold. Exposing
them must leave every identity at that point byte-identical (parameter
dumps, evaluation settings, the golden trace root), give every other point
an identity of its own, and keep the hold a count of decision bars -- never
of wall-clock time -- when it outlasts a session.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.programs.ema_crossover_signal import (
    EmaCrossoverSignalParams,
    build_ema_crossover_signal_program,
)
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationTrace,
    Settlement,
    SignalProgram,
    StageQuarantine,
    trace_root,
)
from app.lean_sidecar import trading_calendar
from app.services.spec_strategy_runner import InMemoryDataReader
from tests._helpers.signal_program import (
    RecordingExecutor,
    bind_strategy_context,
    bucket,
    indexed_bucket,
    mark_bar_arrival,
)

_REGISTRATION = _STRATEGY_REGISTRY["ema_crossover_signal"]
_WIDTH_MS = 15 * 60_000
_LENGTHS = ("fast_period", "slow_period", "hold_bars")
_GOLDEN = Path(__file__).resolve().parents[3] / "fixtures/golden"
_CELL = "SPY_W3mo_2026-02-02_to_2026-04-30"

# The default dump exactly as the schema produced it before #2696 added the
# lengths -- key order included, since some identities hash it unsorted.
_PRE_LENGTHS_DEFAULT_JSON = '{"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}'


@dataclass
class _ReadyIndicator:
    """A ready indicator pinned at one value, so a test controls the entry bar."""

    current_value: Decimal
    is_ready: bool = True

    def update(self, _timestamp_ms: int, _value: Decimal) -> None:
        """Hold the pinned value; the countdown under test never reads indicators."""


def _prepared(**params: object) -> tuple[SignalProgram, StrategyContext, RecordingExecutor]:
    program = build_ema_crossover_signal_program(EmaCrossoverSignalParams(**params))
    context, executor = bind_strategy_context(program.strategy)
    return program, context, executor


def _force_entry_on_next_bar(strategy: EmaCrossoverSignalAlgorithm) -> None:
    """Pin a fresh crossover that clears every gate; it stays above afterwards, so no re-entry."""
    strategy._ema5 = _ReadyIndicator(Decimal("500.50"))  # type: ignore[assignment]
    strategy._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[assignment]
    strategy._prev_ema5_above_ema10 = False


def _decide(program: SignalProgram, context: StrategyContext, bar: TradeBar) -> EvaluationTrace:
    """Drive one decision bar through the session and commit whatever it staged."""
    mark_bar_arrival(context, bar)
    stage = program.session.advance(bar, mode=EvaluationMode.DECIDE)
    assert not isinstance(stage, StageQuarantine), stage
    return program.session.settle(Settlement.COMMIT).trace


def _session_bars(day: date) -> list[TradeBar]:
    """Every decision bar of one scheduled session, bounds from the canonical calendar."""
    open_ms = trading_calendar.session_open_ms_utc(day)
    close_ms = trading_calendar.session_close_ms_utc(day)
    return [bucket("SPY", start, start + _WIDTH_MS, "500") for start in range(open_ms, close_ms, _WIDTH_MS)]


def _first_session(*, early_close: bool) -> date:
    for window in trading_calendar.session_windows_ms_utc(date(2024, 1, 2), date(2024, 12, 31)):
        if trading_calendar.is_early_close(window.session_date) is early_close:
            return window.session_date
    raise AssertionError(f"no 2024 session with early_close={early_close}")


def _cell_minute_bars(cell: str, symbol: str) -> list[TradeBar]:
    bars: list[TradeBar] = []
    with (_GOLDEN / "cross-engine-studies/cells" / cell / "lean/observations.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                TradeBar(
                    symbol=symbol,
                    start_ms=end_ms - 60_000,
                    end_ms=end_ms,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(Decimal(row["volume"])),
                )
            )
    return bars


# ── Parameters ────────────────────────────────────────────────────────────


def test_params_dump_at_the_default_point_is_byte_identical_to_the_pre_lengths_schema() -> None:
    params = EmaCrossoverSignalParams()

    assert json.dumps(params.model_dump(mode="json")) == _PRE_LENGTHS_DEFAULT_JSON
    assert params.model_dump() == {"gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0, "symbol": "SPY"}


def test_params_explicit_defaults_dump_to_the_same_canonical_form() -> None:
    explicit = EmaCrossoverSignalParams(fast_period=5, slow_period=10, hold_bars=5)

    assert json.dumps(explicit.model_dump(mode="json")) == _PRE_LENGTHS_DEFAULT_JSON
    assert explicit == EmaCrossoverSignalParams()


@pytest.mark.parametrize(
    ("overrides", "present"),
    [
        ({"fast_period": 3}, {"fast_period": 3}),
        ({"slow_period": 40}, {"slow_period": 40}),
        ({"hold_bars": 1}, {"hold_bars": 1}),
        ({"fast_period": 5, "slow_period": 12}, {"slow_period": 12}),
        (
            {"fast_period": 30, "slow_period": 40, "hold_bars": 26},
            {"fast_period": 30, "slow_period": 40, "hold_bars": 26},
        ),
    ],
)
def test_params_dump_carries_exactly_the_non_default_lengths_and_round_trips(
    overrides: dict[str, int], present: dict[str, int]
) -> None:
    params = EmaCrossoverSignalParams(**overrides)

    dumped = params.model_dump(mode="json")

    assert {name: dumped[name] for name in _LENGTHS if name in dumped} == present
    assert params.model_dump(mode="json", exclude={"symbol"}) == {k: v for k, v in dumped.items() if k != "symbol"}
    assert EmaCrossoverSignalParams.model_validate(dumped) == params
    assert EmaCrossoverSignalParams.model_validate_json(params.model_dump_json()) == params


def test_params_schema_declares_every_length_with_its_default_and_bounds() -> None:
    properties = EmaCrossoverSignalParams.model_json_schema()["properties"]

    declared = {
        name: (properties[name]["type"], properties[name]["default"], properties[name]["minimum"], properties[name]["maximum"])
        for name in _LENGTHS
    }

    assert declared == {
        "fast_period": ("integer", 5, 2, 30),
        "slow_period": ("integer", 10, 3, 40),
        "hold_bars": ("integer", 5, 1, 26),
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"fast_period": 10, "slow_period": 10},
        {"fast_period": 12, "slow_period": 10},
        {"fast_period": 1},
        {"fast_period": 31, "slow_period": 40},
        {"slow_period": 41},
        {"hold_bars": 0},
        {"hold_bars": 27},
        {"fast_period": 5.5},
        {"hold_bars": True},
        {"slow_period": "ten"},
    ],
)
def test_params_refuse_an_illegal_length_or_hold(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EmaCrossoverSignalParams(**overrides)


def test_params_name_inverted_lengths() -> None:
    with pytest.raises(ValidationError, match="fast_period must be less than slow_period"):
        EmaCrossoverSignalParams(fast_period=20, slow_period=8)


# ── Algorithm identity ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"fast_period": 5.0}, TypeError),
        ({"hold_bars": True}, TypeError),
        ({"slow_period": "10"}, TypeError),
        ({"hold_bars": 0}, ValueError),
        ({"fast_period": 10, "slow_period": 10}, ValueError),
        ({"fast_period": 11, "slow_period": 10}, ValueError),
    ],
)
def test_algorithm_refuses_a_non_integer_or_inverted_length(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        EmaCrossoverSignalAlgorithm(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(("name", "value"), [("fast_period", 4), ("slow_period", 11), ("hold_bars", 6)])
def test_each_non_default_length_joins_evaluation_identity(name: str, value: int) -> None:
    default = _REGISTRATION.build(EmaCrossoverSignalParams()).signal_program_settings()

    variant = _REGISTRATION.build(EmaCrossoverSignalParams(**{name: value})).signal_program_settings()

    assert variant == {**default, name: str(value)}


@pytest.mark.parametrize(
    ("params", "names", "source_model", "insight_minutes"),
    [
        ({}, ("EMA5", "EMA10"), "EmaCross_5_10_RSI14", 75),
        ({"fast_period": 3, "slow_period": 20, "hold_bars": 8}, ("EMA3", "EMA20"), "EmaCross_3_20_RSI14", 120),
    ],
)
def test_configured_lengths_name_the_indicators_and_the_insight(
    params: dict[str, int], names: tuple[str, str], source_model: str, insight_minutes: int
) -> None:
    program, context, _executor = _prepared(**params)
    strategy = program.strategy
    assert (strategy._ema5.name, strategy._ema10.name) == names
    _force_entry_on_next_bar(strategy)

    trace = _decide(program, context, indexed_bucket("SPY", 0, _WIDTH_MS, "500"))

    assert trace.staged_candidate == "ENTER"
    [insight] = context.insight_manager.all_insights
    assert insight.source_model == source_model
    assert insight.period == timedelta(minutes=insight_minutes)


# ── Hold semantics: decision bars, never wall-clock time ─────────────────


@pytest.mark.parametrize("early_close", [False, True], ids=["ordinary", "early-close"])
def test_default_hold_entered_two_bars_before_the_close_exits_on_the_third_bar_of_the_next_session(
    early_close: bool,
) -> None:
    """Five decision bars: two left in the entry session, three into the next.

    Seventy-five wall-clock minutes after this entry the market is closed, so
    a wall-clock hold would exit on the next session's first bar instead.
    """
    day = _first_session(early_close=early_close)
    following = trading_calendar.next_trading_day(day)
    run = _session_bars(day)[-3:] + _session_bars(following)
    program, context, executor = _prepared()
    _force_entry_on_next_bar(program.strategy)

    for bar in run:
        _decide(program, context, bar)

    assert [intent.kind for intent in executor.intents] == [SignalIntentKind.ENTER, SignalIntentKind.EXIT]
    assert executor.intents[0].bar_close_ms == trading_calendar.session_close_ms_utc(day) - 2 * _WIDTH_MS
    assert executor.intents[1].bar_close_ms == trading_calendar.session_open_ms_utc(following) + 3 * _WIDTH_MS


@pytest.mark.parametrize("hold_bars", [1, 2, 3, 8, 26])
@pytest.mark.parametrize("early_close", [False, True], ids=["ordinary", "early-close"])
def test_configured_hold_exits_on_its_own_decision_bar_count_across_the_close(early_close: bool, hold_bars: int) -> None:
    day = _first_session(early_close=early_close)
    following = trading_calendar.next_trading_day(day)
    run = _session_bars(day)[-3:] + _session_bars(following) + _session_bars(trading_calendar.next_trading_day(following))
    program, context, executor = _prepared(hold_bars=hold_bars)
    _force_entry_on_next_bar(program.strategy)

    candidates = [_decide(program, context, bar).staged_candidate for bar in run]

    assert candidates[0] == "ENTER"
    assert candidates[1:hold_bars] == [None] * (hold_bars - 1)
    assert candidates[hold_bars] == "EXIT"
    assert [intent.kind for intent in executor.intents] == [SignalIntentKind.ENTER, SignalIntentKind.EXIT]
    assert executor.intents[1].bar_close_ms == run[hold_bars].end_ms


# ── Real bars: other lengths are another strategy ────────────────────────


def _replay(params: EmaCrossoverSignalParams, minute_bars: list[TradeBar]) -> list[EvaluationTrace]:
    strategy = _REGISTRATION.build(params)
    BacktestEngine.for_decision_identity(InMemoryDataReader(minute_bars)).run(strategy)
    assert strategy.signal_program is not None
    return strategy.signal_program.session.traces


def test_non_default_lengths_decide_differently_on_the_same_bars_and_hold_their_own_count() -> None:
    corpus = json.loads((_GOLDEN / "ema-signal-session/v1/trace-corpus.json").read_text(encoding="utf-8"))
    pinned = next(entry for entry in corpus["entries"] if entry["cell"] == _CELL)
    minute_bars = _cell_minute_bars(_CELL, "SPY")
    variant_params = EmaCrossoverSignalParams(symbol="SPY", fast_period=3, slow_period=20, hold_bars=8)

    default = _replay(EmaCrossoverSignalParams(symbol="SPY"), minute_bars)
    variant = _replay(variant_params, minute_bars)

    # The default point is still the golden one, bar for bar.
    assert trace_root(default) == pinned["trace_root"]
    assert len(default) == pinned["trace_count"]
    default_entries = [trace.bar_close_ms for trace in default if trace.staged_candidate == "ENTER"]
    variant_entries = [trace.bar_close_ms for trace in variant if trace.staged_candidate == "ENTER"]
    assert default_entries and variant_entries
    assert variant_entries != default_entries
    assert trace_root(variant) != pinned["trace_root"]
    # Every exit lands exactly its hold of decision bars after its entry,
    # across overnight gaps included -- real sessions, not synthetic ones.
    for traces, hold in ((default, 5), (variant, variant_params.hold_bars)):
        for index, trace in enumerate(traces):
            if trace.staged_candidate == "ENTER" and index + hold < len(traces):
                assert [t.staged_candidate for t in traces[index + 1 : index + hold + 1]] == [None] * (hold - 1) + ["EXIT"]


# ── Warmup: a crossover is fresh only against a known relation ───────────


@pytest.mark.parametrize("slow_period", [10, 14, 15, 20, 40])
def test_a_rising_path_never_enters_because_nothing_ever_crosses(slow_period: int) -> None:
    """With slow >= 15 both EMAs turn ready on the same bar as RSI(14).

    The relation on the bar before is unknown, not "below", so that first
    ready bar is no crossover; comparing it to the unprimed state would enter
    on a path where the fast line was never below the slow one.
    """
    program, context, executor = _prepared(slow_period=slow_period, gap=0.0, rsi_min=0.0, rsi_max=100.0)

    traces = [_decide(program, context, indexed_bucket("SPY", i, _WIDTH_MS, str(100 + i))) for i in range(slow_period + 10)]

    first_ready = next(index for index, trace in enumerate(traces) if trace.ready)
    assert first_ready == max(slow_period, 15) - 1
    assert traces[first_ready].relation_facts["ema_fast_above_slow"] is True
    assert executor.intents == []


def test_the_first_real_crossover_after_a_late_ready_bar_still_enters() -> None:
    program, context, executor = _prepared(slow_period=20, gap=0.0, rsi_min=0.0, rsi_max=100.0)
    closes = [200 - i for i in range(30)] + [170 + 2 * i for i in range(1, 30)]

    traces = [_decide(program, context, indexed_bucket("SPY", i, _WIDTH_MS, str(close))) for i, close in enumerate(closes)]

    entries = [index for index, trace in enumerate(traces) if trace.staged_candidate == "ENTER"]
    assert len(entries) == 1
    entry = traces[entries[0]]
    before = traces[entries[0] - 1]
    assert before.ready
    assert Decimal(entry.reason_evidence["ema_fast"]) > Decimal(entry.reason_evidence["ema_slow"])
    assert Decimal(before.reason_evidence["ema_fast"]) <= Decimal(before.reason_evidence["ema_slow"])
    assert [intent.kind for intent in executor.intents] == [SignalIntentKind.ENTER, SignalIntentKind.EXIT]


# ── Registry contract ─────────────────────────────────────────────────────


def _samples_until_ready(indicator: object) -> int:
    for sample in range(1, 1_000):
        indicator.update(sample * _WIDTH_MS, Decimal(100 + sample % 7))  # type: ignore[attr-defined]
        if indicator.is_ready:  # type: ignore[attr-defined]
            return sample
    raise AssertionError("indicator never became ready")


@pytest.mark.parametrize("overrides", [{}, {"fast_period": 3, "slow_period": 40, "hold_bars": 12}])
def test_contract_series_and_hold_describe_the_program_these_parameters_build(overrides: dict[str, int]) -> None:
    contract = _REGISTRATION.signal_program_contract
    assert contract is not None
    params = EmaCrossoverSignalParams(**overrides)
    program, _context, _executor = _prepared(**overrides)
    strategy = program.strategy
    built = {"ema_fast": strategy._ema5, "ema_slow": strategy._ema10, "rsi": strategy._rsi14}

    series = {entry.name: entry for entry in contract.resolved_signals(params)}

    assert set(series) == set(built)
    for name, indicator in built.items():
        assert series[name].period == indicator.period
        assert series[name].warmup_bars == _samples_until_ready(indicator)
    assert contract.resolved_exit_eligibility(params).countdown_decision_clocks == params.hold_bars
