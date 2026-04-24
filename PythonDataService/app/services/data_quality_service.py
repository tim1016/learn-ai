"""Data quality pipeline: 7-step cleanup for minute OHLCV data with before/after reporting.

CSV serialization and download-token caching were removed — the data-lab
component is the single authority for generating dataset CSVs. This service
now returns only the structured before/after report.
"""

from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

from app.services.dataset_service import (
    calculate_dynamic_indicators,
    compute_warmup_start_date,
    estimate_max_lookback,
    fetch_bars_chunked,
)
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

_ET = ZoneInfo("US/Eastern")
_CORE_COLS = {"timestamp", "open", "high", "low", "close", "volume", "vwap", "transactions"}


def _compute_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Compute quality metrics for a dataframe."""
    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    dates = dt_et.dt.date

    bpd = df.groupby(dates).size()
    bpd_dist = {str(k): int(v) for k, v in bpd.value_counts().sort_index().items()}

    zero_vol = int((df["volume"] == 0).sum())
    flat_mask = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
    flat_bars = int(flat_mask.sum())
    flat_with_vol = int((flat_mask & (df["volume"] > 0)).sum())

    frac_mask = df["volume"] != df["volume"].astype("int64")
    frac_bars = int(frac_mask.sum())
    frac_dates = dates[frac_mask]
    frac_range = [str(frac_dates.min()), str(frac_dates.max())] if frac_bars > 0 else []

    vwap_hi = 0
    vwap_lo = 0
    if "vwap" in df.columns:
        vwap_hi = int((df["vwap"] > df["high"]).sum())
        vwap_lo = int((df["vwap"] < df["low"]).sum())

    ohlc_violations = int(
        (
            (df["high"] < df["open"])
            | (df["high"] < df["close"])
            | (df["low"] > df["open"])
            | (df["low"] > df["close"])
            | (df["high"] < df["low"])
        ).sum()
    )
    dupes = int(df["timestamp"].duplicated().sum())
    weekend = int((dt_et.dt.dayofweek >= 5).sum())

    # Intra-day gaps
    gap_count = 0
    for _, group in df.groupby(dates):
        ts_arr = group["timestamp"].sort_values().values
        diffs = np.diff(ts_arr)
        gap_count += int((diffs > 60000).sum())

    # Big moves
    df_copy = df.copy()
    df_copy["_date"] = dates.values
    df_copy["_pct"] = df_copy.groupby("_date")["close"].pct_change().abs() * 100
    big_1 = int((df_copy["_pct"] > 1.0).sum())
    big_2 = int((df_copy["_pct"] > 2.0).sum())

    unique_dates = sorted(dates.unique())

    return {
        "total_bars": len(df),
        "trading_days": len(unique_dates),
        "bars_per_day_distribution": bpd_dist,
        "date_range": [str(unique_dates[0]), str(unique_dates[-1])] if unique_dates else [],
        "zero_volume_bars": zero_vol,
        "flat_bars_ohlc_equal": flat_bars,
        "flat_with_volume": flat_with_vol,
        "fractional_volume_bars": frac_bars,
        "fractional_volume_date_range": frac_range,
        "vwap_above_high": vwap_hi,
        "vwap_below_low": vwap_lo,
        "ohlc_violations": ohlc_violations,
        "duplicate_timestamps": dupes,
        "weekend_bars": weekend,
        "intraday_gaps": gap_count,
        "big_moves_1pct": big_1,
        "big_moves_2pct": big_2,
    }


def step1_session_filter(df: pd.DataFrame, from_date: str, to_date: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Filter to valid NYSE RTH minutes using pandas_market_calendars."""
    bars_before = len(df)

    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.schedule(start_date=from_date, end_date=to_date)
    valid_minutes = mcal.date_range(schedule, frequency="1min")

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    # Round to minute to match calendar
    dt_rounded = dt_utc.dt.floor("min")
    mask = dt_rounded.isin(valid_minutes)
    df = df[mask].reset_index(drop=True)

    # Detect early-close days
    early_close_days = []
    for date_val, row in schedule.iterrows():
        market_open = row["market_open"]
        market_close = row["market_close"]
        session_minutes = int((market_close - market_open).total_seconds() / 60)
        if session_minutes < 390:
            early_close_days.append(str(date_val.date()))

    bars_removed = bars_before - len(df)
    logger.info(f"[DQ STEP 1] NYSE session filter: {bars_before} → {len(df)} bars ({bars_removed} removed)")

    return df, {
        "order": 1,
        "name": "NYSE Session Filter",
        "library": "pandas_market_calendars",
        "description": "Removed bars outside valid NYSE RTH minutes (handles early-close days)",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": bars_removed,
        "details": {
            "early_close_days_trimmed": len(early_close_days),
            "early_close_dates": early_close_days,
        },
    }


