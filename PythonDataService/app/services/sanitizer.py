"""Data sanitization using native pandas/numpy (replaces pandas-dq Fix_DQ).

Formula: Gap detection (NaN/0-volume/OHLC-violation checks), outlier clipping (99th-percentile quantile), monotonicity enforcement, fail-fast duplicate detection.
Reference: Internal — no external algorithmic reference; gap/monotonicity rules are repo-invariants per .claude/rules/numerical-rigor.md (Timestamp rigor → Two and only two conversion boundaries).
Canonical implementation: app/services/sanitizer.py
Validated against: PythonDataService/tests/test_sanitizer.py
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

from app.config import settings

logger = logging.getLogger(__name__)


def _clean_numeric(df: pd.DataFrame, quantile: float = 0.99) -> pd.DataFrame:
    """Clean numeric DataFrame: fill NaNs with median, clip outliers by quantile."""
    numeric = df.select_dtypes(include="number")
    if numeric.empty:
        return df

    # Fill missing with column median
    filled = numeric.fillna(numeric.median())

    # Clip outliers beyond lower/upper quantile bounds
    lo = filled.quantile(1 - quantile)
    hi = filled.quantile(quantile)
    clipped = filled.clip(lower=lo, upper=hi, axis=1)

    # Remove rows that were entirely NaN across all numeric cols before fill
    all_nan_mask = numeric.isna().all(axis=1)
    result = df.copy()
    result[clipped.columns] = clipped
    result = result[~all_nan_mask]
    return result


class DataSanitizer:
    """Sanitize market data using native pandas/numpy operations"""

    @staticmethod
    def sanitize_aggregates(raw_data: list[dict[str, Any]]) -> dict[str, Any]:
        """Clean and validate OHLCV aggregate data"""
        try:
            if not raw_data:
                return {"data": [], "summary": {"original_count": 0, "cleaned_count": 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Sanitizing {original_count} aggregate records")

            # Convert timestamp from ms to datetime for processing.
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

            # Fail-fast on duplicates — they indicate upstream corruption.
            if settings.REMOVE_DUPLICATES:
                dup_mask = df["timestamp"].duplicated(keep=False)
                if dup_mask.any():
                    dup_ts = df.loc[dup_mask, "timestamp"].unique()[:3].tolist()
                    raise ValueError(
                        f"Duplicate timestamps detected ({dup_mask.sum()} rows): {dup_ts}. "
                        "Callers must deduplicate before sanitizing."
                    )

            # Sort by timestamp
            df = df.sort_values("timestamp").reset_index(drop=True)

            # Clean numeric columns (median fill + quantile clip)
            numeric_cols = ["open", "high", "low", "close", "volume"]
            optional_cols = [c for c in ["vwap", "transactions"] if c in df.columns]
            dq_cols = numeric_cols + optional_cols

            timestamps = df["timestamp"]
            dq_df = df[dq_cols].copy()
            cleaned = _clean_numeric(dq_df)

            df = cleaned.copy()
            df["timestamp"] = timestamps.loc[df.index]

            # Basic OHLCV integrity filter (keep rows where high >= low and volume >= 0)
            mask = (df["high"] >= df["low"]) & (df["volume"] >= 0)
            df = df[mask]

            # Return timestamp as int64 ms UTC (canonical wire format).
            # Pandas 3 defaults ``pd.to_datetime(..., unit="ms", utc=True)`` to
            # ``datetime64[ms, UTC]`` precision, where pandas 2 returned
            # ``datetime64[ns, UTC]``. The previous
            # ``astype("datetime64[ms]").astype("int64")`` chain attempted to
            # strip tz by going naive — illegal under recent pandas
            # (``Cannot use .astype to convert from timezone-aware dtype to
            # timezone-naive dtype``). Cast to ns explicitly so ``int64``
            # always yields ns-since-epoch-UTC, then divide to ms — robust
            # against pandas internal-precision defaults.
            df["timestamp"] = df["timestamp"].astype("datetime64[ns, UTC]").astype("int64") // 1_000_000

            cleaned_count = len(df)
            removed_count = original_count - cleaned_count

            logger.info(f"Sanitization complete: {cleaned_count} records retained, {removed_count} removed")

            return {
                "data": df.to_dict("records"),
                "summary": {
                    "original_count": original_count,
                    "cleaned_count": cleaned_count,
                    "removed_count": removed_count,
                    "removal_percentage": round((removed_count / original_count) * 100, 2) if original_count > 0 else 0,
                },
            }

        except Exception as e:
            logger.error(f"Error sanitizing aggregates: {e!s}")
            raise

    @staticmethod
    def sanitize_trades(raw_data: list[dict[str, Any]]) -> dict[str, Any]:
        """Clean and validate trade data"""
        try:
            if not raw_data:
                return {"data": [], "summary": {"original_count": 0, "cleaned_count": 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Sanitizing {original_count} trade records")

            # Convert timestamp (nanoseconds for trades).
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ns", utc=True)

            # Fail-fast on duplicates — they indicate upstream corruption.
            if settings.REMOVE_DUPLICATES:
                subset = ["timestamp", "trade_id"] if "trade_id" in df.columns else ["timestamp"]
                dup_mask = df.duplicated(subset=subset, keep=False)
                if dup_mask.any():
                    dup_ts = df.loc[dup_mask, "timestamp"].unique()[:3].tolist()
                    raise ValueError(
                        f"Duplicate trade records detected ({dup_mask.sum()} rows): {dup_ts}. "
                        "Callers must deduplicate before sanitizing."
                    )

            df = df.sort_values("timestamp").reset_index(drop=True)

            # Clean numeric trade columns
            timestamps = df["timestamp"]
            non_numeric_cols = ["exchange", "conditions", "sequence_number", "trade_id"]
            saved_cols = {col: df[col].copy() for col in non_numeric_cols if col in df.columns}

            numeric_cols = [c for c in ["price", "size"] if c in df.columns]
            dq_df = df[numeric_cols].copy()
            cleaned = _clean_numeric(dq_df)

            df = cleaned.copy()
            df["timestamp"] = timestamps.loc[df.index]
            for col, series in saved_cols.items():
                df[col] = series.loc[df.index].values

            # Basic validity: price > 0, size > 0
            df = df[(df["price"] > 0) & (df["size"] > 0)]

            # Return timestamp as int64 ms UTC (canonical wire format).
            # Same precision-default story as ``sanitize_aggregates`` above:
            # cast to ``datetime64[ns, UTC]`` first so ``int64`` always
            # yields ns-since-epoch-UTC, then divide to ms.
            df["timestamp"] = df["timestamp"].astype("datetime64[ns, UTC]").astype("int64") // 1_000_000

            cleaned_count = len(df)

            return {
                "data": df.to_dict("records"),
                "summary": {
                    "original_count": original_count,
                    "cleaned_count": cleaned_count,
                    "removed_count": original_count - cleaned_count,
                },
            }

        except Exception as e:
            logger.error(f"Error sanitizing trades: {e!s}")
            raise

    @staticmethod
    def sanitize_indicator(raw_data: dict[str, Any]) -> dict[str, Any]:
        """Clean and validate technical indicator data"""
        try:
            logger.info(f"Sanitizing {raw_data.get('indicator_type')} indicator")

            if raw_data.get("values"):
                values_df = pd.DataFrame(raw_data["values"])
                cleaned = _clean_numeric(values_df)
                raw_data["values"] = cleaned.to_dict("records")

            return {
                "data": raw_data,
                "summary": {
                    "indicator_type": raw_data.get("indicator_type"),
                    "ticker": raw_data.get("ticker"),
                    "values_count": len(raw_data.get("values", [])),
                },
            }

        except Exception as e:
            logger.error(f"Error sanitizing indicator: {e!s}")
            raise

    @staticmethod
    def sanitize_generic(raw_data: list[dict[str, Any]], quantile: float = 0.99) -> dict[str, Any]:
        """Sanitize arbitrary market data.
        Used by the standalone /api/sanitize endpoint."""
        try:
            if not raw_data:
                return {"data": [], "summary": {"original_count": 0, "cleaned_count": 0}}

            df = pd.DataFrame(raw_data)
            original_count = len(df)

            logger.info(f"Generic sanitization on {original_count} records (quantile={quantile})")

            # Handle timestamp column if present — preserve original Unix ms untouched.
            # Historically this round-tripped through pd.to_datetime and back with //10**6,
            # which in pandas 3.0 returns microseconds (not ns), collapsing every
            # timestamp by a factor of 10**6 (e.g. 1704067200000 → 1704067 → 1970-01-20).
            has_timestamp = "timestamp" in df.columns
            original_timestamps = None
            if has_timestamp:
                original_timestamps = df["timestamp"].copy()
                df = df.drop(columns=["timestamp"])

            # Handle symbol/string columns
            string_cols = df.select_dtypes(include=["object"]).columns.tolist()
            string_data = df[string_cols].copy() if string_cols else None
            if string_cols:
                df = df.drop(columns=string_cols)

            cleaned = _clean_numeric(df, quantile=quantile)

            # Reassemble
            if string_data is not None:
                for col in string_cols:
                    cleaned[col] = string_data[col].loc[cleaned.index].values

            if has_timestamp and original_timestamps is not None:
                # Return the original ms values untouched — no tz math, no collapse.
                cleaned["timestamp"] = original_timestamps.loc[cleaned.index].astype(np.int64)

            cleaned_count = len(cleaned)

            return {
                "data": cleaned.to_dict("records"),
                "summary": {
                    "original_count": original_count,
                    "cleaned_count": cleaned_count,
                    "removed_count": original_count - cleaned_count,
                    "removal_percentage": round(((original_count - cleaned_count) / original_count) * 100, 2)
                    if original_count > 0
                    else 0,
                    "columns_processed": list(df.columns),
                },
            }

        except Exception as e:
            logger.error(f"Error in generic sanitization: {e!s}")
            raise
