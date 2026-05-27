"""Pydantic DTO for ``BacktestRunResult`` — the wire/storage shape.

All timestamps are ``int64 ms UTC`` per
``.claude/rules/numerical-rigor.md`` § "Timestamp rigor". Decimal money/
price values surface as ``float`` at this boundary because the consumer
is JSON. Math has already happened in the engine using ``Decimal``;
this layer is transport.

The result DTO is the input to Phase B (workbench) and the parent record
for Phases C-E (walk-forward folds, Monte Carlo simulations, robustness
tests, null baselines, sensitivity sweeps). Five forward-compat fields
documented in the architecture spec are present from v1:
``parent_run_id`` and ``parent_spec_hash`` on the ledger;
``random_seed`` on the ledger; ``bars_held`` per trade; ``warnings``
on the result.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EquityCurvePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_ms: int
    equity: float


class DrawdownPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_ms: int
    drawdown_pct: float = Field(ge=0.0, le=1.0)


class RunTrade(BaseModel):
    """Round-trip trade in wire/storage shape.

    Mirrors ``app.engine.strategy.base.LoggedTrade`` but with timestamps
    converted to ``int64 ms UTC`` and Decimal fields cast to float. The
    ``bars_held`` field is computed by the runner — it does not exist
    on ``LoggedTrade`` itself.
    """

    model_config = ConfigDict(extra="forbid")

    trade_number: int
    entry_time_ms: int
    entry_price: float
    exit_time_ms: int
    exit_price: float
    indicators_at_entry: dict[str, float] = Field(default_factory=dict)
    pnl_pts: float
    pnl_pct: float
    result: Literal["WIN", "LOSS"]
    signal_reason: str = ""
    bars_held: int = Field(ge=0)


class RunMetrics(BaseModel):
    """Headline metrics for a backtest run.

    All numeric fields are sourced from
    ``app.engine.results.statistics.summarize`` — Angular formats,
    never computes. Fields that ``summarize`` may emit as None
    (degenerate Sharpe, no losses → no profit-factor denominator,
    etc.) stay None here rather than collapsing to a sentinel.

    ``exposure_pct`` and ``avg_trade_bars`` are derived by the runner
    from the trade list and the engine's bar stream. Both are optional
    in v1 because precise exposure requires consolidated-bar boundaries
    that the engine result doesn't expose cleanly today; the runner fills
    them when the inputs allow and leaves them None otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float | None = None
    total_return_pct: float
    max_drawdown_pct: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    profit_factor: float | None = None
    expectancy_pct: float | None = None
    payoff_ratio: float | None = None
    exposure_pct: float | None = None
    avg_trade_bars: float | None = None


class BacktestRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    initial_cash: float
    final_equity: float
    equity_curve: list[EquityCurvePoint] = Field(default_factory=list)
    drawdown_curve: list[DrawdownPoint] = Field(default_factory=list)
    trades: list[RunTrade] = Field(default_factory=list)
    metrics: RunMetrics
    log_lines: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    bars_consumed: int = Field(default=0, ge=0)
