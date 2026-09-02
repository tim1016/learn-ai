from __future__ import annotations

import hashlib

import pytest

from app.lean_sidecar.trusted_samples.ema_crossover_signal import EMA_CROSSOVER_SIGNAL_SOURCE
from app.lean_sidecar.trusted_samples.rsi_mean_reversion import RSI_MEAN_REVERSION_SOURCE
from app.services.strategy_lean_source_service import (
    StrategyLeanSourceNotFoundError,
    resolve_strategy_lean_source,
)


def test_resolve_strategy_lean_source_returns_registered_qc_algorithm() -> None:
    result = resolve_strategy_lean_source("ema_crossover_signal")

    assert result.template == "ema_crossover_signal"
    assert result.source == EMA_CROSSOVER_SIGNAL_SOURCE
    assert "class MyAlgorithm(QCAlgorithm)" in result.source
    assert result.source_sha256 == hashlib.sha256(result.source.encode("utf-8")).hexdigest()


def test_resolve_strategy_lean_source_rejects_strategy_without_lean_twin() -> None:
    with pytest.raises(StrategyLeanSourceNotFoundError, match="no registered LEAN"):
        resolve_strategy_lean_source("sma_crossover")


def test_resolve_strategy_lean_source_returns_rsi_mean_reversion_twin() -> None:
    result = resolve_strategy_lean_source("rsi_mean_reversion")

    assert result.template == "rsi_mean_reversion"
    assert result.source == RSI_MEAN_REVERSION_SOURCE
    assert "class MyAlgorithm(QCAlgorithm)" in result.source
    assert result.source_sha256 == hashlib.sha256(result.source.encode("utf-8")).hexdigest()


def test_rsi_mean_reversion_twin_pins_its_thresholds_as_constants() -> None:
    """The twin is an oracle: rules are code, not GetParameter values.

    The registry forwards no ``lean_parameter_names`` for this strategy, so a
    run overriding these would be reported unrepresentable rather than
    compared against a twin still running 14/30/70.
    """
    source = resolve_strategy_lean_source("rsi_mean_reversion").source

    assert "RSI_PERIOD = 14" in source
    assert "OVERSOLD = 30" in source
    assert "OVERBOUGHT = 70" in source
    for tunable in ("RSI_PERIOD", "OVERSOLD", "OVERBOUGHT"):
        assert f'GetParameter("{tunable.lower()}")' not in source


def test_rsi_mean_reversion_twin_mirrors_the_canonical_decision_branches() -> None:
    """Strict thresholds, exit-before-entry, at most one action per bar.

    Mirrors RsiMeanReversionAlgorithm.evaluate_signal_bar.
    """
    source = resolve_strategy_lean_source("rsi_mean_reversion").source

    assert "if rsi > self.OVERBOUGHT:" in source
    assert "if rsi < self.OVERSOLD:" in source
    assert "if not self.rsi.IsReady:" in source
    assert "MovingAverageType.Wilders" in source
    # End-of-run flattening, matching the canonical on_end_of_algorithm.
    assert "def OnEndOfAlgorithm(self):" in source
