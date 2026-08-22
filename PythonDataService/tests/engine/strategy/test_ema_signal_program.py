"""Contract tests for the registry-backed staged EMA Signal Program."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent
from app.engine.strategy.signal_program import (
    EmaCrossoverSignalProgram,
    EvaluationMode,
    EvaluationTrace,
    Settlement,
    StageQuarantine,
    StageStatus,
    trace_corpus_root,
    trace_root,
)
from app.services.spec_strategy_runner import InMemoryDataReader


@dataclass
class _ReadyIndicator:
    current_value: Decimal
    is_ready: bool = True

    def update(self, _timestamp_ms: int, _value: Decimal) -> None:
        """Keep the one-cycle program fixture independent of indicator math."""


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


def _prepared_program() -> tuple[EmaCrossoverSignalProgram, EmaCrossoverSignalAlgorithm, StrategyContext]:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_factory is not None
    program = registration.signal_program_factory(registration.param_schema())
    strategy = program.strategy
    program.activate_for_backtest()
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    context.set_signal_intent_executor(_RecordingExecutor())
    strategy._ema5 = _ReadyIndicator(Decimal("500.50"))
    strategy._ema10 = _ReadyIndicator(Decimal("500.00"))
    strategy._rsi14 = _ReadyIndicator(Decimal("60"))
    strategy._prev_ema5_above_ema10 = False
    return program, strategy, context


def test_registry_factory_is_the_single_public_ema_program_construction_seam() -> None:
    program, strategy, _context = _prepared_program()

    assert strategy.signal_program is program
    assert _STRATEGY_REGISTRY["ema_crossover_signal"].build(
        _STRATEGY_REGISTRY["ema_crossover_signal"].param_schema()
    ).signal_program is not None


def test_backtest_commits_each_registered_program_stage_at_its_existing_order_seam() -> None:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    strategy = registration.build(registration.param_schema())
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    minute_bars = [
        TradeBar(
            symbol="SPY",
            time=start + timedelta(minutes=index),
            end_time=start + timedelta(minutes=index + 1),
            open=Decimal("500"),
            high=Decimal("500"),
            low=Decimal("500"),
            close=Decimal("500"),
            volume=100,
        )
        for index in range(45)
    ]

    BacktestEngine(InMemoryDataReader(minute_bars)).run(strategy)

    assert strategy.signal_program.session.active_stage is None
    assert len(strategy.signal_program.session.traces) == 3


def test_session_quarantines_a_second_advance_until_the_first_stage_settles() -> None:
    program, _strategy, context = _prepared_program()
    executor = context._signal_intent_executor
    assert isinstance(executor, _RecordingExecutor)

    stage = program.session.advance(_bar(), mode=EvaluationMode.DECIDE)

    assert stage.status is StageStatus.STAGED
    assert stage.trace.staged_candidate == "ENTER"
    assert stage.trace.action_plan_request == {"contract": "single_long_stock", "intent": "ENTER"}
    assert executor.intents == []
    assert isinstance(program.session.advance(_bar(1), mode=EvaluationMode.DECIDE), StageQuarantine)
    assert program.session.advance(_bar(1), mode=EvaluationMode.DECIDE).status is StageStatus.UNSETTLED_STAGE

    program.session.settle(Settlement.COMMIT)

    assert [intent.kind.value for intent in executor.intents] == ["ENTER"]
    assert program.session.active_stage is None


def test_discarded_countdown_exit_reemits_on_the_next_eligible_clock() -> None:
    program, strategy, context = _prepared_program()
    strategy._in_position = True
    strategy._bars_until_exit = 1
    executor = context._signal_intent_executor
    assert isinstance(executor, _RecordingExecutor)

    first = program.session.advance(_bar(), mode=EvaluationMode.DECIDE)
    assert first.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.DISCARD)

    assert executor.intents == []
    assert strategy._in_position is True
    assert strategy._bars_until_exit == 1

    second = program.session.advance(_bar(1), mode=EvaluationMode.DECIDE)
    assert second.trace.staged_candidate == "EXIT"
    program.session.settle(Settlement.COMMIT)

    assert [intent.kind.value for intent in executor.intents] == ["EXIT"]


def test_observe_only_candidate_is_discarded_before_continue_can_change_mode() -> None:
    program, strategy, context = _prepared_program()
    executor = context._signal_intent_executor
    assert isinstance(executor, _RecordingExecutor)

    observed = program.session.advance(_bar(), mode=EvaluationMode.OBSERVE_ONLY)

    assert observed.trace.evaluation_mode is EvaluationMode.OBSERVE_ONLY
    assert observed.trace.staged_candidate == "ENTER"
    assert observed.settlement is Settlement.DISCARD
    assert program.session.active_stage is None
    assert executor.intents == []
    assert strategy._in_position is False


def test_trace_root_changes_only_when_trace_semantics_change() -> None:
    trace = EvaluationTrace(
        program_key="ema_crossover_signal",
        program_version="ema-crossover-signal/v1",
        evaluation_id="evaluation",
        bar_close_ms=1,
        bar_qualified=True,
        bucket_closed=True,
        ready=True,
        relation_facts={"ema_fast_above_slow": True, "was_in_position": False},
        signal_facts={"decision": "ENTER", "timeframe": "15m"},
        staged_candidate="ENTER",
        reason_evidence={"prior_countdown": 0, "ema_fast": "2", "ema_slow": "1", "rsi": "60"},
        action_plan_request={"contract": "single_long_stock", "intent": "ENTER"},
    )

    assert trace_root([trace]) == trace_root([replace(trace)])
    assert trace_root([trace]) != trace_root([replace(trace, staged_candidate="EXIT")])


def test_validated_ema_settings_corpus_has_a_pinned_trace_root() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/golden/ema-signal-session/v1/trace-corpus.json"
    corpus = json.loads(fixture.read_text(encoding="utf-8"))

    assert trace_corpus_root(corpus["entries"]) == corpus["trace_root"]
    assert len(corpus["entries"]) == 10
    cells_root = fixture.parents[2] / "cross-engine-studies/cells"
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
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
        BacktestEngine(InMemoryDataReader(minute_bars)).run(strategy)
        assert strategy.signal_program is not None
        assert len(strategy.signal_program.session.traces) == entry["trace_count"]
        assert trace_root(strategy.signal_program.session.traces) == entry["trace_root"]
