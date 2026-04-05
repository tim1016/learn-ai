"""API endpoint for rule-based configurable backtesting."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.polygon_client import PolygonClientService
from app.services.rule_based_backtest import run_rule_based_backtest

router = APIRouter()
logger = logging.getLogger(__name__)
polygon_client = PolygonClientService()


class RuleBasedBacktestRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date YYYY-MM-DD")
    to_date: str = Field(..., description="End date YYYY-MM-DD")
    multiplier: int = Field(15, ge=1, description="Bar multiplier")
    timespan: str = Field("minute", description="minute, hour, day")
    filter_rth: bool = Field(True, description="Filter to Regular Trading Hours")
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "fast_ema_period": 5,
            "slow_ema_period": 10,
            "rsi_period": 14,
            "adx_period": 14,
            "min_ema_gap": 0.20,
            "rsi_min": 50,
            "rsi_max": 70,
            "exit_mode": "fixed_bars",
            "exit_bars": 5,
            "direction": "long",
        }
    )


class RuleBasedTradeResponse(BaseModel):
    trade_number: int
    trade_type: str
    entry_timestamp: str
    exit_timestamp: str
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    cumulative_pnl_pct: float
    signal_reason: str
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_gap: float | None = None
    rsi: float | None = None
    adx: float | None = None


class RuleBasedBacktestResponse(BaseModel):
    success: bool
    ticker: str
    strategy_name: str
    parameters: dict[str, Any]
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    win_loss_ratio: float = 0.0
    profit_factor: float = 0.0
    expectancy_per_trade: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    total_pnl_pts: float = 0.0
    sharpe_ratio: float = 0.0
    bars_processed: int = 0
    trades: list[RuleBasedTradeResponse] = []
    error: str | None = None


RTH_OPEN_MINUTES = 9 * 60 + 30   # 9:30 AM
RTH_CLOSE_MINUTES = 16 * 60       # 4:00 PM


def _filter_rth(bars: list[dict]) -> list[dict]:
    """Filter bars to Regular Trading Hours (9:30 AM - 4:00 PM ET).
    Polygon timestamps are in ms UTC; convert to ET for filtering."""
    import pytz
    eastern = pytz.timezone("America/New_York")
    filtered = []
    for bar in bars:
        ts_ms = bar.get("t") or bar.get("timestamp")
        if ts_ms is None:
            continue
        dt_utc = datetime.utcfromtimestamp(ts_ms / 1000).replace(tzinfo=pytz.utc)
        dt_et = dt_utc.astimezone(eastern)
        minutes = dt_et.hour * 60 + dt_et.minute
        if RTH_OPEN_MINUTES <= minutes < RTH_CLOSE_MINUTES:
            filtered.append(bar)
    return filtered


@router.post("/run", response_model=RuleBasedBacktestResponse)
async def run_backtest(request: RuleBasedBacktestRequest):
    """Run a rule-based backtest using Polygon data."""
    try:
        logger.info(
            "[RuleBasedBacktest] %s %s×%s from %s to %s, params=%s",
            request.ticker, request.multiplier, request.timespan,
            request.from_date, request.to_date, request.parameters,
        )

        raw_bars = polygon_client.fetch_aggregates(
            ticker=request.ticker.upper(),
            multiplier=request.multiplier,
            timespan=request.timespan,
            from_date=request.from_date,
            to_date=request.to_date,
        )

        if not raw_bars:
            return RuleBasedBacktestResponse(
                success=False,
                ticker=request.ticker,
                strategy_name=request.parameters.get("strategy_name", "ema_crossover_rsi"),
                parameters=request.parameters,
                error="No bars returned from Polygon",
            )

        # Normalize Polygon response to {timestamp, open, high, low, close, volume}
        bars = _normalize_bars(raw_bars)

        if request.filter_rth:
            bars = _filter_rth(bars)
            logger.info("[RuleBasedBacktest] RTH filter: %d bars remaining", len(bars))

        if len(bars) < 50:
            return RuleBasedBacktestResponse(
                success=False,
                ticker=request.ticker,
                strategy_name=request.parameters.get("strategy_name", "ema_crossover_rsi"),
                parameters=request.parameters,
                error=f"Only {len(bars)} bars after filtering — need at least 50",
            )

        result = run_rule_based_backtest(
            ticker=request.ticker.upper(),
            bars=bars,
            params=request.parameters,
        )

        trade_responses = [
            RuleBasedTradeResponse(
                trade_number=t.trade_number,
                trade_type=t.trade_type,
                entry_timestamp=t.entry_timestamp,
                exit_timestamp=t.exit_timestamp,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                pnl=t.pnl,
                pnl_pct=t.pnl_pct,
                cumulative_pnl_pct=t.cumulative_pnl_pct,
                signal_reason=t.signal_reason,
                ema_fast=t.ema_fast,
                ema_slow=t.ema_slow,
                ema_gap=t.ema_gap,
                rsi=t.rsi,
                adx=t.adx,
            )
            for t in result.trades
        ]

        logger.info(
            "[RuleBasedBacktest] Done: %d trades, win_rate=%.1f%%, total_pnl=%.4f%%",
            result.total_trades, result.win_rate * 100, result.total_pnl_pct * 100,
        )

        return RuleBasedBacktestResponse(
            success=result.success,
            ticker=result.ticker,
            strategy_name=result.strategy_name,
            parameters=result.parameters,
            total_trades=result.total_trades,
            winning_trades=result.winning_trades,
            losing_trades=result.losing_trades,
            win_rate=result.win_rate,
            avg_win_pct=result.avg_win_pct,
            avg_loss_pct=result.avg_loss_pct,
            win_loss_ratio=result.win_loss_ratio,
            profit_factor=result.profit_factor,
            expectancy_per_trade=result.expectancy_per_trade,
            total_pnl_pct=result.total_pnl_pct,
            max_drawdown_pct=result.max_drawdown_pct,
            total_pnl_pts=result.total_pnl_pts,
            sharpe_ratio=result.sharpe_ratio,
            bars_processed=result.bars_processed,
            trades=trade_responses,
            error=result.error,
        )

    except Exception as e:
        logger.error("[RuleBasedBacktest] Error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rule-based backtest failed: {str(e)}",
        )


def _normalize_bars(raw_bars: list[dict]) -> list[dict]:
    """Normalize Polygon SDK response to standard OHLCV dict format."""
    normalized = []
    for bar in raw_bars:
        if isinstance(bar, dict):
            ts = bar.get("t") or bar.get("timestamp")
            normalized.append({
                "timestamp": ts,
                "open": bar.get("o") or bar.get("open"),
                "high": bar.get("h") or bar.get("high"),
                "low": bar.get("l") or bar.get("low"),
                "close": bar.get("c") or bar.get("close"),
                "volume": bar.get("v") or bar.get("volume", 0),
            })
        else:
            # Polygon SDK objects
            normalized.append({
                "timestamp": getattr(bar, "timestamp", None) or getattr(bar, "t", None),
                "open": getattr(bar, "open", None) or getattr(bar, "o", None),
                "high": getattr(bar, "high", None) or getattr(bar, "h", None),
                "low": getattr(bar, "low", None) or getattr(bar, "l", None),
                "close": getattr(bar, "close", None) or getattr(bar, "c", None),
                "volume": getattr(bar, "volume", None) or getattr(bar, "v", 0),
            })
    return normalized
