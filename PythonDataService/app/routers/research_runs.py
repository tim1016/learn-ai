"""Research-run HTTP boundary.

Three endpoints under ``/api/research/strategy-runs``:

  * ``POST   /``                — kick off a strategy run, persist the
    ledger + result, return both. Synchronous: blocks until the engine
    finishes. For long runs, future work moves this to the existing
    job orchestration in ``app/routers/jobs.py``.
  * ``GET    /{run_id}``        — load a previously-persisted run.
  * ``GET    /``                — list ledgers, with filters and limit.

GraphQL passthrough is intentionally not wired here. Phase B (research
workbench) will decide whether to add one based on whether the UI is
GraphQL-only or willing to call FastAPI directly. See
``docs/architecture/build-alpha-style-features-1-8-research-spec.md``.

The data-source dependency mirrors ``app/routers/spec_strategy.py``:
production injects a real ``LeanMinuteDataReader``; tests override via
``app.dependency_overrides[get_data_source_factory]``.

Failed runs are *first-class research records*. A run that hits an
unsupported spec feature, missing data, or any boundary failure is
persisted with ``status='failed'`` and surfaced as a normal 200
response — clients introspect ``ledger.status`` and
``ledger.failure_reason``. This is a deliberate departure from
``spec_strategy.py``'s 400-on-NotImplementedError behavior; the
research pipeline cares about discoverable failures across many runs.

**Sync handlers, not async — deliberate**: the router uses ``def`` rather
than ``async def`` because every operation here (engine run, file
I/O via ``save_run`` / ``load_run`` / ``list_runs``) is fully blocking.
FastAPI executes ``def`` handlers in a threadpool, which keeps the
event loop responsive under concurrent requests. Converting to
``async def`` without wrapping each call in ``run_in_executor`` (or
adopting ``aiofiles``) would actively *block* the loop and degrade
throughput, so the threadpool path is the right one for Phase A. If
the runner ever grows real async I/O (Phase D's MC could parallelise
folds), revisit per-handler. See ``.claude/rules/python.md`` § FastAPI
for the project's general async-by-default rule.
"""

from __future__ import annotations

import logging
import os
from datetime import date as Date
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.engine.strategy.spec import StrategySpec
from app.research.runs import (
    BacktestRunResult,
    RunAlreadyExistsError,
    RunCorruptError,
    RunLedger,
    RunNotFoundError,
    RunRequest,
    list_runs,
    load_run,
    run_strategy_spec,
    save_run,
)
from app.routers.spec_strategy import (
    _default_data_source_factory,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models.
# ---------------------------------------------------------------------------
class StrategyRunRequest(BaseModel):
    """Inputs for ``POST /api/research/strategy-runs``.

    Pydantic validates the embedded ``StrategySpec`` before the
    endpoint body runs; a malformed spec returns 422 from FastAPI's
    own validation layer without ever reaching the runner.
    """

    spec: StrategySpec = Field(..., description="Validated StrategySpec — see /api/spec-strategy/schema")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    initial_cash: float = Field(100_000.0, ge=0)
    fill_mode: str = Field("signal_bar_close", description="signal_bar_close or next_bar_open")
    commission_per_order: float = Field(0.0, ge=0)
    slippage_per_share: float = Field(
        0.0,
        ge=0,
        description=(
            "Per-share fill-price slippage in price points (Decimal-compatible). "
            "Applied against the trade direction by FillModel — long fills "
            "pay slippage above the bar price; short fills pay below."
        ),
    )
    random_seed: int = 0
    strategy_spec_id: str = Field("", description="Caller label; defaults to spec.name when empty")
    parent_run_id: str | None = Field(None, description="Set on fold/MC/sweep child runs")
    parent_spec_hash: str | None = Field(None, description="Set on sensitivity-grid child runs")


class StrategyRunResponse(BaseModel):
    """Response payload for a single run (POST and single-run GET)."""

    ledger: RunLedger
    result: BacktestRunResult


class StrategyRunListResponse(BaseModel):
    """Response payload for listing — ledgers only, no results.

    Keeps the listing payload small. Clients fetch full results
    on-demand via ``GET /{run_id}``.
    """

    runs: list[RunLedger]


# ---------------------------------------------------------------------------
# Dependencies.
# ---------------------------------------------------------------------------
def get_data_source_factory():
    """Return a ``(symbol, start, end) -> reader`` factory.

    Mirrors the dependency in ``app/routers/spec_strategy.py``. Tests
    override via ``app.dependency_overrides`` to inject a synthetic
    data source — same pattern the spec parity tests already use.
    """
    return _default_data_source_factory


def get_artifacts_root() -> Path | None:
    """Return the artifacts root for persistence, or ``None`` to use
    the storage layer's default (env-or-package).

    Returning ``None`` lets ``save_run`` / ``load_run`` / ``list_runs``
    fall through to ``default_artifacts_root()``. Tests override this
    dependency to inject a ``tmp_path`` so each test runs in its own
    artifact tree without touching the real ``artifacts/`` directory.
    """
    explicit = os.environ.get("LEARN_AI_ARTIFACTS_ROOT")
    if explicit:
        return Path(explicit)
    return None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _parse_date(s: str, field: str) -> Date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be YYYY-MM-DD: {s!r}",
        ) from exc


