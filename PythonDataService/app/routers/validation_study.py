"""Validation Study API — upload minute CSV, run strategy, compare against reference trades.

Endpoints:
- POST /run        → JSON with backtest result + trade comparison + chart data
- POST /export-csv → ZIP with CSVs (bars, trades, comparison, columns, metadata)
- POST /report     → Markdown validation report
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.chart_service import (
    _format_indicator_results,
    _preprocess_minute_bars,
    _resample_bars,
)
from app.services.dataset_service import calculate_dynamic_indicators
from app.services.strategies.registry import get_strategy
from app.services.trade_comparison import TradeComparison, match_trades

router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_PARAMS: dict[str, Any] = {
    "strategy_name": "ema_crossover_rsi",
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

COLUMN_DESCRIPTIONS = [
    {"name": "timestamp", "description": "Bar open time (Unix ms UTC)", "type": "int"},
    {"name": "open", "description": "Opening price", "type": "float"},
    {"name": "high", "description": "Highest price in bar", "type": "float"},
    {"name": "low", "description": "Lowest price in bar", "type": "float"},
    {"name": "close", "description": "Closing price", "type": "float"},
    {"name": "volume", "description": "Total shares traded", "type": "int"},
    {"name": "ema_fast", "description": "Exponential Moving Average (fast period)", "type": "float"},
    {"name": "ema_slow", "description": "Exponential Moving Average (slow period)", "type": "float"},
    {"name": "ema_gap", "description": "EMA fast minus EMA slow", "type": "float"},
    {"name": "rsi", "description": "Relative Strength Index (14-period)", "type": "float"},
    {"name": "adx", "description": "Average Directional Index (14-period)", "type": "float"},
]


class ValidationTradeResponse(BaseModel):
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


class ReferenceSummary(BaseModel):
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float


class TradeComparisonResponse(BaseModel):
    trade_num: int
    ref_entry_time: str | None
    our_entry_time: str | None
    ref_exit_time: str | None
    our_exit_time: str | None
    ref_entry_price: float | None
    our_entry_price: float | None
    ref_exit_price: float | None
    our_exit_price: float | None
    ref_pnl: float | None
    our_pnl: float | None
    ref_pnl_pct: float | None
    our_pnl_pct: float | None
    entry_price_delta: float | None
    exit_price_delta: float | None
    pnl_delta: float | None
    pnl_pct_delta: float | None
    timestamp_delta_s: float | None
    matched: bool
    source: str


class MatchStatsResponse(BaseModel):
    total_ref: int
    total_ours: int
    matched_count: int
    extra_ref: int
    extra_ours: int
    match_rate: float
    avg_ts_delta_s: float
    avg_entry_price_delta: float
    avg_pnl_delta: float


class ColumnDescription(BaseModel):
    name: str
    description: str
    type: str


class ValidationStudyResponse(BaseModel):
    success: bool
    # Our backtest result
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
    bars_processed: int = 0
    trades: list[ValidationTradeResponse] = []
    # Reference
    reference_summary: ReferenceSummary | None = None
    # Comparison
    comparisons: list[TradeComparisonResponse] = []
    match_stats: MatchStatsResponse | None = None
    # Pipeline info
    source_bars: int = 0
    rth_bars: int = 0
    resampled_bars: int = 0
    # Chart data
    chart_bars: list[dict] = []
    chart_indicators: list[dict] = []
    # CSV visualizer data (15-min bars with indicators)
    bar_table: list[dict] = []
    column_descriptions: list[ColumnDescription] = []
    # Parameters used
    parameters: dict[str, Any] = {}
    error: str | None = None


def _parse_minute_csv(content: bytes) -> list[dict]:
    """Parse the uploaded minute CSV into a list of bar dicts with 'timestamp' in ms."""
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    bars: list[dict] = []
    field_names = reader.fieldnames or []
    logger.info("[VALIDATION] CSV columns: %s", field_names[:10])
    for row in reader:
        try:
            if "unix_ts" in row:
                ts = int(row["unix_ts"])
            elif "timestamp" in row:
                val = row["timestamp"]
                ts = int(val) if float(val) > 1e9 else int(float(val) * 1000)
            elif "iso_time" in row:
                dt = datetime.strptime(row["iso_time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
                ts = int(dt.timestamp() * 1000)
            elif "time" in row:
                time_str = row["time"].strip()
                dt = (
                    pd.Timestamp(time_str).tz_convert("UTC")
                    if pd.Timestamp(time_str).tzinfo
                    else pd.Timestamp(time_str, tz="UTC")
                )
                ts = int(dt.timestamp() * 1000)
            else:
                continue

            # Handle case-insensitive column names
            row_lower = {k.lower(): v for k, v in row.items()}
            bars.append(
                {
                    "timestamp": ts,
                    "open": float(row_lower["open"]),
                    "high": float(row_lower["high"]),
                    "low": float(row_lower["low"]),
                    "close": float(row_lower["close"]),
                    "volume": float(row_lower.get("volume", 0)),
                }
            )
        except (ValueError, KeyError):
            continue

    return bars


def _compute_reference_summary(ref_trades: list[dict]) -> ReferenceSummary:
    """Compute summary metrics from reference trade list."""
    wins = [t for t in ref_trades if t.get("pnl", 0) > 0]
    losses = [t for t in ref_trades if t.get("pnl", 0) <= 0]
    total = len(ref_trades)

    avg_win = sum(t.get("pnl_pct", 0) for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.get("pnl_pct", 0) for t in losses) / len(losses) if losses else 0.0
    total_win = sum(t.get("pnl_pct", 0) for t in wins)
    total_loss_abs = abs(sum(t.get("pnl_pct", 0) for t in losses))
    pf = total_win / total_loss_abs if total_loss_abs > 0 else 0.0

    cum_pnl = sum(t.get("pnl_pct", 0) for t in ref_trades)

    return ReferenceSummary(
        total_trades=total,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=len(wins) / total if total else 0.0,
        total_pnl_pct=cum_pnl,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        profit_factor=pf,
    )


def _run_validation_pipeline(
    csv_content: bytes,
    ref_trades: list[dict],
    params: dict[str, Any],
) -> ValidationStudyResponse:
    """Core pipeline: parse CSV → preprocess → resample → run strategy → compare."""
    merged_params = {**DEFAULT_PARAMS, **params}

    # 1. Parse minute CSV
    raw_bars = _parse_minute_csv(csv_content)
    source_count = len(raw_bars)
    if not raw_bars:
        return ValidationStudyResponse(success=False, error="No bars parsed from CSV")

    # Determine date range from data
    timestamps = [b["timestamp"] for b in raw_bars]
    from_date = datetime.fromtimestamp(min(timestamps) / 1000, tz=UTC).strftime("%Y-%m-%d")
    to_date = datetime.fromtimestamp(max(timestamps) / 1000, tz=UTC).strftime("%Y-%m-%d")

    # 2. Preprocess: RTH filter, forward-fill
    df, _quality = _preprocess_minute_bars(raw_bars, from_date, to_date, "rth", True)
    rth_count = len(df)

    if df.empty or len(df) < 50:
        return ValidationStudyResponse(
            success=False,
            source_bars=source_count,
            error=f"Only {len(df)} bars after RTH filter — need at least 50",
        )

    # 3. Resample to 15-min
    df_15m = _resample_bars(df, "15m", "rth")
    resampled_count = len(df_15m)

    # 4. Get strategy and compute chart indicators
    strategy_def = get_strategy("ema_crossover_rsi")
    indicator_entries = strategy_def.get_indicator_entries(merged_params)
    df_with_ind, column_meta = calculate_dynamic_indicators(df_15m.copy(), indicator_entries)

    # 5. Run strategy
    run_params = {**merged_params, "_ticker": "SPY"}
    strategy_result = strategy_def.run_fn(df_15m.copy(), run_params)

    if not strategy_result.success:
        return ValidationStudyResponse(
            success=False,
            source_bars=source_count,
            rth_bars=rth_count,
            resampled_bars=resampled_count,
            error=strategy_result.error,
        )

    # 6. Match trades
    our_trade_dicts = [
        {
            "entry_timestamp": t.entry_timestamp,
            "exit_timestamp": t.exit_timestamp,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "pnl": t.pnl,
            "pnl_pct": t.pnl_pct,
        }
        for t in strategy_result.trades
    ]
    comparisons, match_stats = match_trades(our_trade_dicts, ref_trades)

    # 7. Format chart bars
    chart_bars = []
    for _, row in df_with_ind.iterrows():
        bar: dict[str, Any] = {
            "t": int(row["timestamp"]),
            "o": round(float(row["open"]), 2),
            "h": round(float(row["high"]), 2),
            "l": round(float(row["low"]), 2),
            "c": round(float(row["close"]), 2),
            "v": int(row.get("volume", 0)),
        }
        if "session" in row:
            bar["session"] = row["session"]
        chart_bars.append(bar)

    # 8. Format indicators for chart
    chart_indicators = _format_indicator_results(df_with_ind, column_meta, indicator_entries)

    # 9. Build bar table for CSV visualizer (15-min data with key indicators)
    bar_table = []
    for _, row in df_with_ind.iterrows():
        entry: dict[str, Any] = {
            "timestamp": int(row["timestamp"]),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": int(row.get("volume", 0)),
        }
        # Add indicator columns if present
        for _col in ["ema_fast", "ema_slow", "ema_gap", "rsi", "adx"]:
            # The rule-based engine computes these internally; check df_with_ind for pandas-ta columns
            pass

        # Look for indicator columns from calculate_dynamic_indicators
        for meta in column_meta:
            col_name = meta.get("column", "")
            if col_name in row.index and pd.notna(row[col_name]):
                entry[col_name] = round(float(row[col_name]), 4)

        bar_table.append(entry)

    # 10. Format trades
    trade_responses = [
        ValidationTradeResponse(
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

    # 11. Reference summary
    ref_summary = _compute_reference_summary(ref_trades) if ref_trades else None

    return ValidationStudyResponse(
        success=True,
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
        bars_processed=strategy_result.bars_processed,
        trades=trade_responses,
        reference_summary=ref_summary,
        comparisons=[TradeComparisonResponse(**asdict(c)) for c in comparisons],
        match_stats=MatchStatsResponse(**asdict(match_stats)),
        source_bars=source_count,
        rth_bars=rth_count,
        resampled_bars=resampled_count,
        chart_bars=chart_bars,
        chart_indicators=chart_indicators,
        bar_table=bar_table,
        column_descriptions=[ColumnDescription(**d) for d in COLUMN_DESCRIPTIONS],
        parameters=merged_params,
    )


@router.post("/run", response_model=ValidationStudyResponse)
async def run_validation_study(
    minute_csv: UploadFile = File(..., description="SPY 1-minute OHLCV CSV"),
    reference_trades_json: str = Form("[]", description="Reference trades JSON array"),
    parameters: str = Form("{}", description="Strategy parameters override JSON"),
):
    """Run validation study: parse minute CSV, resample to 15m, run EMA crossover strategy, compare."""
    try:
        logger.info("[VALIDATION] Starting validation study, file=%s", minute_csv.filename)
        content = await minute_csv.read()
        ref_trades = json.loads(reference_trades_json)
        params = json.loads(parameters)
        return _run_validation_pipeline(content, ref_trades, params)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {e}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("[VALIDATION] Error: %s", str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/export-csv")
async def export_validation_csv(
    minute_csv: UploadFile = File(...),
    reference_trades_json: str = Form("[]"),
    parameters: str = Form("{}"),
):
    """Run validation and return a ZIP with CSVs."""
    try:
        content = await minute_csv.read()
        ref_trades = json.loads(reference_trades_json)
        params = json.loads(parameters)
        result = _run_validation_pipeline(content, ref_trades, params)

        if not result.success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.error)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1. Resampled bars CSV
            bars_csv = _build_csv(
                [_bar_row(b) for b in result.bar_table],
                ["timestamp", "iso_time", "open", "high", "low", "close", "volume"],
            )
            zf.writestr("spy_15min_bars.csv", bars_csv)

            # 2. Reproduced trades CSV
            trades_csv = _build_csv(
                [_trade_row(t) for t in result.trades],
                [
                    "trade_number",
                    "trade_type",
                    "entry_timestamp",
                    "exit_timestamp",
                    "entry_price",
                    "exit_price",
                    "pnl",
                    "pnl_pct",
                    "cumulative_pnl_pct",
                    "signal_reason",
                    "result",
                ],
            )
            zf.writestr("reproduced_trades.csv", trades_csv)

            # 3. Reference trades CSV
            if ref_trades:
                ref_csv = _build_csv(
                    ref_trades,
                    ["entry_time", "exit_time", "entry_price", "exit_price", "pnl", "pnl_pct", "result"],
                )
                zf.writestr("reference_trades.csv", ref_csv)

            # 4. Comparison CSV
            comp_csv = _build_csv(
                [asdict(c) if isinstance(c, TradeComparison) else c.model_dump() for c in result.comparisons],
                [
                    "trade_num",
                    "ref_entry_time",
                    "our_entry_time",
                    "ref_entry_price",
                    "our_entry_price",
                    "ref_exit_price",
                    "our_exit_price",
                    "ref_pnl",
                    "our_pnl",
                    "pnl_delta",
                    "timestamp_delta_s",
                    "matched",
                    "source",
                ],
            )
            zf.writestr("comparison.csv", comp_csv)

            # 5. Columns CSV
            cols_csv = _build_csv(
                [d.model_dump() for d in result.column_descriptions],
                ["name", "description", "type"],
            )
            zf.writestr("columns.csv", cols_csv)

            # 6. Metadata CSV
            meta = {
                "strategy": "ema_crossover_rsi",
                "ticker": "SPY",
                "timeframe": "15m",
                "session": "rth",
                "source_bars": result.source_bars,
                "rth_bars": result.rth_bars,
                "resampled_bars": result.resampled_bars,
                "total_trades": result.total_trades,
                "winning_trades": result.winning_trades,
                "losing_trades": result.losing_trades,
                "win_rate": f"{result.win_rate:.4f}",
                "total_pnl_pct": f"{result.total_pnl_pct:.6f}",
                "profit_factor": f"{result.profit_factor:.4f}",
                "sharpe_ratio": f"{result.sharpe_ratio:.4f}",
                "max_drawdown_pct": f"{result.max_drawdown_pct:.6f}",
            }
            meta_csv = _build_csv([meta], list(meta.keys()))
            zf.writestr("metadata.csv", meta_csv)

        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="validation_study_SPY_15m.zip"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[VALIDATION EXPORT] Error: %s", str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/report")
async def generate_validation_report(
    minute_csv: UploadFile = File(...),
    reference_trades_json: str = Form("[]"),
    parameters: str = Form("{}"),
):
    """Run validation and return a markdown report."""
    try:
        content = await minute_csv.read()
        ref_trades = json.loads(reference_trades_json)
        params = json.loads(parameters)
        result = _run_validation_pipeline(content, ref_trades, params)

        if not result.success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.error)

        md = _build_markdown_report(result, ref_trades)
        return {"markdown": md}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[VALIDATION REPORT] Error: %s", str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/report-pdf")
async def generate_validation_pdf(
    minute_csv: UploadFile = File(...),
    reference_trades_json: str = Form("[]"),
    parameters: str = Form("{}"),
):
    """Run validation and return a professional PDF report."""
    try:
        content = await minute_csv.read()
        ref_trades = json.loads(reference_trades_json)
        params = json.loads(parameters)
        result = _run_validation_pipeline(content, ref_trades, params)

        if not result.success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.error)

        pdf_bytes = _build_pdf_report(result)
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="validation_report_SPY_15m.pdf"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[VALIDATION PDF] Error: %s", str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _build_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _bar_row(bar: dict) -> dict:
    ts = bar.get("timestamp", 0)
    iso = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""
    return {**bar, "iso_time": iso}


def _trade_row(t: ValidationTradeResponse) -> dict:
    return {
        "trade_number": t.trade_number,
        "trade_type": t.trade_type,
        "entry_timestamp": t.entry_timestamp,
        "exit_timestamp": t.exit_timestamp,
        "entry_price": f"{t.entry_price:.2f}",
        "exit_price": f"{t.exit_price:.2f}",
        "pnl": f"{t.pnl:.2f}",
        "pnl_pct": f"{t.pnl_pct:.6f}",
        "cumulative_pnl_pct": f"{t.cumulative_pnl_pct:.6f}",
        "signal_reason": t.signal_reason,
        "result": "WIN" if t.pnl > 0 else "LOSS",
    }


def _build_markdown_report(result: ValidationStudyResponse, ref_trades: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Strategy Validation Report")
    lines.append(f"\n**Generated:** {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("**Strategy:** EMA Crossover RSI (SPY 15-Min Long)")

    # Strategy Rules
    lines.append("\n## Strategy Rules")
    lines.append("\n### Entry Conditions (all must be true)")
    lines.append("1. EMA(5) crosses ABOVE EMA(10) on current 15-min candle CLOSE")
    lines.append("2. EMA Gap = (EMA5 - EMA10) >= 0.20 at crossover candle")
    lines.append("3. RSI (14-period) is between 50 and 70 at crossover candle")
    lines.append("4. Enter at CLOSE of the crossover candle")
    lines.append("\n### Exit Condition")
    lines.append("- Exit exactly 5 candles (75 minutes) after entry at CLOSE")
    lines.append("- If 5th candle is after market close, exit at next open")
    lines.append("\n### Skip Conditions")
    lines.append("- RSI < 50 or RSI > 70 at signal")
    lines.append("- EMA gap < 0.20 (crossover too weak)")
    lines.append("- Not a FRESH crossover (EMA5 was already above EMA10)")

    # Data Pipeline
    lines.append("\n## Data Pipeline")
    lines.append("| Step | Count |")
    lines.append("|------|-------|")
    lines.append(f"| Source 1-min bars | {result.source_bars:,} |")
    lines.append(f"| After RTH filter | {result.rth_bars:,} |")
    lines.append(f"| Resampled 15-min bars | {result.resampled_bars:,} |")
    lines.append(f"| Bars processed by strategy | {result.bars_processed:,} |")

    # Parameters
    lines.append("\n## Parameters Used")
    lines.append("| Parameter | Value |")
    lines.append("|-----------|-------|")
    for k, v in result.parameters.items():
        if k.startswith("_"):
            continue
        lines.append(f"| {k} | {v} |")

    # Metrics Comparison
    lines.append("\n## Performance Metrics Comparison")
    lines.append("| Metric | Reproduced | Reference | Delta |")
    lines.append("|--------|-----------|-----------|-------|")

    ref = result.reference_summary
    if ref:
        lines.append(
            f"| Total Trades | {result.total_trades} | {ref.total_trades} | {result.total_trades - ref.total_trades} |"
        )
        lines.append(
            f"| Winning Trades | {result.winning_trades} | {ref.winning_trades} | {result.winning_trades - ref.winning_trades} |"
        )
        lines.append(
            f"| Losing Trades | {result.losing_trades} | {ref.losing_trades} | {result.losing_trades - ref.losing_trades} |"
        )
        lines.append(
            f"| Win Rate | {result.win_rate:.1%} | {ref.win_rate:.1%} | {(result.win_rate - ref.win_rate):.1%} |"
        )
        lines.append(
            f"| Total PnL % | {result.total_pnl_pct:.4%} | {ref.total_pnl_pct:.4%} | {(result.total_pnl_pct - ref.total_pnl_pct):.4%} |"
        )
        lines.append(
            f"| Avg Win % | {result.avg_win_pct:.4%} | {ref.avg_win_pct:.4%} | {(result.avg_win_pct - ref.avg_win_pct):.4%} |"
        )
        lines.append(
            f"| Avg Loss % | {result.avg_loss_pct:.4%} | {ref.avg_loss_pct:.4%} | {(result.avg_loss_pct - ref.avg_loss_pct):.4%} |"
        )
        lines.append(
            f"| Profit Factor | {result.profit_factor:.2f} | {ref.profit_factor:.2f} | {result.profit_factor - ref.profit_factor:.2f} |"
        )
    else:
        lines.append(f"| Total Trades | {result.total_trades} | - | - |")
        lines.append(f"| Win Rate | {result.win_rate:.1%} | - | - |")
        lines.append(f"| Total PnL % | {result.total_pnl_pct:.4%} | - | - |")
        lines.append(f"| Profit Factor | {result.profit_factor:.2f} | - | - |")

    lines.append(f"| Max Drawdown | {result.max_drawdown_pct:.4%} | - | - |")
    lines.append(f"| Sharpe Ratio | {result.sharpe_ratio:.4f} | - | - |")

    # Match Summary
    if result.match_stats:
        ms = result.match_stats
        lines.append("\n## Trade Matching Summary")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Reference Trades | {ms.total_ref} |")
        lines.append(f"| Reproduced Trades | {ms.total_ours} |")
        lines.append(f"| Matched | {ms.matched_count} |")
        lines.append(f"| Extra (Reference only) | {ms.extra_ref} |")
        lines.append(f"| Extra (Reproduced only) | {ms.extra_ours} |")
        lines.append(f"| **Match Rate** | **{ms.match_rate:.1%}** |")
        lines.append(f"| Avg Timestamp Delta | {ms.avg_ts_delta_s:.0f}s |")
        lines.append(f"| Avg Entry Price Delta | ${ms.avg_entry_price_delta:.2f} |")
        lines.append(f"| Avg PnL Delta | ${ms.avg_pnl_delta:.2f} |")

        # Verdict
        lines.append("\n## Verdict")
        if ms.match_rate >= 0.9:
            lines.append(
                f"**VALIDATED** — {ms.matched_count}/{ms.total_ref} trades matched ({ms.match_rate:.0%}). "
                "Strategy reproduction is consistent with the reference study."
            )
        elif ms.match_rate >= 0.7:
            lines.append(
                f"**PARTIALLY VALIDATED** — {ms.matched_count}/{ms.total_ref} trades matched ({ms.match_rate:.0%}). "
                "Most trades reproduced correctly; discrepancies likely due to data source or "
                "resampling differences."
            )
        else:
            lines.append(
                f"**DIVERGENT** — Only {ms.matched_count}/{ms.total_ref} trades matched ({ms.match_rate:.0%}). "
                "Significant differences between reference and reproduced results. "
                "Investigate data source, indicator computation, or strategy parameters."
            )

    # Trade-by-trade comparison
    if result.comparisons:
        lines.append("\n## Trade-by-Trade Comparison")
        lines.append("| # | Ref Entry | Our Entry | Ref PnL | Our PnL | Delta | Match |")
        lines.append("|---|-----------|-----------|---------|---------|-------|-------|")
        for c in result.comparisons:
            ref_entry = c.ref_entry_time or "-"
            our_entry = c.our_entry_time or "-"
            ref_pnl = f"${c.ref_pnl:.2f}" if c.ref_pnl is not None else "-"
            our_pnl = f"${c.our_pnl:.2f}" if c.our_pnl is not None else "-"
            delta = f"${c.pnl_delta:.2f}" if c.pnl_delta is not None else "-"
            match_icon = "yes" if c.matched else "no"
            lines.append(
                f"| {c.trade_num} | {ref_entry} | {our_entry} | {ref_pnl} | {our_pnl} | {delta} | {match_icon} |"
            )

    # Reproduced trade log
    lines.append("\n## Reproduced Trade Log")
    lines.append("| # | Entry Time | Exit Time | Entry $ | Exit $ | PnL | PnL % | Cum PnL % | Result |")
    lines.append("|---|-----------|-----------|---------|--------|-----|-------|-----------|--------|")
    for t in result.trades:
        result_label = "WIN" if t.pnl > 0 else "LOSS"
        lines.append(
            f"| {t.trade_number} | {t.entry_timestamp} | {t.exit_timestamp} | "
            f"${t.entry_price:.2f} | ${t.exit_price:.2f} | "
            f"${t.pnl:.2f} | {t.pnl_pct:.4%} | {t.cumulative_pnl_pct:.4%} | {result_label} |"
        )

    return "\n".join(lines)


def _build_pdf_report(result: ValidationStudyResponse) -> bytes:
    """Build a professional PDF validation report using reportlab."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title2", parent=styles["Title"], fontSize=18, spaceAfter=6)
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=14, spaceBefore=16, spaceAfter=8)
    ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("Body2", parent=styles["Normal"], fontSize=9, leading=12)
    small = ParagraphStyle(
        "Small", parent=styles["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#555555")
    )

    story: list = []

    # Header
    story.append(Paragraph("Strategy Validation Report", title_style))
    story.append(
        Paragraph(
            f"<b>Strategy:</b> EMA Crossover RSI &mdash; SPY 15-Min Long &nbsp;&nbsp;|&nbsp;&nbsp;"
            f"<b>Generated:</b> {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            small,
        )
    )
    story.append(Spacer(1, 12))

    # Strategy Rules
    story.append(Paragraph("Strategy Rules", h1))
    rules = [
        "<b>Entry:</b> EMA(5) crosses ABOVE EMA(10) on 15-min candle close + EMA gap &ge; 0.20 + RSI(14) between 50&ndash;70. Enter at CLOSE.",
        "<b>Exit:</b> 5 candles (75 min) after entry at CLOSE. If after market close, exit at next open.",
        "<b>Skip:</b> RSI outside 50&ndash;70, gap &lt; 0.20, or not a fresh crossover.",
    ]
    for rule in rules:
        story.append(Paragraph(f"&bull; {rule}", body))
    story.append(Spacer(1, 8))

    # Data Pipeline
    story.append(Paragraph("Data Pipeline", h1))
    pipeline_data = [
        ["Step", "Count"],
        ["Source 1-min bars", f"{result.source_bars:,}"],
        ["After RTH filter", f"{result.rth_bars:,}"],
        ["Resampled 15-min bars", f"{result.resampled_bars:,}"],
        ["Bars processed", f"{result.bars_processed:,}"],
    ]
    story.append(_pdf_table(pipeline_data, col_widths=[3 * inch, 1.5 * inch]))
    story.append(Spacer(1, 8))

    # Performance Metrics
    story.append(Paragraph("Performance Metrics Comparison", h1))
    ref = result.reference_summary
    if ref:
        metrics_data = [
            ["Metric", "Reproduced", "Reference", "Delta"],
            [
                "Total Trades",
                str(result.total_trades),
                str(ref.total_trades),
                str(result.total_trades - ref.total_trades),
            ],
            [
                "Winning",
                str(result.winning_trades),
                str(ref.winning_trades),
                str(result.winning_trades - ref.winning_trades),
            ],
            [
                "Losing",
                str(result.losing_trades),
                str(ref.losing_trades),
                str(result.losing_trades - ref.losing_trades),
            ],
            ["Win Rate", f"{result.win_rate:.1%}", f"{ref.win_rate:.1%}", f"{(result.win_rate - ref.win_rate):.1%}"],
            [
                "Total PnL %",
                f"{result.total_pnl_pct:.4%}",
                f"{ref.total_pnl_pct:.4%}",
                f"{(result.total_pnl_pct - ref.total_pnl_pct):.4%}",
            ],
            [
                "Avg Win %",
                f"{result.avg_win_pct:.4%}",
                f"{ref.avg_win_pct:.4%}",
                f"{(result.avg_win_pct - ref.avg_win_pct):.4%}",
            ],
            [
                "Avg Loss %",
                f"{result.avg_loss_pct:.4%}",
                f"{ref.avg_loss_pct:.4%}",
                f"{(result.avg_loss_pct - ref.avg_loss_pct):.4%}",
            ],
            [
                "Profit Factor",
                f"{result.profit_factor:.2f}",
                f"{ref.profit_factor:.2f}",
                f"{result.profit_factor - ref.profit_factor:+.2f}",
            ],
            ["Max Drawdown", f"{result.max_drawdown_pct:.4%}", "-", "-"],
            ["Sharpe Ratio", f"{result.sharpe_ratio:.4f}", "-", "-"],
        ]
        story.append(_pdf_table(metrics_data, col_widths=[1.8 * inch, 1.5 * inch, 1.5 * inch, 1.2 * inch]))
    else:
        metrics_data = [
            ["Metric", "Value"],
            ["Total Trades", str(result.total_trades)],
            ["Win Rate", f"{result.win_rate:.1%}"],
            ["Total PnL %", f"{result.total_pnl_pct:.4%}"],
            ["Profit Factor", f"{result.profit_factor:.2f}"],
            ["Max Drawdown", f"{result.max_drawdown_pct:.4%}"],
            ["Sharpe Ratio", f"{result.sharpe_ratio:.4f}"],
        ]
        story.append(_pdf_table(metrics_data, col_widths=[3 * inch, 2 * inch]))
    story.append(Spacer(1, 8))

    # Match Summary
    if result.match_stats:
        ms = result.match_stats
        story.append(Paragraph("Trade Matching Summary", h1))
        match_data = [
            ["Metric", "Value"],
            ["Reference Trades", str(ms.total_ref)],
            ["Reproduced Trades", str(ms.total_ours)],
            ["Matched", str(ms.matched_count)],
            ["Extra (Reference only)", str(ms.extra_ref)],
            ["Extra (Reproduced only)", str(ms.extra_ours)],
            ["Match Rate", f"{ms.match_rate:.1%}"],
            ["Avg Timestamp Delta", f"{ms.avg_ts_delta_s:.0f}s"],
            ["Avg Entry Price Delta", f"${ms.avg_entry_price_delta:.2f}"],
            ["Avg PnL Delta", f"${ms.avg_pnl_delta:.2f}"],
        ]
        story.append(_pdf_table(match_data, col_widths=[3 * inch, 2 * inch]))
        story.append(Spacer(1, 8))

        # Verdict
        story.append(Paragraph("Verdict", h1))
        if ms.match_rate >= 0.9:
            verdict = (
                f"<b>VALIDATED</b> &mdash; {ms.matched_count}/{ms.total_ref} trades matched "
                f"({ms.match_rate:.0%}). Strategy reproduction is consistent with the reference study."
            )
            verdict_color = colors.HexColor("#166534")
        elif ms.match_rate >= 0.7:
            verdict = (
                f"<b>PARTIALLY VALIDATED</b> &mdash; {ms.matched_count}/{ms.total_ref} trades matched "
                f"({ms.match_rate:.0%}). Most trades reproduced correctly."
            )
            verdict_color = colors.HexColor("#92400e")
        else:
            verdict = (
                f"<b>DIVERGENT</b> &mdash; {ms.matched_count}/{ms.total_ref} trades matched "
                f"({ms.match_rate:.0%}). Significant differences found."
            )
            verdict_color = colors.HexColor("#991b1b")

        verdict_style = ParagraphStyle("Verdict", parent=body, fontSize=10, textColor=verdict_color, leading=14)
        story.append(Paragraph(verdict, verdict_style))
        story.append(Spacer(1, 12))

    # Trade-by-Trade Comparison
    if result.comparisons:
        story.append(Paragraph("Trade-by-Trade Comparison", h1))
        comp_header = ["#", "Ref Entry", "Our Entry", "Ref PnL", "Our PnL", "Delta", "Match"]
        comp_rows = [comp_header]
        for c in result.comparisons:
            comp_rows.append(
                [
                    str(c.trade_num),
                    c.ref_entry_time or "-",
                    c.our_entry_time or "-",
                    f"${c.ref_pnl:.2f}" if c.ref_pnl is not None else "-",
                    f"${c.our_pnl:.2f}" if c.our_pnl is not None else "-",
                    f"${c.pnl_delta:.2f}" if c.pnl_delta is not None else "-",
                    "Yes" if c.matched else "No",
                ]
            )
        story.append(
            _pdf_table(
                comp_rows,
                col_widths=[0.4 * inch, 1.3 * inch, 1.3 * inch, 0.8 * inch, 0.8 * inch, 0.7 * inch, 0.5 * inch],
                font_size=7,
                row_colors=True,
            )
        )
        story.append(Spacer(1, 12))

    # Reproduced Trade Log
    story.append(Paragraph("Reproduced Trade Log", h1))
    trade_header = ["#", "Entry Time", "Exit Time", "Entry $", "Exit $", "PnL", "PnL %", "Cum %", "Result"]
    trade_rows = [trade_header]
    for t in result.trades:
        trade_rows.append(
            [
                str(t.trade_number),
                t.entry_timestamp,
                t.exit_timestamp,
                f"${t.entry_price:.2f}",
                f"${t.exit_price:.2f}",
                f"${t.pnl:.2f}",
                f"{t.pnl_pct:.3%}",
                f"{t.cumulative_pnl_pct:.3%}",
                "WIN" if t.pnl > 0 else "LOSS",
            ]
        )
    story.append(
        _pdf_table(
            trade_rows,
            col_widths=[
                0.35 * inch,
                1.15 * inch,
                1.15 * inch,
                0.7 * inch,
                0.7 * inch,
                0.6 * inch,
                0.7 * inch,
                0.7 * inch,
                0.55 * inch,
            ],
            font_size=7,
            row_colors=True,
        )
    )

    doc.build(story)
    return buf.getvalue()


def _pdf_table(
    data: list[list[str]],
    col_widths: list | None = None,
    font_size: int = 8,
    row_colors: bool = False,
) -> Table:  # noqa: F821 — Table is imported inside function body
    """Create a styled reportlab Table."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    t = Table(data, colWidths=col_widths, repeatRows=1)
    style_cmds: list = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), font_size),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), font_size),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]

    if row_colors:
        for i in range(1, len(data)):
            bg = colors.HexColor("#f0f4f8") if i % 2 == 0 else colors.white
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))

    t.setStyle(TableStyle(style_cmds))
    return t
