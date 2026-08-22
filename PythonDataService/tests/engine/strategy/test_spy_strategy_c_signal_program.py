"""Contract tests for the registry-backed staged Strategy C Signal Program.

Golden-trace qualification test mirroring
``tests/engine/strategy/test_sma_signal_program.py::
test_validated_sma_settings_corpus_has_a_pinned_trace_root`` — see that
test's docstring for why this is the runtime admission gate (PRD S11.4):
a program edit that changes behavior without also updating the registry's
sealed ``golden_trace_root`` must fail here, not slip through as a
"qualified" receipt.

Also proves the single most important correctness question for this
promotion (issue #1730 Slice 5): Strategy C's entry gate compares ADX
bar-over-bar (``ADX[i] > ADX[i-1]``), and that "previous bar" state lives
inside the ``AverageDirectionalIndex`` indicator object itself, not a
strategy-level flag. ``RsiRangeStrategy.evaluate_signal_bar`` advances that
indicator unconditionally, once per bar, regardless of whether the resulting
candidate is later COMMITted or DISCARDed — see
``test_discarded_entry_does_not_corrupt_the_next_bar_adx_rising_comparison``.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.algorithms.spy_strategy_c import SpyStrategyCAlgorithm
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent
from app.engine.strategy.signal_program import (
    EvaluationMode,
    EvaluationStage,
    Settlement,
    SignalProgram,
    trace_corpus_root,
    trace_root,
)
from app.services.spec_strategy_runner import InMemoryDataReader


@dataclass
class _SteppingAdx:
    """Minimal ``BarIndicator`` double: current/previous mirror ``.update()`` exactly.

    Real ADX arithmetic is proven elsewhere (``app/engine/tests/test_adx.py``,
    the golden fixture at ``app/engine/tests/fixtures/golden/adx_14/``); this
    double isolates the custody-split *plumbing* question — whether a
    DISCARDed candidate corrupts the bar-over-bar ``previous_value`` the next
    decision clock reads — from ADX's own Wilder-smoothing math. ``_index``
    starts at 0 (one value already "seen"), modeling a strategy already past
    warmup with a first ``current_value`` on hand, the same state a real,
    live ``AverageDirectionalIndex`` would be in by the time a bot is
    evaluating real decision clocks.
    """

    values: list[Decimal]
    is_ready: bool = True
    _index: int = 0

    @property
    def current_value(self) -> Decimal | None:
        return self.values[self._index] if self._index >= 0 else None

    @property
    def previous_value(self) -> Decimal | None:
        return self.values[self._index - 1] if self._index >= 1 else None

    def update(self, _bar: TradeBar) -> bool:
        self._index += 1
        return True


@dataclass
class _ReadyRsi:
    """Always-ready RSI double pinned inside the entry range gate."""

    current_value: Decimal
    is_ready: bool = True

    def update(self, _timestamp_ms: int, _value: Decimal) -> bool:
        return True


@dataclass
class _RecordingExecutor:
    intents: list[SignalIntent] = field(default_factory=list)

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


def _bar(offset: int = 0) -> TradeBar:
    start = datetime(2026, 7, 17, 14, 45, tzinfo=UTC) + timedelta(minutes=15 * offset)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=15),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


def _prepared_program() -> tuple[SignalProgram, SpyStrategyCAlgorithm, StrategyContext]:
    registration = _STRATEGY_REGISTRY["spy_strategy_c"]
    assert registration.signal_program_factory is not None
    program = registration.signal_program_factory(registration.param_schema())
    strategy = program.strategy
    assert isinstance(strategy, SpyStrategyCAlgorithm)
    program.activate_for_backtest()
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    context.set_signal_intent_executor(_RecordingExecutor())
    # RSI parked in-range for the whole test; only ADX's bar-over-bar
    # relation is under test here.
    strategy._rsi = _ReadyRsi(current_value=Decimal("50"))
    strategy._adx = _SteppingAdx(values=[Decimal("18"), Decimal("25"), Decimal("30")])
    return program, strategy, context


def test_discarded_entry_does_not_corrupt_the_next_bar_adx_rising_comparison() -> None:
    """A DISCARDed ENTER must neither (a) leave the strategy believing it
    holds a position it was never granted, nor (b) roll back the ADX
    indicator's own previous-bar tracking, which would make the next
    decision clock re-compare against a stale pair instead of advancing.
    """
    program, strategy, context = _prepared_program()
    executor = context._signal_intent_executor
    assert isinstance(executor, _RecordingExecutor)

    first = program.session.advance(_bar(), mode=EvaluationMode.DECIDE)
    assert isinstance(first, EvaluationStage)
    assert first.trace.staged_candidate == "ENTER"
    assert first.trace.reason_evidence["adx"] == "25"
    assert first.trace.reason_evidence["adx_prev"] == "18"
    program.session.settle(Settlement.DISCARD)

    assert executor.intents == []
    assert strategy._in_position is False

    second = program.session.advance(_bar(1), mode=EvaluationMode.DECIDE)
    assert isinstance(second, EvaluationStage)
    # If the discard had rolled the indicator back, this bar would still
    # see (adx=25, adx_prev=18) -- the exact pair the discarded bar already
    # evaluated -- instead of advancing to (30, 25).
    assert second.trace.reason_evidence["adx"] == "30"
    assert second.trace.reason_evidence["adx_prev"] == "25"
    assert second.trace.staged_candidate == "ENTER"
    program.session.settle(Settlement.COMMIT)

    assert [intent.kind.value for intent in executor.intents] == ["ENTER"]
    assert strategy._in_position is True


def test_discarded_exit_does_not_corrupt_in_position_custody() -> None:
    """Symmetric check on the exit side: a DISCARDed EXIT must leave
    ``_in_position`` exactly as it was (still True), the same restore
    contract ``sma_crossover``/``ema_crossover_signal`` already prove for
    their own exit rules.
    """
    program, strategy, context = _prepared_program()
    executor = context._signal_intent_executor
    assert isinstance(executor, _RecordingExecutor)
    strategy._in_position = True
    # ADX below the default exit threshold (15) triggers EXIT.
    strategy._adx = _SteppingAdx(values=[Decimal("20"), Decimal("10")], _index=0)

    stage = program.session.advance(_bar(), mode=EvaluationMode.DECIDE)
    assert isinstance(stage, EvaluationStage)
    assert stage.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.DISCARD)

    assert executor.intents == []
    assert strategy._in_position is True


def test_validated_spy_strategy_c_settings_corpus_has_a_pinned_trace_root() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/golden/spy-strategy-c-signal/v1/trace-corpus.json"
    corpus = json.loads(fixture.read_text(encoding="utf-8"))

    assert trace_corpus_root(corpus["entries"]) == corpus["trace_root"]
    assert len(corpus["entries"]) == 10
    cells_root = fixture.parents[2] / "cross-engine-studies/cells"
    registration = _STRATEGY_REGISTRY["spy_strategy_c"]
    contract = registration.signal_program_contract
    assert contract is not None
    # This is the runtime admission gate (PRD S11.4): a program edit that
    # changes behavior without also updating the registry's sealed
    # golden_trace_root must fail here, not slip through as a "qualified"
    # receipt. `scripts/run_signal_program_build_qualification.py` binds its
    # emitted receipt to `contract.golden_trace_root`, so this suite passing
    # is what makes that receipt admissible evidence.
    assert corpus["trace_root"] == contract.golden_trace_root

    for entry in corpus["entries"]:
        cell = cells_root / entry["cell"]
        minute_bars: list[TradeBar] = []
        with (cell / "lean" / "observations.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                end_ms = int(row["ms_utc"])
                minute_bars.append(
                    TradeBar(
                        symbol=entry["settings"]["symbol"],
                        start_ms=end_ms - 60_000,
                        end_ms=end_ms,
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=int(Decimal(row["volume"])),
                    )
                )
        strategy = registration.build(registration.param_schema(**entry["settings"]))
        assert isinstance(strategy, SpyStrategyCAlgorithm)
        BacktestEngine(InMemoryDataReader(minute_bars)).run(strategy)
        assert strategy.signal_program is not None
        assert len(strategy.signal_program.session.traces) == entry["trace_count"]
        assert trace_root(strategy.signal_program.session.traces) == entry["trace_root"]
