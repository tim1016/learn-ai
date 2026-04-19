"""Time-align TradingView and Polygon DataFrames for direct comparison.

The join is an inner merge on ``time_utc``. Both sides are expected to
carry a tz-aware UTC timestamp column already. Column names on the
TradingView side are suffixed with ``_tv`` and on the Polygon side with
``_pg`` so downstream analysis can read both without ambiguity.

Returns (merged_df, summary_dict) where summary_dict contains diagnostic
counts useful for the dashboard's data-quality panel.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def align_tv_polygon(
    tv: pd.DataFrame,
    pg: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Inner-merge TV and Polygon bar data on ``time_utc``.

    OHLCV columns and any other non-timestamp column are suffixed
    ``_tv`` / ``_pg`` in the merged frame. The join is inner; bars present
    on one side but not the other are dropped and reported in the summary.
    """
    if "time_utc" not in tv.columns or "time_utc" not in pg.columns:
        raise ValueError("Both DataFrames must have a tz-aware `time_utc` column")

    # Narrow the TV side to the minimal column set the analysis needs, but
    # keep ALL indicator columns so we don't have to re-ingest to add one.
    keep_tv = [c for c in tv.columns if c not in {"et"}]
    keep_pg = [c for c in pg.columns if c != "et"]

    tv_subset = tv[keep_tv].copy()
    pg_subset = pg[keep_pg].copy()

    # Suffix overlapping columns. We add suffixes unconditionally to the
    # OHLCV pair so names are always explicit.
    ohlcv_cols = {"open", "high", "low", "close", "volume"}
    tv_subset = tv_subset.rename(columns={c: f"{c}_tv" for c in tv_subset.columns if c in ohlcv_cols})
    pg_subset = pg_subset.rename(columns={c: f"{c}_pg" for c in pg_subset.columns if c in ohlcv_cols})

    # Avoid duplicate `time` / `unix_ts` columns after merge.
    if "time" in tv_subset.columns:
        tv_subset = tv_subset.rename(columns={"time": "time_tv"})
    if "unix_ts" in pg_subset.columns:
        pg_subset = pg_subset.rename(columns={"unix_ts": "unix_ts_pg"})

    merged = pd.merge(tv_subset, pg_subset, on="time_utc", how="inner")

    tv_only = len(tv) - len(merged)
    pg_only = len(pg) - len(merged)
    summary = {
        "tv_rows": len(tv),
        "pg_rows": len(pg),
        "merged_rows": len(merged),
        "tv_only_dropped": int(tv_only),
        "pg_only_dropped": int(pg_only),
        "coverage_pct": round(len(merged) / max(len(tv), 1) * 100, 2),
        "first_merged_utc": merged["time_utc"].iloc[0].isoformat() if len(merged) else "",
        "last_merged_utc": merged["time_utc"].iloc[-1].isoformat() if len(merged) else "",
    }
    logger.info(
        "[ALIGN] TV=%d PG=%d merged=%d (tv_only=%d, pg_only=%d)",
        summary["tv_rows"],
        summary["pg_rows"],
        summary["merged_rows"],
        summary["tv_only_dropped"],
        summary["pg_only_dropped"],
    )
    return merged, summary
