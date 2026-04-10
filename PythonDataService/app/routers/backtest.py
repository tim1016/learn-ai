"""Unified backtest API — all strategies through the Python data pipeline.

Provides /api/backtest/run (JSON response with chart data) and
/api/backtest/generate-zip (ZIP download with dataset + trades).
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.polygon_client import PolygonClientService
from app.services.dataset_service import (
    fetch_bars_chunked,
    calculate_dynamic_indicators,
    estimate_max_lookback,
    compute_warmup_start_date,
    build_zip_bytes,
)
from app.services.chart_service import (
    _preprocess_minute_bars,
    _format_indicator_results,
    _resample_bars,
    QualityReport,
    TIMEFRAME_DEFS,
)
from app.services.strategies.registry import get_strategy, list_strategies
from app.services.strategies.common import StrategyResult
from app.services.strategies.lean_statistics import compute_lean_statistics, LeanStatistics

router = APIRouter()
logger = logging.getLogger(__name__)
_polygon = PolygonClientService()


class BacktestRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="Start date YYYY-MM-DD")
    to_date: str = Field(..., description="End date YYYY-MM-DD")
    timespan: str = Field("minute", description="minute, hour, day")
    multiplier: int = Field(5, ge=1, description="Bar multiplier")
    session: str = Field("rth", description="rth or extended")
    forward_fill: bool = Field(True, description="Forward-fill missing bars")
    warmup: bool = Field(True, description="Fetch extra bars for indicator warm-up")
    strategy_name: str = Field(..., description="Strategy identifier")
    parameters: dict[str, Any] = Field(default_factory=dict)


class BacktestTradeResponse(BaseModel):
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
    indicator_snapshot: dict[str, float | None] = {}


class LeanPortfolioStatsResponse(BaseModel):
    """LEAN PortfolioStatistics — 25 fields matching PS.cs exactly."""
    average_win_rate: float = 0.0
    average_loss_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    win_rate: float = 0.0
    loss_rate: float = 0.0
    expectancy: float = 0.0
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_net_profit: float = 0.0
    compounding_annual_return: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    probabilistic_sharpe_ratio: float = 0.0
    annual_standard_deviation: float = 0.0
    annual_variance: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    information_ratio: float = 0.0
    tracking_error: float = 0.0
    treynor_ratio: float = 0.0
    drawdown: float = 0.0
    drawdown_recovery: int = 0
    value_at_risk_99: float = 0.0
    value_at_risk_95: float = 0.0
    portfolio_turnover: float = 0.0


class LeanTradeStatsResponse(BaseModel):
    """LEAN TradeStatistics — key fields matching TS.cs."""
    start_date_time: str = ""
    end_date_time: str = ""
    total_number_of_trades: int = 0
    number_of_winning_trades: int = 0
    number_of_losing_trades: int = 0
    total_profit_loss: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    largest_profit: float = 0.0
    largest_loss: float = 0.0
    average_profit_loss: float = 0.0
    average_profit: float = 0.0
    average_loss: float = 0.0
    average_trade_duration: str = ""
    average_winning_trade_duration: str = ""
    average_losing_trade_duration: str = ""
    max_consecutive_winning_trades: int = 0
    max_consecutive_losing_trades: int = 0
    profit_factor: float = 0.0
    profit_to_max_drawdown_ratio: float = 0.0
    profit_loss_standard_deviation: float = 0.0
    profit_loss_downside_deviation: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    total_fees: float = 0.0


class LeanRuntimeStatsResponse(BaseModel):
    """LEAN runtimeStatistics — 5 key fields."""
    equity: float = 0.0
    fees: float = 0.0
    net_profit: float = 0.0
    total_return: float = 0.0
    total_orders: int = 0


class LeanStatisticsResponse(BaseModel):
    """Full LEAN statistics suite."""
    portfolio: LeanPortfolioStatsResponse = Field(default_factory=LeanPortfolioStatsResponse)
    trade: LeanTradeStatsResponse = Field(default_factory=LeanTradeStatsResponse)
    runtime: LeanRuntimeStatsResponse = Field(default_factory=LeanRuntimeStatsResponse)


class BacktestResponse(BaseModel):
    success: bool
    ticker: str
    strategy_name: str
    parameters: dict[str, Any]
    # Performance metrics (kept for backward compat)
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
    total_pnl_pts: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    # LEAN-compatible statistics (all 27 + 8 KPIs)
    lean_statistics: LeanStatisticsResponse | None = None
    # Pipeline info
    source_bars: int = 0
    rth_bars: int = 0
    resampled_bars: int = 0
    bars_processed: int = 0
    timeframe: str = ""
    # Chart data
    chart_bars: list[dict] = []
    chart_indicators: list[dict] = []
    quality: dict | None = None
    # Trades
    trades: list[BacktestTradeResponse] = []
    error: str | None = None


def _run_backtest_pipeline(request: BacktestRequest) -> BacktestResponse:
    """Core pipeline: fetch bars → preprocess → compute indicators → run strategy → format chart data."""
    ticker = request.ticker.upper()
    strategy_def = get_strategy(request.strategy_name)

    # Build timeframe label
    if request.timespan == "hour":
        timeframe = f"{request.multiplier}h" if request.multiplier > 1 else "1h"
    elif request.timespan == "day":
        timeframe = "1D"
    else:
        minutes = request.multiplier
        if minutes >= 60:
            timeframe = f"{minutes // 60}h"
        elif minutes > 1:
            timeframe = f"{minutes}m"
        else:
            timeframe = "1m"

    # Get indicator entries from strategy definition + user params
    indicator_entries = strategy_def.get_indicator_entries(request.parameters)

    # Determine fetch range (with optional warmup)
    fetch_from = request.from_date
    if request.warmup and indicator_entries:
        max_lookback = estimate_max_lookback(indicator_entries)
        fetch_from = compute_warmup_start_date(request.from_date, max_lookback)
        logger.info("[BACKTEST] Warmup: fetching from %s (requested %s)", fetch_from, request.from_date)

    # Fetch 1m bars from Polygon
    raw_bars = fetch_bars_chunked(_polygon, ticker, fetch_from, request.to_date)
    source_bar_count = len(raw_bars)

    if not raw_bars:
        return BacktestResponse(
            success=False, ticker=ticker,
            strategy_name=request.strategy_name,
            parameters=request.parameters,
            error="No bars returned from Polygon",
        )

    # Preprocess: session filter, forward-fill, quality checks
    df, quality = _preprocess_minute_bars(
        raw_bars, request.from_date, request.to_date,
        request.session, request.forward_fill,
    )
    rth_bar_count = len(df)

    if df.empty or len(df) < 10:
        return BacktestResponse(
            success=False, ticker=ticker,
            strategy_name=request.strategy_name,
            parameters=request.parameters,
            source_bars=source_bar_count,
            error=f"Only {len(df)} bars after preprocessing — need at least 10",
        )

    # Resample to target timeframe
    if timeframe in TIMEFRAME_DEFS and timeframe != "1m":
        df = _resample_bars(df, timeframe, request.session)

    resampled_bar_count = len(df)
    quality.resampled_bar_count = resampled_bar_count

    # Compute strategy-relevant indicators for chart display
    df_with_indicators, column_meta = calculate_dynamic_indicators(df.copy(), indicator_entries)

    # Trim warmup bars (keep only from requested start date forward)
    if request.warmup and fetch_from != request.from_date:
        from datetime import datetime
        trim_ts = int(datetime.strptime(request.from_date, "%Y-%m-%d").timestamp() * 1000)
        df = df[df["timestamp"] >= trim_ts].reset_index(drop=True)
        df_with_indicators = df_with_indicators[df_with_indicators["timestamp"] >= trim_ts].reset_index(drop=True)

    # Run the strategy
    # Pass ticker in params for ema_crossover_rsi wrapper
    run_params = {**request.parameters, "_ticker": ticker}
    strategy_result: StrategyResult = strategy_def.run_fn(df.copy(), run_params)

    if not strategy_result.success:
        return BacktestResponse(
            success=False, ticker=ticker,
            strategy_name=request.strategy_name,
            parameters=request.parameters,
            source_bars=source_bar_count,
            rth_bars=rth_bar_count,
            resampled_bars=resampled_bar_count,
            error=strategy_result.error,
        )

    # Compute LEAN-compatible statistics from the full equity curve
    lean_stats = compute_lean_statistics(
        df=df.copy(),
        trades=strategy_result.trades,
        start_capital=100_000.0,
        risk_free_rate=0.0,
        benchmark_returns=None,  # zero benchmark like SetBenchmark(d => 0m)
    )

    # Format chart bars
    chart_bars = []
    for _, row in df_with_indicators.iterrows():
        bar = {
            "t": int(row["timestamp"]),
            "o": round(float(row["open"]), 6),
            "h": round(float(row["high"]), 6),
            "l": round(float(row["low"]), 6),
            "c": round(float(row["close"]), 6),
            "v": int(row.get("volume", 0)),
        }
        if "session" in row:
            bar["session"] = row["session"]
        chart_bars.append(bar)

    # Format indicator results for chart rendering
    chart_indicators = _format_indicator_results(
        df_with_indicators, column_meta, indicator_entries,
    )

    # Format trades
    trade_responses = [
        BacktestTradeResponse(
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
            indicator_snapshot=t.indicator_snapshot,
        )
        for t in strategy_result.trades
    ]

    # Serialize quality report
    quality_dict = asdict(quality)
    # Convert GapDetail dataclasses to dicts
    quality_dict["gap_details"] = [asdict(g) for g in quality.gap_details]

    # Build LEAN statistics response
    from dataclasses import asdict as _dc_asdict
    lean_port = lean_stats.portfolio
    lean_trade = lean_stats.trade
    lean_stats_resp = LeanStatisticsResponse(
        portfolio=LeanPortfolioStatsResponse(**_dc_asdict(lean_port)),
        trade=LeanTradeStatsResponse(**_dc_asdict(lean_trade)),
        runtime=LeanRuntimeStatsResponse(
            equity=lean_stats.equity,
            fees=lean_stats.fees,
            net_profit=lean_stats.net_profit,
            total_return=lean_stats.total_return,
            total_orders=lean_stats.total_orders,
        ),
    )

    return BacktestResponse(
        success=True,
        ticker=ticker,
        strategy_name=request.strategy_name,
        parameters=request.parameters,
        total_trades=strategy_result.total_trades,
        winning_trades=strategy_result.winning_trades,
        losing_trades=strategy_result.losing_trades,
        win_rate=strategy_result.win_rate,
        avg_win_pct=strategy_result.avg_win_pct,
        avg_loss_pct=strategy_result.avg_loss_pct,
        win_loss_ratio=strategy_result.win_loss_ratio,
        profit_factor=strategy_result.profit_factor,
        expectancy_per_trade=strategy_result.expectancy_per_trade,
        total_pnl_pct=strategy_result.total_pnl_pct,
        total_pnl_pts=strategy_result.total_pnl_pts,
        max_drawdown_pct=strategy_result.max_drawdown_pct,
        sharpe_ratio=strategy_result.sharpe_ratio,
        lean_statistics=lean_stats_resp,
        source_bars=source_bar_count,
        rth_bars=rth_bar_count,
        resampled_bars=resampled_bar_count,
        bars_processed=strategy_result.bars_processed,
        timeframe=timeframe,
        chart_bars=chart_bars,
        chart_indicators=chart_indicators,
        quality=quality_dict,
        trades=trade_responses,
    )


@router.post("/run", response_model=BacktestResponse)
async def run_backtest(request: BacktestRequest):
    """Run a backtest using the full Python data pipeline."""
    try:
        logger.info(
            "[BACKTEST] %s strategy=%s %s×%s from %s to %s",
            request.ticker, request.strategy_name,
            request.multiplier, request.timespan,
            request.from_date, request.to_date,
        )
        return _run_backtest_pipeline(request)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error("[BACKTEST] Error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backtest failed: {str(e)}",
        )


@router.post("/generate-zip")
async def generate_backtest_zip(request: BacktestRequest):
    """Run a backtest and return a ZIP with dataset.csv, metadata.csv, columns.csv, trades.csv."""
    import io
    import csv

    try:
        logger.info(
            "[BACKTEST ZIP] %s strategy=%s %s×%s from %s to %s",
            request.ticker, request.strategy_name,
            request.multiplier, request.timespan,
            request.from_date, request.to_date,
        )
        result = _run_backtest_pipeline(request)

        if not result.success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.error or "Backtest failed",
            )

        # Build trades CSV
        trades_buf = io.StringIO()
        writer = csv.writer(trades_buf)
        writer.writerow([
            "trade_number", "trade_type", "entry_timestamp", "exit_timestamp",
            "entry_price", "exit_price", "pnl", "pnl_pct",
            "cumulative_pnl_pct", "signal_reason",
        ])
        for t in result.trades:
            writer.writerow([
                t.trade_number, t.trade_type, t.entry_timestamp, t.exit_timestamp,
                f"{t.entry_price:.6f}", f"{t.exit_price:.6f}",
                f"{t.pnl:.6f}", f"{t.pnl_pct:.6f}",
                f"{t.cumulative_pnl_pct:.6f}", t.signal_reason,
            ])
        trades_csv_bytes = trades_buf.getvalue().encode("utf-8")

        # Build DataFrame from chart_bars for dataset.csv
        import pandas as pd
        bars_df = pd.DataFrame(result.chart_bars)
        bars_df.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
        data_cols = ["open", "high", "low", "close", "volume"]
        if "session" in bars_df.columns:
            data_cols.append("session")

        zip_bytes = build_zip_bytes(
            df=bars_df,
            columns=data_cols,
            column_meta=[],
            ohlcv_cols=data_cols,
            ticker=result.ticker,
            from_date=request.from_date,
            to_date=request.to_date,
            session=request.session,
            forward_fill=request.forward_fill,
            timespan=request.timespan,
            multiplier=request.multiplier,
            raw_bar_count=result.source_bars,
            filled_bar_count=result.resampled_bars,
            trades_csv_bytes=trades_csv_bytes,
        )

        session_label = "rth" if request.session == "rth" else "ext"
        filename = (
            f"{result.ticker}_{request.strategy_name}_{result.timeframe}_"
            f"{session_label}_{request.from_date}_to_{request.to_date}.zip"
        )

        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("[BACKTEST ZIP] Error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backtest ZIP failed: {str(e)}",
        )


@router.get("/strategies")
async def get_strategies():
    """Return available strategy names."""
    return {"strategies": list_strategies()}
