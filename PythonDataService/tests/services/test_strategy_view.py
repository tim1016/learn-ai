"""The strategy view's renderer, default gates and read assembly (#2639)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.decision_receipts import QUARANTINE_OUTCOME, DecisionReceipt
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import SignalDecision
from app.schemas.decision_explanation import DecisionExplanationRecord
from app.services.broker_v2_panel.strategy_view_source import BEFORE_START_TEXT, build_strategy_view
from app.services.strategy_view import ResolvedStrategyView, StrategyViewUnavailableError
from scripts.fixture_generators.ema_decision_explanation_2026_09_29 import replay

EXP_001 = Path(__file__).resolve().parents[1] / "fixtures/golden/strategy-explanation/EXP-001/v1/output.json"
PROGRAM_KEYS = sorted(key for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None)


def _ema_view(**params: float) -> ResolvedStrategyView:
    return ResolvedStrategyView.for_settings("ema_crossover_signal", params or None, symbol="SPY")


def _exp001_records() -> list[DecisionExplanationRecord]:
    explanations = json.loads(EXP_001.read_text(encoding="utf-8"))["explanations"]
    return [DecisionExplanationRecord.model_validate(raw) for raw in explanations]


def test_the_2026_09_29_bar_is_worded_for_the_owner() -> None:
    at_1430, _ = _exp001_records()

    rendered = _ema_view().render(at_1430)

    assert [(c.label, c.observed_text, c.needs, c.passed) for c in rendered.checks] == [
        ("Fresh cross", "yes", "a cross up", True),
        ("Gap", "+0.03", "≥ 0.20", False),
        ("RSI", "47.3", "in 50–70", False),
    ]
    assert [c.chip for c in rendered.checks] == ["cross", "gap +0.03", "RSI 47.3"]
    assert all(c.applies for c in rendered.checks)
    assert [(v.label, v.text) for v in rendered.values] == [
        ("EMA 5", "763.46"),
        ("EMA 10", "763.44"),
        ("RSI 14", "47.3"),
    ]


def test_the_default_gate_reads_the_strategys_own_check() -> None:
    at_1430, at_1445 = _exp001_records()
    view = _ema_view()

    assert view.gate_results(at_1430) == {"rsi_band": False}
    assert view.gate_results(at_1445) == {"rsi_band": True}


def test_the_default_gate_follows_the_deployed_settings() -> None:
    declaration = _ema_view(rsi_min=55, rsi_max=65, gap=0.3).declaration()

    gate = declaration.gates[0]
    assert (gate.gate_id, gate.label, gate.source) == ("rsi_band", "RSI in 55–65", "strategy")
    rsi = next(value for value in declaration.values if value.key == "rsi")
    assert rsi.band == [55.0, 65.0]
    assert rsi.pane == "rsi"
    assert [value.label for value in declaration.values] == ["EMA 5", "EMA 10", "RSI 14"]


class _ReadyIndicator:
    def __init__(self, value: Decimal) -> None:
        self.current_value = value
        self.is_ready = True

    def update(self, _time: object, _value: object) -> None:
        return None


def _ema_decision_at_rsi(rsi: str) -> tuple[TradeBar, SignalDecision]:
    strategy = EmaCrossoverSignalAlgorithm(symbol="SPY")
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.initialize()
    strategy._ema10 = _ReadyIndicator(Decimal("100.00"))  # type: ignore[assignment]
    strategy._ema5 = _ReadyIndicator(Decimal("100.30"))  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(Decimal(rsi))  # type: ignore[assignment]
    start = datetime(2026, 9, 29, 18, 15, tzinfo=UTC)
    bar = TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=15),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=10,
    )
    return bar, strategy.evaluate_signal_bar(bar)


@pytest.mark.parametrize(("rsi", "bright"), [("50", True), ("70", True), ("49.99", False), ("70.01", False)])
def test_a_bar_exactly_on_the_rsi_edge_is_shaded_the_way_the_bot_judged_it(rsi: str, bright: bool) -> None:
    bar, decision = _ema_decision_at_rsi(rsi)
    record = DecisionExplanationRecord.from_decision(bar, decision)
    assert record is not None

    assert _ema_view().gate_results(record) == {"rsi_band": bright}


@pytest.mark.parametrize("program_key", PROGRAM_KEYS)
def test_every_strategy_words_every_decision_it_makes(program_key: str) -> None:
    """Each declared template formats against real recorded decisions."""
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    assert contract is not None
    view = ResolvedStrategyView.for_settings(program_key, contract.validated_settings, symbol="SPY")
    declaration = view.declaration()
    assert declaration.default_gate_id == registration.strategy_view.default_gate.gate_id  # type: ignore[union-attr]

    minutes = json.loads((EXP_001.parent / "input.json").read_text(encoding="utf-8"))["minutes"]
    staged = replay(minutes, program_key=program_key, settings=contract.validated_settings)
    assert staged
    gate_seen: set[bool | None] = set()
    for bar, decision in staged:
        record = DecisionExplanationRecord.from_decision(bar, decision)
        assert record is not None
        rendered = view.render(record)
        assert all(check.label and check.chip and check.observed_text for check in rendered.checks)
        gate_seen.update(view.gate_results(record).values())
    assert {True, False} <= gate_seen


def test_a_held_bar_marks_its_entry_rules_as_not_applying() -> None:
    _, at_1445 = _exp001_records()
    held = at_1445.model_copy(update={"holding": True})

    rendered = _ema_view().render(held)

    assert not any(check.applies for check in rendered.checks)


def test_an_unregistered_strategy_has_no_view() -> None:
    with pytest.raises(StrategyViewUnavailableError):
        ResolvedStrategyView.for_settings("retired_strategy", None, symbol="SPY")


def _receipt(
    seq: int, record: DecisionExplanationRecord | None, *, run_id: str = "run-1", outcome: str = "no_action"
) -> DecisionReceipt:
    return DecisionReceipt(
        seq=seq,
        ts_ms=1_790_707_510_000,
        bar_ref=f"decision-bar:ibkr:SPY:{seq}",
        outcome=outcome,  # type: ignore[arg-type]
        reason_code="NO_ACTION",
        run_id=run_id,
        decision_bar_close_ms=None if record is None else record.bar.end_ms,
        explanation=record,
    )


def test_the_view_shows_warmup_behind_the_start_and_the_runs_decisions_after_it() -> None:
    at_1430, at_1445 = _exp001_records()

    response = build_strategy_view(
        _ema_view(),
        symbol="SPY",
        run_id="run-1",
        run_ids=frozenset({"run-1", "sid:run-1"}),
        run_started_at_ms=1_790_706_720_000,
        run_stopped_at_ms=None,
        # A re-entered run's warmup also evaluated the bar it decided live.
        before_start=[at_1430, at_1445],
        receipts=[
            _receipt(1, None),  # written before decisions recorded values
            _receipt(2, at_1445, run_id="sid:run-1"),
            _receipt(3, at_1430, run_id="older-run"),
            _receipt(4, None, outcome=QUARANTINE_OUTCOME),
        ],
    )

    assert [(c.bar_close_ms, c.phase) for c in response.candles] == [
        (at_1430.bar.end_ms, "before_start"),
        (at_1445.bar.end_ms, "decision"),
    ]
    before, decided = response.candles
    assert before.phase_text == BEFORE_START_TEXT and before.decision_seq is None
    assert (decided.outcome, decided.decision_seq) == ("no_action", 2)
    assert (before.gates, decided.gates) == ({"rsi_band": False}, {"rsi_band": True})
    assert response.unexplained_decision_count == 1
    assert response.notices == [
        "1 decision in this run was recorded before decisions saved their values: values not recorded."
    ]
    assert response.declaration.default_gate_id == "rsi_band"
    assert response.decision_timeframe_ms == 15 * 60_000


def test_a_missing_ledger_says_warmup_bars_are_unavailable() -> None:
    response = build_strategy_view(
        _ema_view(),
        symbol="SPY",
        run_id="run-1",
        run_ids=frozenset({"run-1"}),
        run_started_at_ms=None,
        run_stopped_at_ms=None,
        before_start=None,
        receipts=[],
    )

    assert response.candles == []
    assert response.notices == ["Bars from before the bot started are unavailable for this run."]


def test_a_bar_the_run_decided_is_never_drawn_as_before_start() -> None:
    """A re-entered run's warmup re-evaluates bars it already decided, some before values were saved."""
    at_1430, at_1445 = _exp001_records()
    later = at_1445.model_copy(
        update={"bar": at_1445.bar.model_copy(update={"start_ms": 1_790_707_500_000, "end_ms": 1_790_708_400_000})}
    )
    decided_without_values = _receipt(1, None).model_copy(update={"decision_bar_close_ms": at_1445.bar.end_ms})

    response = build_strategy_view(
        _ema_view(),
        symbol="SPY",
        run_id="run-1",
        run_ids=frozenset({"run-1"}),
        # Started between the 14:30 close and the 14:45 close.
        run_started_at_ms=at_1430.bar.end_ms + 120_000,
        run_stopped_at_ms=None,
        before_start=[at_1430, at_1445, later],
        receipts=[decided_without_values],
    )

    assert [(c.bar_close_ms, c.phase) for c in response.candles] == [(at_1430.bar.end_ms, "before_start")]
    assert response.unexplained_decision_count == 1


