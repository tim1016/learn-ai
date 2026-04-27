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
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.jobs.progress import JobCancelled, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.models.requests import DatasetGenerationRequest
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from app.services.dataset_service import RunCancelledError
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


class DatasetZipJobRequest(_CamelCaseModel):
    """Body of POST /api/jobs-internal/dataset-zip.

    Carries every field the existing :class:`DatasetGenerationRequest`
    accepts — since the dataset request is an established schema, we
    accept it as a ``dataset`` sub-object rather than flattening, so the
    bundler keeps its single source of truth for shape validation.
    """

    job_id: str = Field(..., min_length=1)
    dataset: dict[str, Any] = Field(default_factory=dict)


class EngineBacktestJobRequest(_CamelCaseModel):
    """Body of POST /api/jobs-internal/engine-backtest.

    Mirrors the existing synchronous EngineBacktestRequest shape but
    accepts it as a ``backtest`` sub-object so the field validation
    remains the single source of truth. The .NET JobsApi forwards the
    Engine Lab POST body verbatim plus an injected ``job_id``.
    """

    job_id: str = Field(..., min_length=1)
    backtest: dict[str, Any] = Field(default_factory=dict)


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
        emit.log(f"Fetching {req.ticker} {req.multiplier}{req.timespan} bars from {req.from_date} to {req.to_date}")
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


@router.post("/dataset-zip", status_code=status.HTTP_202_ACCEPTED)
async def start_dataset_zip_job(req: DatasetZipJobRequest) -> dict:
    """Kick off a dataset fetch + bundle into a ZIP archive.

    Maps the existing dataset bundling pipeline (`_fetch_and_process` +
    `_build_zip_with_events`) onto the generic job framework. The pipeline
    already supports ``on_event`` and ``cancel_check`` callables — we
    plug those into :class:`ProgressEmitter` and
    :class:`CancellationCheck` respectively, so the inner code is
    unchanged. The final ZIP bytes go through ``completed_blob`` which
    parks them at ``job:{id}:result-blob``; the .NET ``GET /api/jobs/
    {id}/download`` streams them with the right Content-Disposition.
    """
    # Validate the embedded dataset payload through the existing schema.
    try:
        dataset_req = DatasetGenerationRequest.model_validate(req.dataset)
    except Exception as exc:  # pydantic ValidationError or shape mismatch
        raise HTTPException(status_code=400, detail=f"invalid dataset payload: {exc}")

    def work(emit: ProgressEmitter, cancel) -> None:
        # Late imports — these modules pull in pandas + polygon SDK and
        # would slow router import time if hoisted.
        from app.routers.dataset import _build_zip_with_events, _fetch_and_process

        def on_event(event: dict[str, Any]) -> None:
            # Forward the existing dataset event vocabulary unchanged so
            # the data-lab run-card UI keeps rendering chunks +
            # bundle-component checklists exactly as before. The
            # framework's terminal events (job.completed/failed) close
            # the SSE stream; chunk_progress et al. flow through as
            # arbitrary mid-run events.
            emit.emit_event(event.get("type", "event"), {k: v for k, v in event.items() if k != "type"})

        def cancel_check() -> bool:
            return cancel.should_cancel()

        emit.phase("loading_bars")
        try:
            df, column_meta, raw_count = _fetch_and_process(
                dataset_req,
                on_event=on_event,
                cancel_check=cancel_check,
            )
        except RunCancelledError as exc:
            # Translate the dataset chunker's cancellation exception into
            # the framework's so run_in_thread emits job.cancelled instead
            # of job.failed.
            raise JobCancelled(str(exc)) from exc
        on_event(
            {
                "type": "fetch_complete",
                "raw_bars": raw_count,
                "processed_bars": len(df),
                "indicator_columns": len([m["column"] for m in column_meta]),
            }
        )

        emit.phase("bundling")
        try:
            zip_bytes, filename = _build_zip_with_events(
                dataset_req,
                df,
                column_meta,
                raw_count,
                on_event=on_event,
                cancel_check=cancel_check,
            )
        except RunCancelledError as exc:
            raise JobCancelled(str(exc)) from exc

        emit.phase("packaging")
        emit.completed_blob(
            filename=filename,
            content_type="application/zip",
            body=zip_bytes,
        )
        # completed_blob already emits job.completed and writes the
        # result; returning None tells run_in_thread NOT to also call
        # completed() with a JSON value.
        return None

    # The dataset bundler's cancel_check is invoked once per chunk
    # (typically every few seconds), so the 1000-call cooldown in
    # CancellationCheck is too lazy. Force a Redis check on every call.
    run_in_thread(
        req.job_id,
        work,
        cancel_check_every_n=1,
        thread_name=f"dataset-zip-{req.job_id[:8]}",
    )
    return {"job_id": req.job_id, "status": "queued"}


@router.post("/engine-backtest", status_code=status.HTTP_202_ACCEPTED)
async def start_engine_backtest_job(req: EngineBacktestJobRequest) -> dict:
    """Kick off a LEAN-engine backtest in a worker thread. Returns 202.

    The Engine Lab UI is the primary caller; the .NET JobsApi forwards
    the request after minting the ``job_id`` and writing the initial
    state record to Redis. The worker emits ``phase`` and ``log`` events
    to the same Redis stream, then ``completed`` with the
    EngineBacktestResponse-shaped result body.
    """
    # Validate the embedded backtest body through the existing schema —
    # no duplication of field constraints.
    try:
        backtest_req = EngineBacktestRequest.model_validate(req.backtest)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid backtest payload: {exc}")

    def work(emit: ProgressEmitter, cancel) -> dict:
        # The engine itself is single-shot and does not poll for cancel
        # mid-run. We honor cancel at the obvious phase boundaries: just
        # before the data load and before invoking engine.run(). Once
        # the simulator starts, it runs to completion (typically
        # seconds-to-low-minutes for the strategies registered today).
        cancel.raise_if_cancelled()

        def on_phase(phase: str) -> None:
            cancel.raise_if_cancelled()
            emit.phase(phase)

        def on_log(message: str) -> None:
            emit.log(message)

        response = execute_engine_backtest(
            request=backtest_req,
            on_phase=on_phase,
            on_log=on_log,
        )
        cancel.raise_if_cancelled()

        # Pydantic v2: dict serialization preserves snake_case to match
        # what the frontend already deserializes from the synchronous
        # /api/engine/backtest endpoint.
        return response.model_dump(mode="json")

    run_in_thread(req.job_id, work, thread_name=f"engine-{req.job_id[:8]}")
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
