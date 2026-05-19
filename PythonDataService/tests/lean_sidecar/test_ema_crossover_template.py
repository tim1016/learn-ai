"""Smoke test: ema_crossover trusted template source is parseable and pinned to spec."""

from __future__ import annotations

import ast

import pytest

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE


def test_source_is_non_empty_string() -> None:
    assert isinstance(EMA_CROSSOVER_SOURCE, str)
    assert len(EMA_CROSSOVER_SOURCE) > 100


def test_source_parses_as_valid_python() -> None:
    ast.parse(EMA_CROSSOVER_SOURCE)


def test_class_constants_match_spec() -> None:
    """Pinned to PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json."""
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    constants: dict[str, int | float] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MyAlgorithm":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                ):
                    constants[stmt.targets[0].id] = stmt.value.value

    assert constants["FAST_PERIOD"] == 5
    assert constants["SLOW_PERIOD"] == 10
    assert constants["RSI_PERIOD"] == 14
    assert constants["BAR_MINUTES"] == 15
    assert constants["EXIT_BARS"] == 5
    assert constants["GAP_MIN"] == pytest.approx(0.20)
    assert constants["RSI_LO"] == 50
    assert constants["RSI_HI"] == 70


def test_source_contains_required_handlers() -> None:
    """Verify Initialize, OnConsolidatedBar, OnEndOfAlgorithm exist."""
    tree = ast.parse(EMA_CROSSOVER_SOURCE)
    method_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            method_names.add(node.name)

    assert "Initialize" in method_names
    assert "OnConsolidatedBar" in method_names
    assert "OnEndOfAlgorithm" in method_names


def test_source_consolidates_15_minute_bars() -> None:
    assert "TradeBarConsolidator" in EMA_CROSSOVER_SOURCE
    assert "timedelta(minutes=self.BAR_MINUTES)" in EMA_CROSSOVER_SOURCE


def test_source_uses_wilders_rsi() -> None:
    assert "MovingAverageType.Wilders" in EMA_CROSSOVER_SOURCE


def test_source_liquidates_at_end() -> None:
    assert "OnEndOfAlgorithm" in EMA_CROSSOVER_SOURCE
    assert "Liquidate(self.symbol)" in EMA_CROSSOVER_SOURCE


def test_source_does_not_override_fill_model() -> None:
    """LEAN's default fill model matches Engine Lab's signal_bar_close per Task 1.0 spike — no override needed."""
    assert "SetFillModel" not in EMA_CROSSOVER_SOURCE
    assert "SignalBarCloseFillModel" not in EMA_CROSSOVER_SOURCE
    assert "MarketOnOpenOrder" not in EMA_CROSSOVER_SOURCE