def step2_fix_volume(df: pd.DataFrame, method: str = "round") -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fix fractional volume values."""
    bars_before = len(df)
    frac_mask = df["volume"] != df["volume"].astype("int64")
    frac_count = int(frac_mask.sum())

    if method == "drop":
        df = df[~frac_mask].reset_index(drop=True)
    elif method == "nullify":
        df.loc[frac_mask, "volume"] = np.nan
    else:  # round (default)
        df["volume"] = df["volume"].round().astype("int64")

    bars_removed = bars_before - len(df)
    logger.info(f"[DQ STEP 2] Volume fix ({method}): {frac_count} fractional bars handled")

    return df, {
        "order": 2,
        "name": "Fractional Volume Fix",
        "library": "pandas",
        "description": f"Fixed {frac_count} fractional volume values using method: {method}",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": bars_removed,
        "details": {
            "fractional_bars_fixed": frac_count,
            "method": method,
        },
    }


def step3_recompute_vwap(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Replace vendor VWAP with session-reset VWAP."""
    bars_before = len(df)
    vwap_violations_before = 0
    if "vwap" in df.columns:
        vwap_violations_before = int(((df["vwap"] > df["high"]) | (df["vwap"] < df["low"])).sum())

    # Compute trading date in ET
    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    trading_date = dt_et.dt.date

    tp = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = tp * df["volume"]

    cum_tp_vol = tp_vol.groupby(trading_date).cumsum()
    cum_vol = df["volume"].groupby(trading_date).cumsum()

    df["vwap"] = np.where(cum_vol > 0, cum_tp_vol / cum_vol, np.nan)

    vwap_violations_after = int(((df["vwap"] > df["high"]) | (df["vwap"] < df["low"])).sum())

    logger.info(f"[DQ STEP 3] VWAP recomputed: violations {vwap_violations_before} → {vwap_violations_after}")

    return df, {
        "order": 3,
        "name": "VWAP Recomputation",
        "library": "pandas (manual TP*Vol cumsum)",
        "description": "Replaced vendor cumulative VWAP with session-reset VWAP: TP=(H+L+C)/3, cumsum(TP*V)/cumsum(V)",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": 0,
        "details": {
            "vwap_violations_before": vwap_violations_before,
            "vwap_violations_after": vwap_violations_after,
        },
    }


