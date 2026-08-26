from __future__ import annotations

import hashlib

import pytest

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


def test_resolve_strategy_lean_source_rejects_strategy_without_lean_twin() -> None:
    with pytest.raises(StrategyLeanSourceNotFoundError, match="no registered LEAN"):
        resolve_strategy_lean_source("spy_orb")
