from __future__ import annotations

import hashlib

import pytest

from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
from app.lean_sidecar.trusted_samples.ema_crossover_signal import EMA_CROSSOVER_SIGNAL_SOURCE
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
    assert result.source_sha256 == hashlib.sha256(EMA_CROSSOVER_SOURCE.encode("utf-8")).hexdigest()


def test_resolve_strategy_lean_source_rejects_strategy_without_lean_twin() -> None:
    with pytest.raises(StrategyLeanSourceNotFoundError, match="no registered LEAN"):
        resolve_strategy_lean_source("sma_crossover")


def test_rsi_mean_reversion_twin_pins_its_thresholds_as_constants_and_mirrors_the_canonical_decision_branches() -> None:
    """The twin is an oracle: rules are code, not GetParameter values.

    The registry forwards no ``lean_parameter_names`` for this strategy, so a
    run overriding these would be reported unrepresentable rather than
    compared against a twin still running 14/30/70.

    It also mirrors RsiMeanReversionAlgorithm.evaluate_signal_bar: strict
    thresholds, exit-before-entry, at most one action per bar.
    """
    source = resolve_strategy_lean_source("rsi_mean_reversion").source

    assert "RSI_PERIOD = 14" in source, "the RSI period is a constant, 14"
    assert "OVERSOLD = 30" in source, "the oversold threshold is a constant, 30"
    assert "OVERBOUGHT = 70" in source, "the overbought threshold is a constant, 70"
    for tunable in ("RSI_PERIOD", "OVERSOLD", "OVERBOUGHT"):
        assert f'GetParameter("{tunable.lower()}")' not in source, f"{tunable} is code, not a GetParameter value"
    assert "if rsi > self.OVERBOUGHT:" in source, "overbought is a strict threshold"
    assert "if rsi < self.OVERSOLD:" in source, "oversold is a strict threshold"
    assert "if not self.rsi.IsReady:" in source, "no action before the RSI is ready"
    assert "MovingAverageType.Wilders" in source, "the RSI is Wilder-smoothed"
    # End-of-run flattening, matching the canonical on_end_of_algorithm.
    assert "def OnEndOfAlgorithm(self):" in source, "the twin flattens at the end of the run"
