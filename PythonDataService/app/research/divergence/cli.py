"""CLI for the data-divergence research module.

Usage::

    python -m app.research.divergence.cli ingest   --tv CSV --pg CSV --tf 15m
    python -m app.research.divergence.cli compare  --tf 15m
    python -m app.research.divergence.cli all      --tv CSV --pg CSV --tf 15m

The ``all`` subcommand runs ingest → native compute → engine compute →
per-bar diff → matrix write in one shot. Intended for interactive use
during the first-pass build. The FastAPI endpoint wraps the same
functions but returns JSON instead of writing parquet.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from app.research.divergence.analysis.bar_divergence import run_full_comparison
from app.research.divergence.indicators.engine_adapter import (
    compute_engine_ema_batch,
    compute_engine_rsi_batch,
    compute_engine_sma_batch,
)
from app.research.divergence.indicators.native import (
    EMA_LENGTHS,
    SMA_LENGTHS,
    compute_all_native,
)
from app.research.divergence.ingest import (
    align_tv_polygon,
    ingest_polygon_1min_csv_resampled,
    ingest_tv_csv,
    reverse_dividend_adjustment,
)

CACHE_ROOT = Path("cache/divergence")


def _attach_engine(merged: pd.DataFrame) -> pd.DataFrame:
    """Append engine-computed indicator columns to ``merged`` (in place)."""
    # Use close_pg as the canonical input — that's Polygon's unadjusted price
    # and matches what the learn-ai engine actually consumes in production.
    close_col = "close_pg"
    for L in EMA_LENGTHS:
        merged[f"ema_{L}_engine"] = compute_engine_ema_batch(
            merged,
            length=L,
            time_col="time_utc",
            value_col=close_col,
        )
    for L in SMA_LENGTHS:
        merged[f"sma_{L}_engine"] = compute_engine_sma_batch(
            merged,
            length=L,
            time_col="time_utc",
            value_col=close_col,
        )
    merged["rsi_14_engine"] = compute_engine_rsi_batch(
        merged,
        length=14,
        time_col="time_utc",
        value_col=close_col,
    )
    return merged


def _run_all(
    tv_csv: str,
    pg_1min_csv: str,
    timeframe: str,
    div_adjust: bool = False,
) -> Path:
    tf_cache = CACHE_ROOT / timeframe
    tf_cache.mkdir(parents=True, exist_ok=True)

    # 1. Ingest
    tv_df, _ = ingest_tv_csv(tv_csv, timeframe=timeframe)
    pg_df, _ = ingest_polygon_1min_csv_resampled(
        pg_1min_csv,
        timeframe=timeframe,
        rth_only=True,
    )
    if div_adjust:
        tv_df = reverse_dividend_adjustment(tv_df)

    # 2. Align
    merged, align_summary = align_tv_polygon(tv_df, pg_df)
    logging.getLogger(__name__).info("aligned: %s", align_summary)

    # 3. Native indicators — computed on Polygon's close (the unadjusted
    #    feed our engine consumes in production).
    native_input = pd.DataFrame(
        {
            "close": merged["close_pg"],
            "high": merged["high_pg"],
            "low": merged["low_pg"],
        }
    )
    native_out = compute_all_native(native_input)
    for col in native_out.columns:
        if col.endswith("_native"):
            merged[col] = native_out[col].values

    # 4. Engine-batch indicators.
    _attach_engine(merged)

    # 5. Save the enriched merged dataframe.
    merged_path = tf_cache / "merged.parquet"
    merged.to_parquet(merged_path, index=False)
    logging.getLogger(__name__).info("wrote %s", merged_path)

    # 6. Per-bar divergence + matrix CSV.
    _, matrix_csv = run_full_comparison(merged, timeframe, tf_cache)
    return matrix_csv


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.research.divergence.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_all = sub.add_parser("all", help="Run ingest → native → engine → diff")
    p_all.add_argument("--tv", required=True, help="Path to TradingView CSV")
    p_all.add_argument("--pg", required=True, help="Path to Polygon 1-min CSV")
    p_all.add_argument("--tf", default="15m", choices=["5m", "15m", "1h"])
    p_all.add_argument("--div-adjust", action="store_true", help="Apply reverse dividend adjustment to TV")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "all":
        path = _run_all(args.tv, args.pg, args.tf, div_adjust=args.div_adjust)
        print(f"\nDivergence matrix: {path}")
    else:
        parser.error(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()
