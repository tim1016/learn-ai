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
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.jobs import cache as result_cache
from app.jobs.phases import friendly as friendly_phase
from app.jobs.progress import JobCancelled, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.models.requests import DatasetGenerationRequest
from app.research.batch_runner import (
    CrossSectionalReport,
    run_cross_sectional_study,
)
from app.research.config import ResearchConfig
from app.research.runner import run_feature_research
from app.research.signal.config import SignalConfig
from app.research.signal.engine import run_signal_engine
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from app.schemas.ticker_request import (
    MultiTickerRequest,
    TickerRequest,
)
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


class _CamelCaseTickerRequest(TickerRequest):
    """Composes ``TickerRequest`` with the camelCase wire convention
    used by .NET-forwarded job bodies.

    Pydantic quirk: a field-level ``validation_alias`` overrides the
    class's ``alias_generator``, so the base's ``from_date`` /
    ``to_date`` / ``symbol`` fields (no longer aliased after PR (iii)
    removed the legacy-name shims) won't pick up the camelCase form
    automatically. We redeclare those three fields here with
    ``AliasChoices`` covering both the snake_case canonical AND its
    camelCase wire variant.

    ``extra="forbid"`` is preserved from the base — unknown fields
    surface as ``extra_forbidden`` 422.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    # Re-declare with AliasChoices covering snake_case canonical + the
    # camelCase wire variant the .NET JobsApi sends. Legacy snake_case
    # names (start_date / end_date / ticker) are no longer accepted
    # post-PR (iii).
    from_date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("from_date", "fromDate"),
    )
    to_date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("to_date", "toDate"),
    )
    symbol: str = Field(
        ...,
        min_length=1,
        max_length=20,
    )


class _CamelCaseMultiTickerRequest(MultiTickerRequest):
    """Multi-symbol equivalent of ``_CamelCaseTickerRequest``."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    from_date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("from_date", "fromDate"),
    )
    to_date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("to_date", "toDate"),
    )
    symbols: list[str] = Field(
        ...,
        min_length=1,
    )


class RuleBasedBacktestJobRequest(_CamelCaseTickerRequest):
    """Body of POST /api/jobs-internal/backtest.

    **Default override**: ``multiplier=15`` to preserve the
    pre-migration default (the rule-based backtest path defaulted to
    15-minute bars before this PR). Without the override, the inherited
    base default of 1 would silently switch every caller to 1-minute
    bars.
    """

    # Override base default — preserves pre-migration multiplier=15.
    multiplier: int = Field(15, ge=1)

    job_id: str = Field(..., min_length=1)
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


class CrossSectionalJobRequest(_CamelCaseMultiTickerRequest):
    """Body of POST /api/jobs-internal/cross-sectional.

    The Frontend posts the same shape it currently sends to the GraphQL
    ``runBatchOptionsResearch`` mutation, plus an injected ``job_id``
    from the .NET JobsApi.
    """

    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    target_type: str = "directional"
    force: bool = False
    """Skip the result cache and always re-run. Default: serve from
    cache if a prior run with identical params is still warm."""


class FeatureResearchJobRequest(_CamelCaseTickerRequest):
    """Body of POST /api/jobs-internal/feature-research.

    The runner fetches bars from Polygon itself — the Frontend sends only
    the symbol + date range + feature, no payload of OHLCV. This keeps
    the .NET round-trip small and matches the cross-sectional pattern.

    Inherits all defaults from the base (``multiplier=1``,
    ``timespan="minute"``, ``session="rth"``) — those match the
    pre-migration shape exactly.
    """

    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    force: bool = False


class SignalEngineJobRequest(_CamelCaseTickerRequest):
    """Body of POST /api/jobs-internal/signal-engine.

    **Default override**: ``multiplier=15`` to preserve the
    pre-migration default. Signal-engine processing operates on
    15-minute bars by default; the inherited base default of 1 would
    silently switch every caller to 1-minute bars.
    """

    # Override base default — preserves pre-migration multiplier=15.
    multiplier: int = Field(15, ge=1)

    job_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    flip_sign: bool = True
    regime_gate_enabled: bool = True
    force: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/backtest", status_code=status.HTTP_202_ACCEPTED)
