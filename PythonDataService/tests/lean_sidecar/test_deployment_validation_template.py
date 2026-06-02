from __future__ import annotations

import ast

from app.lean_sidecar.trusted_samples.deployment_validation import (
    DEPLOYMENT_VALIDATION_SOURCE,
)


def test_source_is_non_empty_string() -> None:
    assert isinstance(DEPLOYMENT_VALIDATION_SOURCE, str)
    assert len(DEPLOYMENT_VALIDATION_SOURCE) > 100


def test_source_parses_as_valid_python() -> None:
    ast.parse(DEPLOYMENT_VALIDATION_SOURCE)


def test_source_contains_required_handlers() -> None:
    tree = ast.parse(DEPLOYMENT_VALIDATION_SOURCE)
    method_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    assert "Initialize" in method_names
    assert "OnData" in method_names
    assert "OnEndOfAlgorithm" in method_names


def test_source_pins_strategy_constants() -> None:
    src = DEPLOYMENT_VALIDATION_SOURCE

    assert "START_AFTER = time(9, 45)" in src
    assert "STOP_AND_FLATTEN = time(15, 45)" in src
    assert "EXIT_BAR_COUNT = 3" in src


def test_source_uses_green_bar_definition_and_reset() -> None:
    src = DEPLOYMENT_VALIDATION_SOURCE

    assert "green = bar.Close > bar.Open" in src
    assert "self._reset_detection()" in src
    assert "self.entry_pending = True" in src


def test_source_writes_observations_and_state_csv() -> None:
    src = DEPLOYMENT_VALIDATION_SOURCE

    assert "observations.csv" in src
    assert "state.csv" in src
    assert "ms_utc,open,high,low,close,volume" in src
    assert "ts_ms_utc,open,close,green_streak,signal" in src


def test_source_pins_interactive_brokers_margin_brokerage() -> None:
    assert (
        "SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)"
        in DEPLOYMENT_VALIDATION_SOURCE
    )