def step4_remove_flat_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Remove zero-volume flat bars (O=H=L=C, vol=0)."""
    bars_before = len(df)
    flat_mask = (df["volume"] == 0) & (df["open"] == df["close"]) & (df["high"] == df["low"])
    removed = int(flat_mask.sum())
    df = df[~flat_mask].reset_index(drop=True)

    logger.info(f"[DQ STEP 4] Removed {removed} zero-vol flat bars")

    return df, {
        "order": 4,
        "name": "Zero-Volume Flat Bar Removal",
        "library": "pandas",
        "description": f"Dropped {removed} bars where volume=0 AND O=H=L=C (stale price carry-forward)",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": removed,
        "details": {},
    }


def step5_ohlc_integrity(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Enforce OHLC rules, sort, deduplicate."""
    bars_before = len(df)

    df = df.sort_values("timestamp")
    dupes = int(df["timestamp"].duplicated().sum())
    df = df.drop_duplicates("timestamp").reset_index(drop=True)

    # Enforce high/low
    ohlc_cols = df[["open", "high", "low", "close"]]
    corrections = int(((df["high"] != ohlc_cols.max(axis=1)) | (df["low"] != ohlc_cols.min(axis=1))).sum())
    df["high"] = ohlc_cols.max(axis=1)
    df["low"] = ohlc_cols.min(axis=1)

    logger.info(f"[DQ STEP 5] OHLC integrity: {corrections} corrections, {dupes} dupes removed")

    return df, {
        "order": 5,
        "name": "OHLC Integrity Enforcement",
        "library": "pandas",
        "description": "Enforced high=max(O,H,L,C), low=min(O,H,L,C). Sorted by timestamp, removed duplicates.",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": dupes,
        "details": {
            "ohlc_corrections": corrections,
            "duplicates_removed": dupes,
        },
    }


