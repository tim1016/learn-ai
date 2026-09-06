"""HTTP contracts for the Python-owned backtest-run reads (PRD #1929).

Field names are the ones the retired GraphQL ``backtestRuns`` /
``backtestRun`` queries emitted — camelCase, ``totalPnL`` and ``pnL``
included — because the wire contract is unchanged in this slice; the
run-history table and the run report read exactly these names. The
snake_case-response convention in ``.claude/rules/python.md`` is therefore
deliberately not applied here.

The heavy envelopes — equity curve, validation analytics, data policy,
metric documentation — are served in their stored producer shape rather
than re-typed: the producer-side builders are their single source of
truth, and the components already read those names (the GraphQL layer
had aliased them back). Every temporal value is ``int64 ms UTC``; the
run's start and end are date-anchored values rendered as ``YYYY-MM-DD``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.research.backtest_runs.repository import ParityVerdictRow, RunDetail, RunSummary, TradeRow

Engine = Literal["PYTHON", "LEAN"]


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class BacktestRunSummaryResponse(_CamelModel):
    """One run-history row."""

    id: int
    source: str
    engine: Engine
    strategy_name: str
    symbol: str
    lean_run_id: str | None
    parameters: str
    start_date: str
    end_date: str
    executed_at: int
    total_trades: int
    total_pnl: float = Field(serialization_alias="totalPnL", validation_alias="totalPnL")
    commission_per_order: float | None
    brokerage_policy: str | None
    notes: str | None
    data_policy: dict[str, Any] | None
    verdict_grade: str | None
    verdict_signal: str | None
    parity_group_id: str | None
    has_synthetic_exit: bool

    @classmethod
    def from_repository(cls, row: RunSummary) -> BacktestRunSummaryResponse:
        return cls(
            id=row.id,
            source=row.source,
            engine=row.engine,
            strategy_name=row.strategy_name,
            symbol=row.symbol,
            lean_run_id=row.lean_run_id,
            parameters=row.parameters_json,
            start_date=row.start_date,
            end_date=row.end_date,
            executed_at=row.executed_at_ms,
            total_trades=row.total_trades,
            total_pnl=row.total_pnl,
            commission_per_order=row.commission_per_order,
            brokerage_policy=row.brokerage_policy,
            notes=row.notes,
            data_policy=_loads(row.data_policy_json),
            verdict_grade=row.verdict_grade,
            verdict_signal=row.verdict_signal,
            parity_group_id=row.parity_group_id,
            has_synthetic_exit=row.has_synthetic_exit,
        )


class BacktestRunTradeResponse(_CamelModel):
    id: int
    entry_timestamp: int
    exit_timestamp: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float = Field(serialization_alias="pnL", validation_alias="pnL")
    pnl_pts: float
    pnl_pct: float
    signal_reason: str
    is_synthetic_exit: bool

    @classmethod
    def from_repository(cls, trade: TradeRow) -> BacktestRunTradeResponse:
        return cls(
            id=trade.id,
            entry_timestamp=trade.entry_ms,
            exit_timestamp=trade.exit_ms,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            quantity=trade.quantity,
            pnl=trade.pnl,
            pnl_pts=trade.exit_price - trade.entry_price,
            pnl_pct=(trade.exit_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0,
            signal_reason=trade.signal_reason,
            is_synthetic_exit=trade.is_synthetic_exit,
        )


class BacktestRunParityVerdictResponse(_CamelModel):
    id: int
    status: str
    verdict_json: str
    created_at: int

    @classmethod
    def from_repository(cls, row: ParityVerdictRow) -> BacktestRunParityVerdictResponse:
        return cls(id=row.id, status=row.status, verdict_json=row.verdict_json, created_at=row.created_at_ms)


class BacktestRunDetailResponse(_CamelModel):
    """Everything the run report renders, plus the bounded trade evidence."""

    id: int
    engine: Engine
    source: str
    requested_engine: str | None
    strategy_name: str
    symbol: str
    lean_run_id: str | None
    parameters: str
    start_date: str
    end_date: str
    fill_mode: str
    executed_at: int
    duration_ms: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float = Field(serialization_alias="totalPnL", validation_alias="totalPnL")
    initial_cash: float
    commission_per_order: float | None
    final_equity: float
    total_fees: float
    max_drawdown: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    profit_factor: float | None
    lean_statistics_json: str | None
    lean_analysis_json: str | None
    verdict_json: str | None
    verdict_version: int | None
    verdict_grade: str | None
    verdict_signal: str | None
    equity_curve: dict[str, Any] | None
    validation_analytics: dict[str, Any] | None
    metric_documentation: list[dict[str, Any]]
    data_policy: dict[str, Any] | None
    insight_summary_json: str | None
    parity_group_id: str | None
    notes: str | None
    trades: list[BacktestRunTradeResponse]
    trades_truncated: bool
    parity_verdicts: list[BacktestRunParityVerdictResponse]

    @classmethod
    def from_repository(cls, run: RunDetail) -> BacktestRunDetailResponse:
        return cls(
            id=run.id,
            engine=run.engine,
            source=run.source,
            requested_engine=run.requested_engine,
            strategy_name=run.strategy_name,
            symbol=run.symbol,
            lean_run_id=run.lean_run_id,
            parameters=run.parameters_json,
            start_date=run.start_date,
            end_date=run.end_date,
            fill_mode=run.fill_mode,
            executed_at=run.executed_at_ms,
            duration_ms=run.duration_ms,
            total_trades=run.total_trades,
            winning_trades=run.winning_trades,
            losing_trades=run.losing_trades,
            win_rate=run.win_rate,
            total_pnl=run.total_pnl,
            initial_cash=run.initial_cash,
            commission_per_order=run.commission_per_order,
            final_equity=run.final_equity,
            total_fees=run.total_fees,
            max_drawdown=run.max_drawdown,
            sharpe_ratio=run.sharpe_ratio,
            sortino_ratio=run.sortino_ratio,
            profit_factor=run.profit_factor,
            lean_statistics_json=run.lean_statistics_json,
            lean_analysis_json=run.lean_analysis_json,
            verdict_json=run.run_verdict_json,
            verdict_version=run.verdict_version,
            verdict_grade=run.verdict_grade,
            verdict_signal=run.verdict_signal,
            equity_curve=_loads(run.equity_curve_json),
            validation_analytics=_loads(run.validation_analytics_json),
            metric_documentation=_loads(run.metric_documentation_json) or [],
            data_policy=_loads(run.data_policy_json),
            insight_summary_json=run.insight_summary_json,
            parity_group_id=run.parity_group_id,
            notes=run.notes,
            trades=[BacktestRunTradeResponse.from_repository(trade) for trade in run.trades],
            trades_truncated=run.trades_truncated,
            parity_verdicts=[BacktestRunParityVerdictResponse.from_repository(row) for row in run.parity_verdicts],
        )


class BacktestRunNotesRequest(BaseModel):
    notes: str | None = None


class BacktestRunNotesResponse(BaseModel):
    id: int
    notes: str | None


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)
