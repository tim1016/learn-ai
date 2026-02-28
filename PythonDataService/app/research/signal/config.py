"""Signal engine configuration — frozen parameters."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalConfig:
    """Immutable signal engine configuration."""

    feature_name: str = "momentum_5m"
    horizon: int = 15
    thresholds: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)
    cost_bps_options: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0)
    default_cost_bps: float = 2.0
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    walk_forward_train_months: int = 3
    walk_forward_test_months: int = 1
    min_bars_for_signal: int = 500
