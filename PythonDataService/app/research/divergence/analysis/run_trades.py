"""Day 4 orchestrator: run 3 strategies × 4 variants and diff the trade lists.

Reads the Day-3 enriched merged.parquet, executes each strategy against
the TV / Native / Engine / Engine-ETH indicator sets, pairs the resulting
trade lists against V-A (TradingView truth), and writes summary tables.

Outputs under ``cache/divergence/{tf}/trades/``:

    {strategy}_{variant}.parquet       # per-variant trade list
    summary.csv                        # per (strategy, variant) aggregate
    match_{strategy}_{variant}.csv     # per (V-A vs variant) categorization
    match_summary.csv                  # per (strategy, variant) bucket stats
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from app.research.divergence.analysis.trade_divergence import (
    categorize_trade_lists,
    matches_to_frame,
)
from app.research.divergence.strategies import (
    run_s1_ema_crossover,
    run_s2_rsi_mean_reversion,
    run_s3_sma_crossover,
)
from app.research.divergence.strategies.engine_runner import (
    build_vd_15m_with_engine_indicators,
)

logger = logging.getLogger(__name__)

CACHE_ROOT = Path("cache/divergence")


# ---------- Strategy × variant dispatch table ----------
# For each (strategy, variant) we define which indicator columns to feed.

_S1_COLS = {
    "V-A": ("ema_5", "ema_10", "rsi_14"),
    "V-B": ("ema_5_native", "ema_10_native", "rsi_14_native"),
    "V-C": ("ema_5_engine", "ema_10_engine", "rsi_14_engine"),
    # V-D uses its own frame (built from lean-cache) with *_engine cols
    "V-D": ("ema_5_engine", "ema_10_engine", "rsi_14_engine"),
}
_S2_COLS = {
    "V-A": "rsi_14",
    "V-B": "rsi_14_native",
    "V-C": "rsi_14_engine",
    "V-D": "rsi_14_engine",
}
_S3_COLS = {
    "V-A": ("sma_50", "sma_200"),
    "V-B": ("sma_50_native", "sma_200_native"),
    "V-C": ("sma_50_engine", "sma_200_engine"),
    "V-D": ("sma_50_engine", "sma_200_engine"),
}


def _run_all_variants(
    merged_rth: pd.DataFrame,
    vd_df: pd.DataFrame,
    timeframe: str,
) -> dict[tuple[str, str], pd.DataFrame]:
    """Run every (strategy, variant) and return a dict of trade-lists."""
    results: dict[tuple[str, str], any] = {}

    for variant in ("V-A", "V-B", "V-C", "V-D"):
        df = vd_df if variant == "V-D" else merged_rth
        # S1 — EMA crossover
        fast, slow, rsi = _S1_COLS[variant]
        tl = run_s1_ema_crossover(
            df,
            ema_fast_col=fast,
            ema_slow_col=slow,
            rsi_col=rsi,
            close_col="close_pg",
            variant=variant,
            timeframe=timeframe,
        )
        results[("s1_ema_crossover", variant)] = tl

        # S2 — RSI mean reversion
        rsi_col = _S2_COLS[variant]
        tl2 = run_s2_rsi_mean_reversion(
            df,
            rsi_col=rsi_col,
            close_col="close_pg",
            variant=variant,
            timeframe=timeframe,
        )
        results[("s2_rsi_mean_reversion", variant)] = tl2

        # S3 — SMA crossover (needs long history; short window often has no trades)
        sma_fast, sma_slow = _S3_COLS[variant]
        if sma_fast in df.columns and sma_slow in df.columns:
            tl3 = run_s3_sma_crossover(
                df,
                sma_fast_col=sma_fast,
                sma_slow_col=sma_slow,
                close_col="close_pg",
                variant=variant,
                timeframe=timeframe,
            )
            results[("s3_sma_crossover", variant)] = tl3

    return results


def run_day4(timeframe: str = "15m") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full Day-4 pipeline. Returns (trade_summary_df, match_summary_df)."""
    tf_cache = CACHE_ROOT / timeframe
    merged_path = tf_cache / "merged.parquet"
    if not merged_path.exists():
        raise FileNotFoundError(
            f"Run Day 3 first: {merged_path} not found. Use `python -m app.research.divergence.cli all ...`."
        )

    merged = pd.read_parquet(merged_path)
    logger.info("loaded merged.parquet: %d rows", len(merged))

    # Build V-D frame from the lean-cache full-session data, covering the
    # same date span as merged.parquet (with a 90-day warmup before).
    start_utc = merged["time_utc"].iloc[0]
    end_utc = merged["time_utc"].iloc[-1]
    warmup_start = (pd.Timestamp(start_utc) - pd.Timedelta(days=90)).date()
    end_date = pd.Timestamp(end_utc).date()
    logger.info("[V-D] building full-session 15m indicators from %s to %s", warmup_start, end_date)
    vd_df = build_vd_15m_with_engine_indicators(warmup_start, end_date)
    # Restrict V-D rows to the merged window for apples-to-apples comparison
    vd_df = vd_df[(vd_df["time_utc"] >= start_utc) & (vd_df["time_utc"] <= end_utc)].reset_index(drop=True)
    logger.info("[V-D] retained %d RTH 15m bars in the comparison window", len(vd_df))

    results = _run_all_variants(merged, vd_df, timeframe)

    # Write per-variant trade CSVs and summary
    trades_dir = tf_cache / "trades"
    trades_dir.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    for (strat, variant), tl in results.items():
        tl.to_frame().to_csv(trades_dir / f"{strat}_{variant}.csv", index=False)
        summary_rows.append(tl.summary())
    summary = pd.DataFrame(summary_rows).sort_values(["strategy", "variant"]).reset_index(drop=True)
    summary.to_csv(trades_dir / "summary.csv", index=False)

    # Pair every (strat, variant != "V-A") against V-A
    match_rows: list[dict] = []
    for strat_name in sorted({s for s, _ in results}):
        va_tl = results.get((strat_name, "V-A"))
        if va_tl is None:
            continue
        for variant in ("V-B", "V-C", "V-D"):
            vb_tl = results.get((strat_name, variant))
            if vb_tl is None:
                continue
            matches, msum = categorize_trade_lists(va_tl, vb_tl, tolerance_bars=5)
            matches_to_frame(matches).to_csv(
                trades_dir / f"match_{strat_name}_{variant}.csv",
                index=False,
            )
            flat = {"strategy": strat_name, "variant": variant}
            for cat in ("matched_aligned", "matched_shifted", "a_only_flip", "b_only_flip"):
                for k, v in msum[cat].items():
                    flat[f"{cat}_{k}"] = v
            if msum.get("shift_distribution"):
                for k, v in msum["shift_distribution"].items():
                    flat[f"shift_{k}"] = v
            match_rows.append(flat)

    match_summary = pd.DataFrame(match_rows)
    match_summary.to_csv(trades_dir / "match_summary.csv", index=False)
    return summary, match_summary
