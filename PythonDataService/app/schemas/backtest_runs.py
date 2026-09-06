"""HTTP contracts for the Python-owned backtest-run reads (PRD #1929).

Field names are the ones the retired GraphQL ``backtestRuns`` /
``backtestRun`` queries emitted — camelCase, ``totalPnL`` and ``pnL``
included — because the wire contract is unchanged in this slice; the
run-history table and the run report read exactly these names. The
snake_case-response convention in ``.claude/rules/python.md`` is therefore
deliberately not applied here.

Each response validates straight from the repository's row dataclass
(``from_attributes``): a wire field that renames a column names that column
as its validation alias, and the stored JSON envelopes — equity curve,
validation analytics, data policy, metric documentation — are parsed once
and served in their producer shape rather than re-typed. Every temporal
value is ``int64 ms UTC``; the run's start and end are date-anchored values
rendered as ``YYYY-MM-DD``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.research.backtest_runs.repository import Engine

RunSource = Literal["engine", "lean-sidecar"]


def _column(column: str, wire: str) -> Any:
    """A wire field read from a differently named row attribute."""
    return Field(validation_alias=column, serialization_alias=wire)


class _RunResponse(BaseModel):
    """The columns both reads share, validated from a ``RunRow``."""

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    id: int
    source: RunSource
    engine: Engine
    strategy_name: str
    symbol: str
    lean_run_id: str | None
    parameters: str = _column("parameters_json", "parameters")
    start_date: str
    end_date: str
    executed_at: int = _column("executed_at_ms", "executedAt")
    total_trades: int
    total_pnl: float = _column("total_pnl", "totalPnL")
    commission_per_order: float | None
    brokerage_policy: str | None
    notes: str | None
    data_policy: dict[str, Any] | None = _column("data_policy_json", "dataPolicy")
    verdict_grade: str | None
    verdict_signal: str | None
    parity_group_id: str | None

    @field_validator("data_policy", mode="before")
    @classmethod
    def _parse_json(cls, value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value


class BacktestRunSummaryResponse(_RunResponse):
    """One run-history row."""

    has_synthetic_exit: bool


class BacktestRunTradeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    id: int
    entry_timestamp: int = _column("entry_ms", "entryTimestamp")
    exit_timestamp: int = _column("exit_ms", "exitTimestamp")
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float = _column("pnl", "pnL")
    pnl_pts: float
    pnl_pct: float
    signal_reason: str
    is_synthetic_exit: bool


class BacktestRunParityVerdictResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    id: int
    status: str
    verdict_json: str
    created_at: int = _column("created_at_ms", "createdAt")


class BacktestRunDetailResponse(_RunResponse):
    """Everything the run report renders, plus the bounded trade evidence."""

    requested_engine: str | None
    fill_mode: str
    duration_ms: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    initial_cash: float
    final_equity: float
    total_fees: float
    max_drawdown: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    profit_factor: float | None
    lean_statistics_json: str | None
    lean_analysis_json: str | None
    verdict_json: str | None = _column("run_verdict_json", "verdictJson")
    verdict_version: int | None
    equity_curve: dict[str, Any] | None = _column("equity_curve_json", "equityCurve")
    validation_analytics: dict[str, Any] | None = _column("validation_analytics_json", "validationAnalytics")
    metric_documentation: list[dict[str, Any]] = _column("metric_documentation_json", "metricDocumentation")
    insight_summary_json: str | None
    trades: list[BacktestRunTradeResponse]
    trades_truncated: bool
    parity_verdicts: list[BacktestRunParityVerdictResponse]

    @field_validator("equity_curve", "validation_analytics", mode="before")
    @classmethod
    def _parse_envelope(cls, value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("metric_documentation", mode="before")
    @classmethod
    def _parse_documentation(cls, value: Any) -> Any:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed or []


class BacktestRunNotesRequest(BaseModel):
    notes: str | None = None


class BacktestRunNotesResponse(BaseModel):
    id: int
    notes: str | None
