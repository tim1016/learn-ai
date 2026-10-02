"""A decision's displayed checks are the rule that decided it (#2639).

The checks are recorded, not golden-validated, so this is what pins them to
the decision instead: over each Signal Program's golden replay cells, a flat
bar stages ENTER exactly when every entry check passed, and a held bar
stages EXIT exactly when an exit check passed. It also pins each strategy's
explanation to its registered view, so a check the owner is shown always has
wording and a value always has a label.
"""

from __future__ import annotations

from pathlib import Path

import pytest


from app.engine.engine import BacktestEngine
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import SignalDecision
from app.services.spec_strategy_runner import InMemoryDataReader
from scripts.generate_signal_program_trace_corpus import (
    _DEFAULT_CELLS_ROOT,
    _CellManifest,
    _discover_cells,
    _minute_bars,
    _select_cells,
)

PROGRAM_KEYS = sorted(key for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None)


def _cells(program_key: str) -> list[_CellManifest]:
    contract = _STRATEGY_REGISTRY[program_key].signal_program_contract
    assert contract is not None
    return _select_cells(contract, _discover_cells(Path(_DEFAULT_CELLS_ROOT)))


def _replayed_decisions(program_key: str, cell: _CellManifest) -> list[SignalDecision]:
    """Every decision the program staged replaying one golden cell, in order."""
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    assert contract is not None
    strategy = registration.build(registration.param_schema(**{**contract.validated_settings, "symbol": cell.ticker}))
    decisions: list[SignalDecision] = []
    evaluate = strategy.evaluate_signal_bar  # type: ignore[attr-defined]

    def recording_evaluate(bar):  # type: ignore[no-untyped-def]
        decision = evaluate(bar)
        decisions.append(decision)
        return decision

    strategy.evaluate_signal_bar = recording_evaluate  # type: ignore[attr-defined]
    minute_bars = _minute_bars(Path(_DEFAULT_CELLS_ROOT) / cell.cell_id, cell.ticker)
    BacktestEngine.for_decision_identity(InMemoryDataReader(minute_bars)).run(strategy)
    return decisions


def _assert_checks_match_decisions(program_key: str, decisions: list[SignalDecision]) -> None:
    view = _STRATEGY_REGISTRY[program_key].strategy_view
    assert view is not None
    declared_values = {value.key for value in view.values}
    entries = exits = 0
    for decision in decisions:
        explanation = decision.explanation
        assert explanation is not None, "every decision reports an explanation"
        assert set(explanation.values) <= declared_values
        for check in explanation.checks:
            assert view.check(check.check_id) is not None, f"undeclared check {check.check_id}"
        kind = None if decision.intent is None else decision.intent.kind
        if not decision.ready:
            assert kind is None
            continue
        if explanation.holding:
            assert (kind is SignalIntentKind.EXIT) == explanation.exit_check_fires()
            exits += kind is SignalIntentKind.EXIT
        else:
            assert (kind is SignalIntentKind.ENTER) == explanation.entry_checks_pass()
            entries += kind is SignalIntentKind.ENTER
    assert entries > 0 and exits > 0, "the replay exercised both an entry and an exit"


@pytest.mark.parametrize("program_key", PROGRAM_KEYS)
def test_checks_match_decisions_on_the_shortest_golden_cell(program_key: str) -> None:
    cell = min(_cells(program_key), key=lambda candidate: (candidate.duration_days, candidate.cell_id))
    _assert_checks_match_decisions(program_key, _replayed_decisions(program_key, cell))


@pytest.mark.slow
@pytest.mark.parametrize("program_key", PROGRAM_KEYS)
def test_checks_match_decisions_over_the_whole_golden_corpus(program_key: str) -> None:
    for cell in _cells(program_key):
        _assert_checks_match_decisions(program_key, _replayed_decisions(program_key, cell))