async def start_rule_based_backtest_job(req: RuleBasedBacktestJobRequest) -> dict:
    """Kick off a rule-based backtest in a worker thread. Returns 202.

    The actual progress is observed by subscribing to the SSE stream
    served by the .NET layer at ``/jobs/{id}/events``.
    """
    if not req.symbol.strip():
        raise HTTPException(status_code=400, detail="symbol is required")

    def work(emit: ProgressEmitter, cancel) -> dict:
        # ----- Phase 1: load bars from Polygon -----
        emit.phase("loading_bars")
        emit.log(f"Fetching {req.symbol} {req.multiplier}{req.timespan} bars from {req.from_date} to {req.to_date}")
        cancel.raise_if_cancelled()

        bars = polygon_client.fetch_aggregates(
            ticker=req.symbol.upper(),
            multiplier=req.multiplier,
            timespan=req.timespan,
            from_date=req.from_date,
            to_date=req.to_date,
        )
        if not bars:
            raise ValueError(f"No bars returned for {req.symbol} in date range")

        emit.log(f"Fetched {len(bars)} bars")
        emit.progress(current=len(bars), total=len(bars), unit="bars", message="bars loaded")
        cancel.raise_if_cancelled()

        # ----- Phase 2: run the backtest -----
        emit.phase("simulating")
        # The rule-based engine is one-shot — it computes indicators and
        # iterates the dataframe internally. Coarse progress only:
        # phase boundaries are the meaningful signal here.
        result: RuleBasedBacktestResult = run_rule_based_backtest(
            ticker=req.symbol.upper(),
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
            # Raises JobCancelled when set; otherwise returns False so
            # call sites can use it either as a poll or as a sync barrier.
            cancel.raise_if_cancelled()
            return False

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


@router.post("/cross-sectional", status_code=status.HTTP_202_ACCEPTED)
async def start_cross_sectional_job(req: CrossSectionalJobRequest) -> dict:
    """Kick off a cross-sectional study in a worker thread. Returns 202.

    The runner processes tickers sequentially — ~30–90 seconds per ticker
    on Polygon Starter — so we wire ``ProgressEmitter`` into the runner's
    optional ``on_phase`` / ``on_log`` / ``on_progress`` callbacks. The
    Frontend's run-dock subscribes to the SSE stream and renders each
    ticker's status as it lands.

    A successful prior run with identical params is served from cache —
    the response carries ``status=cached`` and the worker thread is
    skipped. Pass ``force=true`` in the body to bypass the cache.
    """
    # Note: ``req.symbols`` is already validated by Pydantic (min_length=1
    # on the list, plus per-element ``min_length=1, max_length=20`` from
    # ``MultiTickerRequest``); an empty list or empty-string element fails
    # at validation, never reaches here.
    if not req.feature_name.strip():
        raise HTTPException(status_code=400, detail="feature_name is required")

    cache_params = {
        "feature_name": req.feature_name,
        "tickers": req.symbols,
        "from_date": req.from_date,
        "to_date": req.to_date,
        "target_type": req.target_type,
    }
    if not req.force:
        hit = result_cache.lookup("cross_sectional", cache_params)
        if hit is not None:
            _, cached = hit
            result_cache.serve_cached_result(req.job_id, "cross_sectional", cached)
            return {"job_id": req.job_id, "status": "cached"}

    def work(emit: ProgressEmitter, cancel) -> dict:
        cancel.raise_if_cancelled()
        emit.phase("starting")
        emit.log(
            f"Starting cross-sectional study on {len(req.symbols)} ticker"
            f"{'' if len(req.symbols) == 1 else 's'} "
            f"({', '.join(t.upper() for t in req.symbols[:5])}"
            f"{'…' if len(req.symbols) > 5 else ''}) "
            f"for feature '{req.feature_name}'"
        )

        def on_phase(phase: str) -> None:
            cancel.raise_if_cancelled()
            emit.phase(phase)

        def on_log(message: str) -> None:
            emit.log(message)

        def on_progress(current: int, total: int, message: str) -> None:
            emit.progress(
                current=current,
                total=total,
                unit="tickers",
                message=message,
            )

        def cancel_check() -> bool:
            # Raises JobCancelled when set; otherwise returns False so
            # call sites can use it either as a poll or as a sync barrier.
            cancel.raise_if_cancelled()
            return False

        report: CrossSectionalReport = run_cross_sectional_study(
            feature_name=req.feature_name,
            tickers=[t.upper() for t in req.symbols],
            start_date=req.from_date,
            end_date=req.to_date,
            polygon_client=polygon_client,
            target_type=req.target_type,
            on_phase=on_phase,
            on_log=on_log,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )

        cancel.raise_if_cancelled()
        emit.phase("completed")
        emit.log(report.summary)

        # Snake-case dict the Frontend mapper transforms into camelCase
        # ``BatchResearchResult``. ``tickers_tested`` is kept as a legacy
        # alias for the raw count so any older consumer keeps working;
        # new consumers should use ``tickers_tested_raw`` /
        # ``tickers_valid`` / ``validity_summary``.
        result = {
            "success": True,
            "feature_name": report.feature_name,
            "target_type": report.target_type,
            "tickers_tested": report.tickers_tested_raw,
            "tickers_tested_raw": report.tickers_tested_raw,
            "tickers_valid": report.tickers_valid,
            "tickers_passed": report.tickers_passed,
            "pass_rate": report.pass_rate,
            "cross_sectional_consistent": report.cross_sectional_consistent,
            "aggregate_ic": report.aggregate_ic,
            "aggregate_ic_uniform": report.aggregate_ic_uniform,
            "aggregate_ic_ci": asdict(report.aggregate_ic_ci),
            "binomial_test": asdict(report.binomial_test),
            "n_eff_assets": report.n_eff_assets,
            "n_eff_assets_method": report.n_eff_assets_method,
            "validity_summary": asdict(report.validity_summary),
            "stage_info": asdict(report.stage_info),
            "ticker_results": report.ticker_results,
            "summary": report.summary,
        }
        result_cache.store("cross_sectional", cache_params, result)
        return result

    run_in_thread(
        req.job_id,
        work,
        thread_name=f"cross-sectional-{req.job_id[:8]}",
        # The cross-sectional loop calls cancel_check() once per ticker
        # (every ~30–90s), and the runner's no-op callbacks otherwise
        # make no calls. Force a Redis check on every call to honour
        # cancel requests promptly even mid-ticker.
        cancel_check_every_n=1,
    )
    return {"job_id": req.job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# Feature research / signal engine — single-ticker job dispatch
# ---------------------------------------------------------------------------


def _emit_friendly_phase(
    emit: ProgressEmitter,
    cancel,
    job_type: str,
    phase_id: str,
    message: str | None = None,
) -> None:
    """Emit a phase event plus a friendly log line for it.

    Centralizes the pattern of ``cancel.raise_if_cancelled() →
    emit.phase(id) → emit.log(<friendly label>)`` so each stage in a
    runner reads as one line. ``message`` overrides the default
    friendly label (used when the runner has more specific information,
    like "Computing IC across 1,260 windows").
    """
    cancel.raise_if_cancelled()
    emit.phase(phase_id)
    label = message if message is not None else friendly_phase(job_type, phase_id)
    emit.log(label)


@router.post("/feature-research", status_code=status.HTTP_202_ACCEPTED)
async def start_feature_research_job(req: FeatureResearchJobRequest) -> dict:
    """Kick off a feature-validation experiment in a worker thread.

    The runner fetches bars from Polygon, computes the feature, runs IC
    + stationarity + quantile + robustness analysis, and assigns a
    0/1/2/3 stage verdict via the per-feature validation contract.
    Phases stream over SSE so the user sees what's happening.

    A successful prior run with identical (ticker, feature, range,
    bar resolution) is served from cache. Pass ``force=true`` to bypass.
    """
    if not req.symbol.strip():
        raise HTTPException(status_code=400, detail="symbol is required")
    if not req.feature_name.strip():
        raise HTTPException(status_code=400, detail="feature_name is required")

    cache_params = {
        "ticker": req.symbol.upper(),
        "feature_name": req.feature_name,
        "from_date": req.from_date,
        "to_date": req.to_date,
        "multiplier": req.multiplier,
        "timespan": req.timespan,
    }
    if not req.force:
        hit = result_cache.lookup("feature_research", cache_params)
        if hit is not None:
            _, cached = hit
            result_cache.serve_cached_result(req.job_id, "feature_research", cached)
            return {"job_id": req.job_id, "status": "cached"}

    def work(emit: ProgressEmitter, cancel) -> dict:
        ticker = req.symbol.upper()

        # ── Phase: load bars ─────────────────────────────────────────
        _emit_friendly_phase(
            emit,
            cancel,
            "feature_research",
            "loading_bars",
            f"Loading {ticker} {req.multiplier}{req.timespan} bars "
            f"from {req.from_date} to {req.to_date}",
        )

        bars = polygon_client.fetch_aggregates(
            ticker=ticker,
            multiplier=req.multiplier,
            timespan=req.timespan,
            from_date=req.from_date,
            to_date=req.to_date,
        )
        if not bars:
            raise ValueError(f"No bars returned for {ticker} in date range")
        emit.log(f"Loaded {len(bars):,} bars")
        emit.progress(current=len(bars), total=len(bars), unit="bars", message="bars loaded")

        # The runner emits its own phase events via callbacks; we forward
        # them through the friendly-label table so the UI text reads
        # cleanly without the runner having to know about phase ids.
        def on_phase(phase: str) -> None:
            cancel.raise_if_cancelled()
            emit.phase(phase)

        def on_log(message: str, level: str = "info") -> None:
            emit.log(message, level=level)

        def on_progress(current: int, total: int, unit: str = "windows", message: str | None = None) -> None:
            emit.progress(current=current, total=total, unit=unit, message=message)

        def cancel_check() -> bool:
            # Raises JobCancelled when set; otherwise returns False so
            # call sites can use it either as a poll or as a sync barrier.
            cancel.raise_if_cancelled()
            return False

        report = run_feature_research(
            ticker=ticker,
            feature_name=req.feature_name,
            bars=bars,
            start_date=req.from_date,
            end_date=req.to_date,
            config=ResearchConfig(),
            on_phase=on_phase,
            on_log=on_log,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )

        if report.error:
            # Surface the failure as an exception so run_in_thread emits
            # job.failed (the runner caught the exception internally to
            # populate report.error; here we re-raise so the SSE consumer
            # sees a clean terminal event instead of "completed but
            # error_field=…").
            raise ValueError(report.error)

        cancel.raise_if_cancelled()
        _emit_friendly_phase(emit, cancel, "feature_research", "completed")

        result = _serialize_feature_report(report)
        result_cache.store("feature_research", cache_params, result)
        return result

    run_in_thread(
        req.job_id,
        work,
        thread_name=f"feature-{req.job_id[:8]}",
        cancel_check_every_n=100,
    )
    return {"job_id": req.job_id, "status": "queued"}


@router.post("/signal-engine", status_code=status.HTTP_202_ACCEPTED)
async def start_signal_engine_job(req: SignalEngineJobRequest) -> dict:
    """Kick off a signal-engine run in a worker thread.

    Eight phases (load bars → graduation). Long phases (backtest grid,
    walk-forward) emit fine-grained ``on_progress`` so the bar moves
    while the worker is mid-sweep.
    """
    if not req.symbol.strip():
        raise HTTPException(status_code=400, detail="symbol is required")
    if not req.feature_name.strip():
        raise HTTPException(status_code=400, detail="feature_name is required")

    cache_params = {
        "ticker": req.symbol.upper(),
        "feature_name": req.feature_name,
        "from_date": req.from_date,
        "to_date": req.to_date,
        "multiplier": req.multiplier,
        "timespan": req.timespan,
        "flip_sign": req.flip_sign,
        "regime_gate_enabled": req.regime_gate_enabled,
    }
    if not req.force:
        hit = result_cache.lookup("signal_engine", cache_params)
        if hit is not None:
            _, cached = hit
            result_cache.serve_cached_result(req.job_id, "signal_engine", cached)
            return {"job_id": req.job_id, "status": "cached"}

    def work(emit: ProgressEmitter, cancel) -> dict:
        ticker = req.symbol.upper()

        _emit_friendly_phase(
            emit,
            cancel,
            "signal_engine",
            "loading_bars",
            f"Loading {ticker} {req.multiplier}{req.timespan} bars "
            f"from {req.from_date} to {req.to_date}",
        )
        bars = polygon_client.fetch_aggregates(
            ticker=ticker,
            multiplier=req.multiplier,
            timespan=req.timespan,
            from_date=req.from_date,
            to_date=req.to_date,
        )
        if not bars:
            raise ValueError(f"No bars returned for {ticker} in date range")
        emit.log(f"Loaded {len(bars):,} bars")

        def on_phase(phase: str) -> None:
            cancel.raise_if_cancelled()
            emit.phase(phase)

        def on_log(message: str, level: str = "info") -> None:
            emit.log(message, level=level)

        def on_progress(current: int, total: int, unit: str = "configs", message: str | None = None) -> None:
            emit.progress(current=current, total=total, unit=unit, message=message)

        def cancel_check() -> bool:
            # Raises JobCancelled when set; otherwise returns False so
            # call sites can use it either as a poll or as a sync barrier.
            cancel.raise_if_cancelled()
            return False

        config = SignalConfig(
            feature_name=req.feature_name,
            flip_sign=req.flip_sign,
            regime_gate_enabled=req.regime_gate_enabled,
        )
        report = run_signal_engine(
            ticker=ticker,
            feature_name=req.feature_name,
            bars=bars,
            start_date=req.from_date,
            end_date=req.to_date,
            config=config,
            on_phase=on_phase,
            on_log=on_log,
            on_progress=on_progress,
            cancel_check=cancel_check,
        )

        if report.error:
            raise ValueError(report.error)

        cancel.raise_if_cancelled()
        _emit_friendly_phase(emit, cancel, "signal_engine", "completed")

        result = _serialize_signal_report(report)
        result_cache.store("signal_engine", cache_params, result)
        return result

    run_in_thread(
        req.job_id,
        work,
        thread_name=f"signal-{req.job_id[:8]}",
        cancel_check_every_n=50,
    )
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


def _serialize_target(target: Any) -> dict:
    """Project a ``TargetResult`` to a JSON-friendly dict that mirrors
    the GraphQL ``TargetMetadata`` shape the Frontend already consumes
    via ``runFeatureResearch``. The bulky ``values``/``timestamps``
    Series are intentionally dropped — only the metadata that drives
    the UI disclosure travels with the report. ``invalid_reason_counts``
    is emitted as a list of ``{reason, count}`` so the async path
    matches the projection Hot Chocolate emits for the sync path.
    """
    return {
        "target_name": target.target_name,
        "horizon_minutes": target.horizon_minutes,
        "horizon_bars": target.horizon_bars,
        "bar_minutes": target.bar_minutes,
        "timezone": target.timezone,
        "valid_count": target.valid_count,
        "total_count": target.total_count,
        "valid_ratio": target.valid_ratio,
        "invalid_reason_counts": [
            {"reason": reason, "count": count}
            for reason, count in target.invalid_reason_counts.items()
        ],
    }


def _serialize_feature_report(report: Any) -> dict:
    """Serialize a ResearchReport for storage in the result cache and
    delivery to the Frontend.

    Hand-rolled (rather than ``asdict``) so we can keep the Pydantic-
    response-model shape stable: nested dataclasses become dicts and we
    flatten the validation verdict.
    """
    return {
        "success": report.error is None,
        "ticker": report.ticker,
        "feature_name": report.feature_name,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "bars_used": report.bars_used,
        "mean_ic": report.mean_ic,
        "ic_t_stat": report.ic_t_stat,
        "ic_p_value": report.ic_p_value,
        "nw_t_stat": report.nw_t_stat,
        "nw_p_value": report.nw_p_value,
        "effective_n": report.effective_n,
        "ic_values": report.ic_values,
        "ic_dates": report.ic_dates,
        "adf_pvalue": report.adf_pvalue,
        "kpss_pvalue": report.kpss_pvalue,
        "is_stationary": report.is_stationary,
        "quantile_bins": report.quantile_bins,
        "is_monotonic": report.is_monotonic,
        "monotonicity_ratio": report.monotonicity_ratio,
        "robustness": asdict(report.robustness) if report.robustness is not None else None,
        "feature_spec": asdict(report.feature_spec) if report.feature_spec is not None else None,
        "validation_verdict": (
            asdict(report.validation_verdict) if report.validation_verdict is not None else None
        ),
        "target": _serialize_target(report.target) if report.target is not None else None,
        "passed_validation": report.passed_validation,
        "error": report.error,
    }


def _serialize_signal_report(report: Any) -> dict:
    """Serialize a SignalEngineReport for storage and delivery."""
    return {
        "success": report.error is None,
        "ticker": report.ticker,
        "feature_name": report.feature_name,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "bars_used": report.bars_used,
        "flip_sign": report.flip_sign,
        "thresholds_tested": report.thresholds_tested,
        "cost_bps_options": report.cost_bps_options,
        "best_threshold": report.best_threshold,
        "best_cost_bps": report.best_cost_bps,
        "backtest_grid": [asdict(bt) for bt in report.backtest_grid],
        "walk_forward": asdict(report.walk_forward) if report.walk_forward is not None else None,
        "graduation": asdict(report.graduation) if report.graduation is not None else None,
        "signal_diagnostics": (
            asdict(report.signal_diagnostics) if report.signal_diagnostics is not None else None
        ),
        "data_sufficiency": (
            asdict(report.data_sufficiency) if report.data_sufficiency is not None else None
        ),
        "effective_sample": (
            asdict(report.effective_sample) if report.effective_sample is not None else None
        ),
        "regime_coverage": report.regime_coverage,
        "joint_regime_coverage": [asdict(b) for b in report.joint_regime_coverage],
        "signal_behavior": (
            asdict(report.signal_behavior) if report.signal_behavior is not None else None
        ),
        "oos_sharpe_ci": (
            asdict(report.oos_sharpe_ci) if report.oos_sharpe_ci is not None else None
        ),
        "deflated_sharpe": (
            asdict(report.deflated_sharpe) if report.deflated_sharpe is not None else None
        ),
        "methodology": report.methodology,
        "research_log": report.research_log,
        "error": report.error,
    }