def test_a_row_this_build_cannot_word_is_left_out_and_said_so() -> None:
    at_1430, _ = _exp001_records()
    unwordable = at_1430.model_copy(
        update={
            "checks": [at_1430.checks[1].model_copy(update={"comparison": "gt", "threshold": None, "observed": "x"})]
        }
    )

    response = build_strategy_view(
        _ema_view(),
        symbol="SPY",
        run_id="run-1",
        run_ids=frozenset({"run-1"}),
        run_started_at_ms=None,
        run_stopped_at_ms=None,
        before_start=[],
        receipts=[_receipt(1, unwordable)],
    )

    assert response.candles == []
    assert response.notices == [
        "This run saved no bars from before it started.",
        "1 decision's values could not be shown by this build.",
    ]


def test_saved_settings_that_no_longer_validate_mean_no_view() -> None:
    with pytest.raises(StrategyViewUnavailableError, match="no longer fit"):
        ResolvedStrategyView.for_settings("ema_crossover_signal", {"rsi_min": "not a number"}, symbol="SPY")


def test_the_normalized_gap_floor_is_reported_and_applied_when_set() -> None:
    strategy = EmaCrossoverSignalAlgorithm(symbol="SPY", gap=Decimal("0.10"), gap_bps=Decimal("40"))
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.initialize()
    strategy._ema10 = _ReadyIndicator(Decimal("100.00"))  # type: ignore[assignment]
    strategy._ema5 = _ReadyIndicator(Decimal("100.30"))  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[assignment]
    bar, _ = _ema_decision_at_rsi("60")

    decision = strategy.evaluate_signal_bar(bar)

    record = DecisionExplanationRecord.from_decision(bar, decision)
    assert record is not None
    gap_bps = next(check for check in record.checks if check.check_id == "gap_bps")
    # 0.30 over 100.00 is 30 bps, short of the 40 bps floor: no entry.
    assert (gap_bps.passed, gap_bps.observed, gap_bps.comparison) == (False, 30.0, "ge")
    assert decision.intent is None
    rendered = _ema_view(gap=0.1, gap_bps=40).render(record)
    assert next(c for c in rendered.checks if c.check_id == "gap_bps").needs == "≥ 40 bps"