def _validate_fill_mode(s: str) -> None:
    norm = s.lower().replace("-", "_")
    if norm not in {"signal_bar_close", "next_bar_open"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown fill_mode {s!r} — expected signal_bar_close or next_bar_open",
        )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------
@router.post("", response_model=StrategyRunResponse)
def create_run(
    request: StrategyRunRequest,
    data_source_factory=Depends(get_data_source_factory),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> StrategyRunResponse:
    """Run a strategy spec, persist the artifacts, return ``(ledger, result)``.

    Failed runs are persisted alongside successful ones. The runner
    populates ``ledger.status`` and ``ledger.failure_reason`` so the
    client can distinguish without an out-of-band channel.
    """
    start_d = _parse_date(request.start_date, "start_date")
    end_d = _parse_date(request.end_date, "end_date")
    _validate_fill_mode(request.fill_mode)
    if start_d >= end_d:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"start_date must be strictly before end_date "
            f"(got start={start_d.isoformat()}, end={end_d.isoformat()})",
        )

    run_request = RunRequest(
        spec=request.spec,
        start_date=start_d,
        end_date=end_d,
        initial_cash=request.initial_cash,
        fill_mode=request.fill_mode,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        strategy_spec_id=request.strategy_spec_id,
        parent_run_id=request.parent_run_id,
        parent_spec_hash=request.parent_spec_hash,
    )

    ledger, result = run_strategy_spec(
        run_request,
        data_source_factory=data_source_factory,
    )

    try:
        save_run(ledger, result, root=artifacts_root)
    except RunAlreadyExistsError as exc:
        # UUID4 collision (~10⁻³⁷ for a few thousand runs) — paranoid
        # but the contract is explicit. 409 Conflict is the right shape
        # because the resource literally exists.
        logger.exception("[RUNS] run_id collision: %s", ledger.run_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run_id collision: {ledger.run_id}",
        ) from exc
    except ValueError as exc:
        # ``_run_dir`` rejects malformed run_ids. The runner generates
        # UUID4 hex which always passes, so this branch is defensive
        # against future callers that thread a custom ``run_id`` in.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # Genuine persistence failure (disk full, EROFS, permission).
        # The run completed; we couldn't record it. 500 is right —
        # silently dropping would lie about durability.
        logger.exception(
            "[RUNS] failed to persist run_id=%s spec=%s",
            ledger.run_id,
            ledger.strategy_spec_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"run completed but persistence failed: {exc}",
        ) from exc

    return StrategyRunResponse(ledger=ledger, result=result)


@router.get("/{run_id}", response_model=StrategyRunResponse)
def get_run(
    run_id: str,
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> StrategyRunResponse:
    """Load a previously-persisted run by ``run_id``.

    A malformed ``run_id`` (path traversal attempt, wrong character set)
    is rejected by the storage layer's ``_run_dir`` regex and surfaces
    here as a ``ValueError`` — translated to 400 to keep the surface
    actionable.
    """
    try:
        ledger, result = load_run(run_id, root=artifacts_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run not found: {run_id}",
        ) from exc
    except RunCorruptError as exc:
        logger.exception("[RUNS] corrupt artifact for run_id=%s", run_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"run artifact is corrupt: {exc}",
        ) from exc
    return StrategyRunResponse(ledger=ledger, result=result)


@router.get("", response_model=StrategyRunListResponse)
def list_runs_endpoint(
    spec_hash: str | None = Query(None, description="Filter by ``strategy_spec_hash``"),
    symbol: str | None = Query(None, description="Filter by traded symbol"),
    run_status: str | None = Query(
        None,
        alias="status",
        description="Filter by lifecycle status: running | completed | failed",
    ),
    parent_run_id: str | None = Query(None, description="Filter by lineage — fold/MC/sweep parent"),
    parent_spec_hash: str | None = Query(None, description="Filter by sensitivity-grid parent"),
    since_ms: int | None = Query(None, ge=0, description="Only return runs created at or after this ``int64 ms UTC``"),
    limit: int | None = Query(None, ge=1, description="Cap result count after sorting newest-first"),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> StrategyRunListResponse:
    """List persisted runs, optionally filtered, newest first."""
    runs = list_runs(
        root=artifacts_root,
        spec_hash=spec_hash,
        symbol=symbol,
        status=run_status,
        parent_run_id=parent_run_id,
        parent_spec_hash=parent_spec_hash,
        since_ms=since_ms,
        limit=limit,
    )
    return StrategyRunListResponse(runs=runs)


