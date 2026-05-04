"""Spec-driven strategy backtest API.

POST /api/spec-strategy/backtest    Run a backtest with an inline JSON spec.
GET  /api/spec-strategy/schema      JSON Schema export for UI form generation.
GET  /api/spec-strategy/fixtures    List canonical Phase-1 spec fixtures.
GET  /api/spec-strategy/fixtures/{name}    Return one canonical fixture.

The spec layer (``app.engine.strategy.spec``) consumes a fully-declarative
``StrategySpec`` and runs through the same ``BacktestEngine`` the
hand-coded LEAN-pinned algorithms use. This router is the HTTP entry
point — Frontend / external tooling POSTs a spec + run config and gets
back the trade log and summary statistics.

Data source is loaded by the same LEAN minute reader as
``/api/engine/backtest`` so this endpoint operates against the same
historical bar dataset; for hermetic testing the
``get_data_source_factory`` dependency is overridable.
"""

from __future__ import annotations

import json
import logging
from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import LoggedTrade
from app.engine.strategy.spec import SpecAlgorithm, StrategySpec

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — fixture directory shipped with the package.
# ---------------------------------------------------------------------------
_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "engine" / "strategy" / "spec" / "fixtures"


# ---------------------------------------------------------------------------
# Request / response models.
# ---------------------------------------------------------------------------
class SpecBacktestRequest(BaseModel):
    spec: StrategySpec = Field(..., description="Validated StrategySpec — see /schema for shape")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    initial_cash: float = Field(100000.0, ge=0)
    fill_mode: str = Field("signal_bar_close", description="signal_bar_close or next_bar_open")
    commission_per_order: float = Field(0.0, ge=0)


class SpecTradeResponse(BaseModel):
    trade_number: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    indicators: dict[str, float] = Field(default_factory=dict)
    pnl_pts: float
    pnl_pct: float
    result: str
    signal_reason: str = ""


class SpecBacktestResponse(BaseModel):
    success: bool
    strategy_name: str
    initial_cash: float
    final_equity: float
    net_profit: float
    total_fees: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    trades: list[SpecTradeResponse] = Field(default_factory=list)
    log_lines: list[str] = Field(default_factory=list)
    error: str | None = None


class FixtureListItem(BaseModel):
    name: str
    spec_name: str
    symbols: list[str]
    description: str | None = None


# ---------------------------------------------------------------------------
# Data source dependency — overridable for tests.
# ---------------------------------------------------------------------------
DataSourceFactory = Any  # callable(symbol, start, end) -> LeanMinuteDataReader-like


def _default_data_source_factory(symbol: str, start: Date, end: Date):
    """Build a real LEAN data reader for the given symbol + date range.

    Reads the LEAN_DATA_ROOT / LEAN_DATA_CACHE env vars the same way
    ``app/routers/engine.py`` does. Tests override via
    ``app.dependency_overrides[get_data_source_factory]``.
    """
    import os

    roots = []
    for env_var in ("LEAN_DATA_ROOT", "LEAN_DATA_CACHE"):
        val = os.environ.get(env_var)
        if val:
            roots.append(Path(val))
    if not roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No LEAN data roots configured (set LEAN_DATA_ROOT or LEAN_DATA_CACHE)",
        )
    return LeanMinuteDataReader(roots)


def get_data_source_factory():
    """FastAPI dependency. Returns a callable ``(symbol, start, end) -> reader``."""
    return _default_data_source_factory


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


def _parse_fill_mode(s: str) -> FillMode:
    norm = s.lower().replace("-", "_")
    if norm == "signal_bar_close":
        return FillMode.SIGNAL_BAR_CLOSE
    if norm == "next_bar_open":
        return FillMode.NEXT_BAR_OPEN
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"unknown fill_mode {s!r} — expected signal_bar_close or next_bar_open",
    )


def _trade_to_response(i: int, t: LoggedTrade) -> SpecTradeResponse:
    return SpecTradeResponse(
        trade_number=i + 1,
        entry_time=t.entry_time.isoformat(),
        entry_price=float(t.entry_price),
        exit_time=t.exit_time.isoformat(),
        exit_price=float(t.exit_price),
        indicators={k: float(v) for k, v in t.indicators.items()},
        pnl_pts=float(t.pnl_pts),
        pnl_pct=float(t.pnl_pct),
        result=t.result,
        signal_reason=t.signal_reason,
    )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------