def step6_normalize_tz(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Convert timestamps to NY timezone and derive trading date."""
    bars_before = len(df)

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)

    # Count EDT vs EST days
    utc_offsets = dt_et.map(lambda t: t.utcoffset().total_seconds() / 3600)
    edt_bars = int((utc_offsets == -4).sum())
    est_bars = int((utc_offsets == -5).sum())

    # Derive unique trading days per offset
    dates_edt = dt_et[utc_offsets == -4].dt.date.nunique() if edt_bars > 0 else 0
    dates_est = dt_et[utc_offsets == -5].dt.date.nunique() if est_bars > 0 else 0

    logger.info(f"[DQ STEP 6] Timezone normalized: {dates_edt} EDT days, {dates_est} EST days")

    return df, {
        "order": 6,
        "name": "Timezone Normalization",
        "library": "pandas + zoneinfo (stdlib)",
        "description": "Converted UTC timestamps to America/New_York for correct session grouping across DST boundaries",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": 0,
        "details": {
            "edt_days": dates_edt,
            "est_days": dates_est,
        },
    }


def step7_recompute_indicators(
    df: pd.DataFrame,
    indicator_entries: list[dict[str, Any]],
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    warmup_days: int = 10,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Drop all indicator columns and recompute with proper warmup."""
    bars_before = len(df)

    # Identify and drop indicator columns
    indicator_cols = [c for c in df.columns if c not in _CORE_COLS]
    dropped_count = len(indicator_cols)
    df = df.drop(columns=indicator_cols, errors="ignore")

    recomputed_count = 0
    warmup_bars_used = 0

    if indicator_entries:
        # Fetch warmup bars
        max_lookback = estimate_max_lookback(indicator_entries)
        warmup_start = compute_warmup_start_date(from_date, max_lookback)
        trim_ts = int(df["timestamp"].iloc[0])

        warmup_bars_raw = fetch_bars_chunked(polygon, ticker, warmup_start, from_date)
        warmup_bars_used = len(warmup_bars_raw)

        if warmup_bars_raw:
            warmup_df = pd.DataFrame(warmup_bars_raw)
            warmup_df = warmup_df.sort_values("timestamp").reset_index(drop=True)
            # Combine warmup + main data
            combined = pd.concat([warmup_df, df], ignore_index=True)
            combined = combined.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        else:
            combined = df.copy()

        combined, col_meta = calculate_dynamic_indicators(combined, indicator_entries)
        recomputed_count = len(col_meta)

        # Trim back to original range
        df = combined[combined["timestamp"] >= trim_ts].reset_index(drop=True)

    logger.info(
        f"[DQ STEP 7] Indicators: dropped {dropped_count}, recomputed {recomputed_count} "
        f"(warmup: {warmup_bars_used} bars)"
    )

    return df, {
        "order": 7,
        "name": "Indicator Recomputation",
        "library": "pandas-ta",
        "description": "Dropped all vendor indicator columns and recomputed from cleaned OHLCV with proper warmup",
        "bars_before": bars_before,
        "bars_after": len(df),
        "bars_removed": 0,
        "details": {
            "indicators_dropped": dropped_count,
            "indicators_recomputed": recomputed_count,
            "warmup_bars_fetched": warmup_bars_used,
            "warmup_days": warmup_days,
        },
    }


def analyze(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    volume_fix: str = "round",
    recompute_indicators: bool = True,
    indicator_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the full 7-step cleanup pipeline and return before/after report."""

    logger.info(f"[DQ] Starting analysis for {ticker}: {from_date} to {to_date}")

    # Fetch raw data
    bars = fetch_bars_chunked(polygon, ticker, from_date, to_date)
    if not bars:
        return {"error": "No bars returned from Polygon"}

    df = pd.DataFrame(bars)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Compute raw summary
    raw_summary = _compute_summary(df)

    # Run pipeline steps
    steps: list[dict[str, Any]] = []

    df, s1 = step1_session_filter(df, from_date, to_date)
    steps.append(s1)

    df, s2 = step2_fix_volume(df, volume_fix)
    steps.append(s2)

    df, s3 = step3_recompute_vwap(df)
    steps.append(s3)

    df, s4 = step4_remove_flat_bars(df)
    steps.append(s4)

    df, s5 = step5_ohlc_integrity(df)
    steps.append(s5)

    df, s6 = step6_normalize_tz(df)
    steps.append(s6)

    if recompute_indicators and indicator_entries:
        df, s7 = step7_recompute_indicators(df, indicator_entries, polygon, ticker, from_date)
        steps.append(s7)

    # Compute clean summary
    clean_summary = _compute_summary(df)
    clean_summary["indicators_recomputed"] = recompute_indicators and bool(indicator_entries)

    logger.info(f"[DQ] Analysis complete: {raw_summary['total_bars']} raw → {clean_summary['total_bars']} clean")

    return {
        "ticker": ticker,
        "from_date": from_date,
        "to_date": to_date,
        "raw_summary": raw_summary,
        "clean_summary": clean_summary,
        "steps": steps,
    }


def render_report_markdown(result: dict[str, Any]) -> bytes:
    """Serialize an ``analyze()`` result to a human-readable markdown report.

    Used by both the /generate-zip bundler and the frontend download button so
    the markdown emitted everywhere is byte-identical.
    """
    from datetime import datetime as _dt

    raw = result.get("raw_summary") or {}
    clean = result.get("clean_summary") or {}
    steps = result.get("steps") or []

    lines: list[str] = []
    lines.append(f"# Data Quality Report — {result.get('ticker', 'UNKNOWN')}")
    lines.append("")
    lines.append(
        f"**Range:** {result.get('from_date', '?')} → {result.get('to_date', '?')}  "
        f"**Generated:** {_dt.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    lines.append("")

    def _row(label: str, r_val: Any, c_val: Any) -> str:
        delta = ""
        try:
            d = int(c_val) - int(r_val)
            delta = f"{d:+d}"
        except (TypeError, ValueError):
            delta = ""
        return f"| {label} | {r_val} | {c_val} | {delta} |"

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Raw | Clean | Δ |")
    lines.append("|---|---:|---:|---:|")
    for label, key in [
        ("Total bars", "total_bars"),
        ("Trading days", "trading_days"),
        ("Zero-volume bars", "zero_volume_bars"),
        ("Flat bars (O=H=L=C)", "flat_bars_ohlc_equal"),
        ("Fractional volume bars", "fractional_volume_bars"),
        ("VWAP > high violations", "vwap_above_high"),
        ("VWAP < low violations", "vwap_below_low"),
        ("OHLC violations", "ohlc_violations"),
        ("Duplicate timestamps", "duplicate_timestamps"),
        ("Weekend bars", "weekend_bars"),
        ("Intraday gaps", "intraday_gaps"),
    ]:
        lines.append(_row(label, raw.get(key), clean.get(key)))

    lines.append("")
    lines.append("## Pipeline steps")
    lines.append("")
    for step in steps:
        lines.append(f"### {step.get('order', '?')}. {step.get('name', '')}")
        lines.append(f"*Library:* `{step.get('library', '')}`")
        lines.append("")
        lines.append(step.get("description", ""))
        lines.append("")
        lines.append(
            f"Bars: {step.get('bars_before', 0)} → {step.get('bars_after', 0)} ({step.get('bars_removed', 0)} removed)"
        )
        lines.append("")

    return "\n".join(lines).encode("utf-8")


def get_pipeline_docs() -> list[dict[str, Any]]:
    """Return documentation for each cleanup step."""
    return [
        {
            "order": 1,
            "name": "NYSE Session Filter",
            "library": "pandas_market_calendars",
            "library_url": "https://github.com/rsheftel/pandas_market_calendars",
            "problem": "Polygon returns 390 bars per day regardless of whether it was an early-close day. Half-days (Jul 3, Nov 29, Dec 24) get ~180 fabricated bars with volume=0 and stale O=H=L=C prices after market close at 13:00 ET.",
            "fix": "Build a set of valid NYSE RTH minutes using pandas_market_calendars. Inner-join the dataframe against this set to keep only bars that fall within actual trading hours.",
            "rules": [
                "Normal day: 9:30 → 16:00 ET (390 minutes)",
                "Early close: 9:30 → 13:00 ET (210 minutes)",
            ],
            "code": "nyse = mcal.get_calendar('NYSE')\nschedule = nyse.schedule(start_date, end_date)\nvalid_minutes = mcal.date_range(schedule, frequency='1min')\ndf = df[df['ts'].isin(valid_minutes)]",
            "impact": "Eliminates zero-volume fabricated bars, false flat candles after close, false end-of-day indicator bleed",
        },
        {
            "order": 2,
            "name": "Fractional Volume Fix",
            "library": "pandas",
            "library_url": None,
            "problem": "Some date ranges return decimal volume values (e.g. 142086.485768). Share volume must be integer. This likely results from Polygon's data interpolation or delayed settlement adjustments.",
            "fix": "Three options depending on strategy: (A) drop those bars entirely for volume-sensitive strategies, (B) round to nearest integer for price-only indicators, (C) set volume to NaN and recompute volume-dependent indicators only on valid data.",
            "rules": [
                "Option A (drop): df = df[df['volume'] % 1 == 0]",
                "Option B (round): df['volume'] = df['volume'].round().astype('int64')",
                "Option C (nullify): df.loc[df['volume'] % 1 != 0, 'volume'] = None",
            ],
            "code": "df['volume'] = df['volume'].round().astype('int64')  # default: round",
            "impact": "Prevents broken VWAP, OBV, MFI, CMF, ADX calculations on fractional volume",
        },
        {
            "order": 3,
            "name": "VWAP Recomputation",
            "library": "pandas (manual computation)",
            "library_url": None,
            "problem": "Polygon's VWAP is session cumulative (accumulates across the full day), not per-bar. This means VWAP can be far outside a single bar's high-low range.",
            "fix": "Recompute VWAP using typical price TP = (H+L+C)/3, then cumulative sum within each session: VWAP = cumsum(TP * Volume) / cumsum(Volume). Reset at each session open.",
            "rules": [
                "TP = (High + Low + Close) / 3",
                "VWAP = cumsum(TP * Volume) / cumsum(Volume), grouped by trading date",
                "If volume = 0, VWAP = NaN",
            ],
            "code": "tp = (df['high'] + df['low'] + df['close']) / 3\ndf['vwap'] = (tp * df['volume']).groupby(df['date']).cumsum() / df['volume'].groupby(df['date']).cumsum()",
            "impact": "VWAP violations drop from thousands to near-zero. Anchored VWAP strategies become reliable.",
            "formula_latex": r"\text{VWAP} = \frac{\sum_{i=1}^{n}(TP_i \cdot V_i)}{\sum_{i=1}^{n} V_i} \quad \text{where} \quad TP = \frac{H + L + C}{3}",
        },
        {
            "order": 4,
            "name": "Zero-Volume Flat Bar Removal",
            "library": "pandas",
            "library_url": None,
            "problem": "Bars with volume=0 and O=H=L=C are stale price carry-forwards, not real market activity. They cause false smoothing in moving averages and momentum indicators.",
            "fix": "After session filtering, drop any remaining bars where volume=0 AND open=high=low=close.",
            "rules": [
                "Drop if: volume == 0 AND open == close AND high == low",
            ],
            "code": "df = df[~((df['volume'] == 0) & (df['open'] == df['close']) & (df['high'] == df['low']))]",
            "impact": "Removes stale bars that would otherwise dilute EMA/RSI calculations with non-market data points",
        },
        {
            "order": 5,
            "name": "OHLC Integrity Enforcement",
            "library": "pandas",
            "library_url": None,
            "problem": "Vendor data can occasionally have high < open or low > close due to trade correction or aggregation bugs. Defensive enforcement ensures downstream calculations never see impossible candles.",
            "fix": "Force high = max(O,H,L,C) and low = min(O,H,L,C). Sort by timestamp and remove any duplicates.",
            "rules": [
                "high = max(open, high, low, close)",
                "low = min(open, high, low, close)",
                "Sort ascending by timestamp",
                "Drop duplicate timestamps",
            ],
            "code": "df = df.sort_values('timestamp').drop_duplicates('timestamp')\ndf['high'] = df[['open','high','low','close']].max(axis=1)\ndf['low'] = df[['open','high','low','close']].min(axis=1)",
            "impact": "Prevents rare vendor glitches from causing NaN propagation in indicators",
        },
        {
            "order": 6,
            "name": "Timezone Normalization",
            "library": "pandas + zoneinfo (stdlib)",
            "library_url": None,
            "problem": "Raw timestamps are UTC. TradingView and most trading platforms compute indicators in exchange timezone (America/New_York). UTC day boundaries differ from ET day boundaries, especially around DST transitions.",
            "fix": "Convert UTC timestamps to America/New_York, derive trading date from the localized timestamp, and use this date for session grouping (VWAP reset, daily aggregation).",
            "rules": [
                "EDT (Mar-Nov): UTC-4, market opens 13:30 UTC",
                "EST (Nov-Mar): UTC-5, market opens 14:30 UTC",
                "Trading date = New York date of bar timestamp",
            ],
            "code": "df['ts'] = pd.to_datetime(df['unix_ts'], unit='ms', utc=True)\ndf['ts_ny'] = df['ts'].dt.tz_convert('America/New_York')\ndf['date'] = df['ts_ny'].dt.date",
            "impact": "Prevents mis-grouped sessions at DST boundaries (affecting daily VWAP, first/last bar detection)",
        },
        {
            "order": 7,
            "name": "Indicator Recomputation",
            "library": "pandas-ta",
            "library_url": "https://github.com/twopirllc/pandas-ta",
            "problem": "Exported indicators have hidden warmup history — all have values from row 0, including EMA-200 which needs 200+ bars to converge. The warmup data is not visible in the export, so indicator values in the first N rows cannot be independently verified.",
            "fix": "Drop all vendor indicator columns. Fetch extra historical bars BEFORE the requested start date (warmup window). Compute indicators on the full extended dataset, then trim to the requested window.",
            "rules": [
                "EMA-200: 10 extra trading days (~3,900 minute bars)",
                "MACD (12,26,9): 5 extra trading days",
                "RSI-14 / ADX-14: 3 extra trading days",
                "Bollinger Bands (20,2): 3 extra trading days",
                "Use the maximum warmup across all requested indicators",
            ],
            "code": "indicator_cols = [c for c in df.columns if c not in CORE_COLS]\ndf = df.drop(columns=indicator_cols)\n# Fetch warmup bars, compute via pandas-ta, then slice",
            "impact": "Indicator values become verifiable and reproducible. TradingView matching improves significantly.",
        },
    ]
