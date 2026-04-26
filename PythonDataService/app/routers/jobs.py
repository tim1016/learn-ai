"""Internal job-execution endpoints.

These routes are called by the .NET ``JobsController``, never directly
from the browser. The .NET layer owns the public ``/jobs/{type}``
endpoints, mints the ``job_id``, and writes the initial state record to
Redis. Python receives the ``job_id`` and runs the actual work, emitting
progress events to the same Redis keys.

The split keeps the architecture aligned with the project rule: Python
owns all math, .NET is transport.

Field naming
------------
Models accept **camelCase** at the wire because .NET hands the request
body through verbatim (it doesn't transcode field names). Internally the
fields are still ``snake_case`` per Python convention; Pydantic v2's
``alias_generator=to_camel`` + ``populate_by_name=True`` lets the
ingress route accept either form, so the same model works for in-process
tests using snake_case kwargs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.jobs.progress import ProgressEmitter
from app.jobs.runner import run_in_thread
from app.services.polygon_client import PolygonClientService
from app.services.rule_based_backtest import (
    RuleBasedBacktestResult,
    run_rule_based_backtest,
)

router = APIRouter()
logger = logging.getLogger(__name__)
polygon_client = PolygonClientService()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class _CamelCaseModel(BaseModel):
    """Base for job request bodies — accepts camelCase from .NET while
    preserving snake_case in code."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class RuleBasedBacktestJobRequest(_CamelCaseModel):
    """Body of POST /api/jobs-internal/backtest."""

    job_id: str = Field(..., min_length=1)
    ticker: str
    from_date: str  # YYYY-MM-DD
    to_date: str
    multiplier: int = 15
    timespan: str = "minute"
    parameters: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/backtest", status_code=status.HTTP_202_ACCEPTED)
async def start_rule_based_backtest_job(req: RuleBasedBacktestJobRequest) -> dict:
    """Kick off a rule-based backtest in a worker thread. Returns 202.

    The actual progress is observed by subscribing to the SSE stream
    served by the .NET layer at ``/jobs/{id}/events``.
    """
    if not req.ticker.strip():
        raise HTTPException(status_code=400, detail="ticker is required")

    def work(emit: ProgressEmitter, cancel) -> dict:
        # ----- Phase 1: load bars from Polygon -----
        emit.phase("loading_bars")
        emit.log(
            f"Fetching {req.ticker} {req.multiplier}{req.timespan} "
            f"bars from {req.from_date} to {req.to_date}"
        )
        cancel.raise_if_cancelled()

        bars = polygon_client.fetch_aggregates(
            ticker=req.ticker.upper(),
            multiplier=req.multiplier,
            timespan=req.timespan,
            from_date=req.from_date,
            to_date=req.to_date,
        )
        if not bars:
            raise ValueError(f"No bars returned for {req.ticker} in date range")

        emit.log(f"Fetched {len(bars)} bars")
        emit.progress(current=len(bars), total=len(bars), unit="bars", message="bars loaded")
        cancel.raise_if_cancelled()

        # ----- Phase 2: run the backtest -----
        emit.phase("simulating")
        # The rule-based engine is one-shot — it computes indicators and
        # iterates the dataframe internally. Coarse progress only:
        # phase boundaries are the meaningful signal here.
        result: RuleBasedBacktestResult = run_rule_based_backtest(
            ticker=req.ticker.upper(),
            bars=bars,
            params=req.parameters,
        )
        cancel.raise_if_cancelled()

        if not result.success:
            raise ValueError(result.error or "Backtest returned no result")

        # ----- Phase 3: serialize -----
        emit.phase("computing_stats")
        emit.progress(
            current=result.bars_processed,
            total=result.bars_processed,
            unit="bars",
            message=f"{result.total_trades} trades",
        )
        return _serialize(result)

    run_in_thread(req.job_id, work, thread_name=f"backtest-{req.job_id[:8]}")
    return {"job_id": req.job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(r: RuleBasedBacktestResult) -> dict:
    """Convert RuleBasedBacktestResult dataclass to a JSON-friendly dict.

    Mirrors the snake_case shape the .NET ``RuleBasedPythonResponse``
    deserializer already expects, so the existing GraphQL response type
    can re-use it when the caller fetches the result."""
    return {
        "success": r.success,
        "ticker": r.ticker,
        "strategy_name": r.strategy_name,
        "parameters": r.parameters,
        "total_trades": r.total_trades,
        "winning_trades": r.winning_trades,
        "losing_trades": r.losing_trades,
        "win_rate": r.win_rate,
        "avg_win_pct": r.avg_win_pct,
        "avg_loss_pct": r.avg_loss_pct,
        "win_loss_ratio": r.win_loss_ratio,
        "profit_factor": r.profit_factor,
        "expectancy_per_trade": r.expectancy_per_trade,
        "total_pnl_pct": r.total_pnl_pct,
        "max_drawdown_pct": r.max_drawdown_pct,
        "total_pnl_pts": r.total_pnl_pts,
        "sharpe_ratio": r.sharpe_ratio,
        "bars_processed": r.bars_processed,
        "trades": [
            {
                "trade_number": t.trade_number,
                "trade_type": t.trade_type,
                "entry_timestamp": t.entry_timestamp,
                "exit_timestamp": t.exit_timestamp,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "pnl_pct": t.pnl_pct,
                "cumulative_pnl_pct": t.cumulative_pnl_pct,
                "signal_reason": t.signal_reason,
                "ema_fast": t.ema_fast,
                "ema_slow": t.ema_slow,
                "ema_gap": t.ema_gap,
                "rsi": t.rsi,
                "adx": t.adx,
            }
            for t in r.trades
        ],
        "error": r.error,
    }