@router.get("/schema")
def get_schema() -> dict[str, Any]:
    """Return the JSON Schema for StrategySpec.

    The Frontend uses this to generate form structure and validate specs
    inline before round-tripping to the backtest endpoint.
    """
    return StrategySpec.model_json_schema()


@router.get("/fixtures", response_model=list[FixtureListItem])
def list_fixtures() -> list[FixtureListItem]:
    """List the canonical Phase-1 spec fixtures shipped with the package."""
    if not _FIXTURES_DIR.is_dir():
        return []
    items: list[FixtureListItem] = []
    for path in sorted(_FIXTURES_DIR.glob("*.spec.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items.append(
                FixtureListItem(
                    name=path.stem.replace(".spec", ""),
                    spec_name=payload.get("name", ""),
                    symbols=payload.get("symbols", []),
                    description=payload.get("description"),
                )
            )
        except Exception:
            logger.warning("failed to parse spec fixture %s", path)
    return items


@router.get("/fixtures/{name}", response_model=StrategySpec)
def get_fixture(name: str) -> StrategySpec:
    path = _FIXTURES_DIR / f"{name}.spec.json"
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown fixture {name!r}",
        )
    try:
        return StrategySpec.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        # Should never happen for canonical fixtures — surface as 500.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"corrupt fixture {name!r}: {exc.errors()}",
        ) from exc


@router.post("/backtest", response_model=SpecBacktestResponse)
def run_spec_backtest(
    request: SpecBacktestRequest,
    data_source_factory=Depends(get_data_source_factory),
) -> SpecBacktestResponse:
    """Run a backtest from an inline ``StrategySpec``.

    The Phase-1 evaluator boundary applies: equity-only, single-symbol,
    no survival actions beyond CLOSE_ALL, no options materialization.
    A spec that uses Phase-2 features the evaluator can't run yet
    surfaces as a ``NotImplementedError`` rendered as HTTP 400.
    """
    spec = request.spec
    start_d = _parse_date(request.start_date, "start_date")
    end_d = _parse_date(request.end_date, "end_date")
    fill_mode = _parse_fill_mode(request.fill_mode)
    symbol = spec.symbols[0]

    try:
        data_source = data_source_factory(symbol, start_d, end_d)
    except HTTPException:
        raise
    except Exception as exc:
        return SpecBacktestResponse(
            success=False,
            strategy_name=spec.name,
            initial_cash=request.initial_cash,
            final_equity=0.0,
            net_profit=0.0,
            total_fees=0.0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0.0,
            error=f"data source unavailable: {exc}",
        )

    try:
        strategy = SpecAlgorithm(spec)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"spec uses unsupported feature: {exc}",
        ) from exc

    # Override the strategy's default date window with the request.
    orig_init = strategy.initialize

    def _patched_init() -> None:
        orig_init()
        strategy.set_start_date(start_d.year, start_d.month, start_d.day)
        strategy.set_end_date(end_d.year, end_d.month, end_d.day)
        strategy.set_cash(request.initial_cash)

    strategy.initialize = _patched_init  # type: ignore[assignment]

    engine = BacktestEngine(
        data_source=data_source,
        fill_model=FillModel(
            mode=fill_mode,
            commission_per_order=Decimal(str(request.commission_per_order)),
        ),
    )
    try:
        result = engine.run(strategy)
    except Exception as exc:
        logger.exception("[SPEC] backtest failed for %s", spec.name)
        return SpecBacktestResponse(
            success=False,
            strategy_name=spec.name,
            initial_cash=request.initial_cash,
            final_equity=0.0,
            net_profit=0.0,
            total_fees=0.0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0.0,
            error=f"backtest run failed: {exc}",
        )

    trades = strategy.trade_log
    winning = sum(1 for t in trades if t.result == "WIN")
    losing = sum(1 for t in trades if t.result == "LOSS")
    win_rate = (winning / len(trades)) if trades else 0.0

    return SpecBacktestResponse(
        success=True,
        strategy_name=spec.name,
        initial_cash=float(result.initial_cash),
        final_equity=float(result.final_equity),
        net_profit=float(result.net_profit),
        total_fees=float(result.total_fees),
        total_trades=len(trades),
        winning_trades=winning,
        losing_trades=losing,
        win_rate=win_rate,
        trades=[_trade_to_response(i, t) for i, t in enumerate(trades)],
        log_lines=list(result.log_lines),
    )
