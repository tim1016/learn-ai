"""Per-bar divergence analysis.

Given a merged TV+Polygon DataFrame and the computed Native + Engine
indicator columns, produce a tidy table of bar-level diff statistics and
write per-indicator parquet files for downstream charting.

The central data structure is a ``DivergenceMatrix``: one row per
(indicator, timeframe, implementation-pair, stat) combination. This is
what drives the dashboard's indicator heatmap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndicatorPair:
    """One side-by-side comparison within a merged dataframe."""

    indicator: str  # human name, e.g. "ema_20"
    tv_col: str  # column name with TV values
    other_col: str  # column name with comparison values
    other_label: str  # e.g. "native", "engine"
    timeframe: str  # "5m" / "15m" / "1h"


def diff_stats(a: pd.Series, b: pd.Series) -> dict[str, float | int | None]:
    """Compute descriptive diff stats for two aligned series."""
    d = (a - b).dropna()
    if len(d) == 0:
        return {
            "n": 0,
            "mean": None,
            "mean_abs": None,
            "median_abs": None,
            "p95_abs": None,
            "max_abs": None,
            "rmse": None,
            "corr": None,
        }
    a2, b2 = a.align(b, join="inner")
    mask = a2.notna() & b2.notna()
    if mask.sum() > 1:
        corr = float(np.corrcoef(a2[mask], b2[mask])[0, 1])
    else:
        corr = None
    return {
        "n": len(d),
        "mean": float(d.mean()),
        "mean_abs": float(d.abs().mean()),
        "median_abs": float(d.abs().median()),
        "p95_abs": float(d.abs().quantile(0.95)),
        "max_abs": float(d.abs().max()),
        "rmse": float(np.sqrt((d**2).mean())),
        "corr": corr,
    }


def pairwise_diff(
    merged: pd.DataFrame,
    pair: IndicatorPair,
) -> tuple[pd.DataFrame, dict]:
    """Produce a per-bar diff DataFrame and a stats dict for one pair."""
    if pair.tv_col not in merged.columns:
        raise KeyError(f"TV column {pair.tv_col!r} not in merged dataframe")
    if pair.other_col not in merged.columns:
        raise KeyError(f"Other column {pair.other_col!r} not in merged dataframe")

    df = merged[["time_utc", pair.tv_col, pair.other_col]].copy()
    df = df.rename(columns={pair.tv_col: "tv", pair.other_col: "other"})
    df["diff"] = df["tv"] - df["other"]
    df["abs_diff"] = df["diff"].abs()
    df["indicator"] = pair.indicator
    df["impl"] = pair.other_label
    df["timeframe"] = pair.timeframe

    stats = diff_stats(df["tv"], df["other"])
    stats.update(
        {
            "indicator": pair.indicator,
            "impl": pair.other_label,
            "timeframe": pair.timeframe,
        }
    )
    return df, stats


@dataclass
class DivergenceMatrix:
    """Summary table: one row per (indicator, impl, timeframe)."""

    rows: list[dict]

    def to_frame(self) -> pd.DataFrame:
        cols = [
            "indicator",
            "impl",
            "timeframe",
            "n",
            "mean",
            "mean_abs",
            "median_abs",
            "p95_abs",
            "max_abs",
            "rmse",
            "corr",
        ]
        df = pd.DataFrame(self.rows)
        for c in cols:
            if c not in df.columns:
                df[c] = None
        return df[cols].sort_values(["indicator", "impl", "timeframe"]).reset_index(drop=True)


# ----- Pair catalog: TV column → (native/engine column) ---------------------

# (indicator, tv_col, native_col, engine_col_or_None)
_PAIRS_15M: tuple[tuple[str, str, str, str | None], ...] = (
    ("ema_5", "ema_5", "ema_5_native", "ema_5_engine"),
    ("ema_10", "ema_10", "ema_10_native", "ema_10_engine"),
    ("ema_20", "ema_20", "ema_20_native", "ema_20_engine"),
    ("ema_30", "ema_30", "ema_30_native", "ema_30_engine"),
    ("ema_40", "ema_40", "ema_40_native", "ema_40_engine"),
    ("ema_50", "ema_50", "ema_50_native", "ema_50_engine"),
    ("ema_100", "ema_100", "ema_100_native", "ema_100_engine"),
    ("ema_200", "ema_200", "ema_200_native", "ema_200_engine"),
    ("sma_20", "sma_20", "sma_20_native", "sma_20_engine"),
    ("sma_50", "sma_50", "sma_50_native", "sma_50_engine"),
    ("sma_200", "sma_200", "sma_200_native", "sma_200_engine"),
    ("rsi_14", "rsi_14", "rsi_14_native", "rsi_14_engine"),
    ("macd_line", "macd_12_26_9", "macd_12_26_9_native", None),
    ("macd_signal", "macds_12_26_9", "macds_12_26_9_native", None),
    ("macd_hist", "macdh_12_26_9", "macdh_12_26_9_native", None),
    ("bb_mid", "bb_mid_20_2", "bb_mid_20_2_native", None),
    ("bb_upper", "bb_upper_20_2", "bb_upper_20_2_native", None),
    ("bb_lower", "bb_lower_20_2", "bb_lower_20_2_native", None),
    ("adx_14", "adx_14", "adx_14_native", None),
    ("dmp_14", "dmp_14", "dmp_14_native", None),
    ("dmn_14", "dmn_14", "dmn_14_native", None),
    ("atr_14", "atr_14", "atr_14_native", None),
    ("supert", "supert_10_3", "supert_10_3_native", None),
    ("supertd", "supertd_10_3", "supertd_10_3_native", None),
)


def run_full_comparison(
    merged: pd.DataFrame,
    timeframe: str,
    out_dir: Path | str,
) -> tuple[DivergenceMatrix, Path]:
    """Run every pair for this timeframe, write per-indicator parquet.

    Args:
        merged: DataFrame with TV columns, ``*_native`` columns and
            (where available) ``*_engine`` columns. Must also have a
            ``time_utc`` column.
        timeframe: "5m" / "15m" / "1h".
        out_dir: Directory under which ``diff/{indicator}_{impl}_{tf}.parquet``
            and ``matrix_{tf}.csv`` will be written.

    Returns:
        Tuple of (matrix, matrix_csv_path).
    """
    out_dir = Path(out_dir)
    (out_dir / "diff").mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []

    for indicator, tv_col, native_col, engine_col in _PAIRS_15M:
        # TV vs Native
        if native_col in merged.columns:
            pair = IndicatorPair(indicator, tv_col, native_col, "native", timeframe)
            diff_df, stats = pairwise_diff(merged, pair)
            diff_df.to_parquet(out_dir / "diff" / f"{indicator}_native_{timeframe}.parquet", index=False)
            rows.append(stats)
        # TV vs Engine
        if engine_col and engine_col in merged.columns:
            pair = IndicatorPair(indicator, tv_col, engine_col, "engine", timeframe)
            diff_df, stats = pairwise_diff(merged, pair)
            diff_df.to_parquet(out_dir / "diff" / f"{indicator}_engine_{timeframe}.parquet", index=False)
            rows.append(stats)

    matrix = DivergenceMatrix(rows)
    matrix_csv = out_dir / f"matrix_{timeframe}.csv"
    matrix.to_frame().to_csv(matrix_csv, index=False)
    logger.info("[DIVERGENCE] wrote %d rows to %s", len(rows), matrix_csv)
    return matrix, matrix_csv
