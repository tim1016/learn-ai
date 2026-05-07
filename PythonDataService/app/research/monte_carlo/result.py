"""Pydantic DTOs for Monte Carlo configurations and results.

Mirrors the Phase A / C pattern: ``int64 ms UTC`` timestamps,
``Decimal``→``float`` at the wire boundary, ``extra='forbid'`` to make
schema drift loud. Identity columns (``monte_carlo_id``,
``parent_run_id``) are stable across reruns of the same configuration.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MonteCarloMethod = Literal["reshuffle", "resample"]


class EquityBandPoint(BaseModel):
    """One trade-index slice of the simulated-equity distribution.

    Index 0 is ``initial_equity`` (before any trade); subsequent
    indices are after the i-th trade in each simulation. P5/P50/P95
    are the 5th, 50th, and 95th percentiles across the simulation
    batch at this index — visualised as a fan chart on the UI.
    """

    model_config = ConfigDict(extra="forbid")

    trade_index: int
    p5: float
    p50: float
    p95: float


class BreachProbability(BaseModel):
    """Probability that *any* simulation hit a drawdown >= threshold.

    Threshold is a positive fraction (``0.10`` = 10% drawdown). The
    probability is the *fraction of simulations* whose realised
    max-drawdown met or exceeded the threshold — a sample estimate
    of P(MDD >= threshold) under the simulation distribution.
    """

    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(ge=0.0, le=1.0)
    probability: float = Field(ge=0.0, le=1.0)


class MonteCarloConfig(BaseModel):
    """Persistable record of the *inputs* that produced an MC result.

    ``parent_run_id`` is required — Monte Carlo always operates on
    a parent run's trade list (we don't simulate from thin air). The
    parent's ledger is the authoritative source of the trades; MC
    persists only its own derivation.
    """

    model_config = ConfigDict(extra="forbid")

    monte_carlo_id: str
    parent_run_id: str
    parent_trade_log_hash: str  # locks the trade-list version this MC ran against
    method: MonteCarloMethod
    simulation_count: int = Field(ge=1)
    projection_trade_count: int = Field(
        ge=0,
        description=(
            "Length of each simulated path. 0 means 'use the parent run's "
            "trade count' (standard reshuffle/bootstrap). >0 extends past "
            "the historical count — only valid for resample."
        ),
    )
    initial_equity: float = Field(gt=0.0)
    random_seed: int
    breach_thresholds: list[float] = Field(default_factory=list)
    created_at_ms: int


class MonteCarloResult(BaseModel):
    """Aggregated Monte Carlo output.

    ``equity_bands`` is the central artifact — one entry per trade
    index (0..N inclusive), each carrying the 5th/50th/95th
    percentile of simulated equity at that index. Drawdown / streak /
    terminal-PnL are scalar quantile dicts for the workbench's
    summary cards.
    """

    model_config = ConfigDict(extra="forbid")

    monte_carlo_id: str
    parent_run_id: str
    method: MonteCarloMethod
    simulation_count: int
    realised_trade_count: int = Field(
        description=(
            "Length of each simulated path actually used (resolved from "
            "``projection_trade_count`` or the parent's trade count)."
        ),
    )
    equity_bands: list[EquityBandPoint] = Field(default_factory=list)
    drawdown_quantiles: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Keys are percentile labels (``p5``/``p50``/``p95``); values "
            "are realised max-drawdown fractions in [0, 1]."
        ),
    )
    terminal_pnl_quantiles: dict[str, float] = Field(default_factory=dict)
    max_losing_streak_quantiles: dict[str, int] = Field(default_factory=dict)
    breach_probabilities: list[BreachProbability] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at_ms: int
    completed_at_ms: int | None = None
    status: Literal["completed", "failed"] = "completed"
    failure_reason: str | None = None
