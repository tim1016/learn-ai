"""
Chart service: two-layer caching, OHLCV resampling, indicator computation.

Layer 1 — Fetch + Preprocess + Resample (cached by segment)
Layer 2 — Indicator computation (cached by canonical indicator key)

Timestamps are stored/served in UTC epoch milliseconds.
Exchange timezone (US/Eastern) is used ONLY for session masking
and resample boundary alignment.
"""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date as date_type
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta
import pandas_market_calendars as mcal
from zoneinfo import ZoneInfo

from app.services.polygon_client import PolygonClientService
from app.services.dataset_service import (
    fetch_bars_chunked,
    calculate_dynamic_indicators,
    estimate_max_lookback,
    compute_warmup_start_date,
    INDICATOR_CONFIGS,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
_ET = ZoneInfo("US/Eastern")
_UTC = ZoneInfo("UTC")
_MAX_BARS = 10_000

# Timeframe definitions: (label, pandas resample rule, minutes per bar)
TIMEFRAME_DEFS: Dict[str, Dict[str, Any]] = {
    "1m":  {"rule": "1min",  "minutes": 1},
    "5m":  {"rule": "5min",  "minutes": 5},
    "15m": {"rule": "15min", "minutes": 15},
    "30m": {"rule": "30min", "minutes": 30},
    "1h":  {"rule": "1h",    "minutes": 60},
    "4h":  {"rule": "4h",    "minutes": 240},
    "1D":  {"rule": "1D",    "minutes": 390},
    "1W":  {"rule": "1W",    "minutes": 1950},
    "1M":  {"rule": "1ME",   "minutes": 8190},
}

# Indicator → panel mapping
_OVERLAY_INDICATORS = frozenset({
    "ema", "sma", "dema", "tema", "wma", "hma", "kama", "zlma", "rma", "alma",
    "bbands", "supertrend", "vwap", "psar", "kc", "donchian",
})

# Fixed color palette per indicator type
_INDICATOR_COLORS: Dict[str, str] = {
    "ema": "#2196F3",
    "sma": "#FF9800",
    "dema": "#00BCD4",
    "tema": "#4CAF50",
    "wma": "#9C27B0",
    "hma": "#E91E63",
    "bbands": "#9E9E9E",
    "supertrend": "#4CAF50",
    "vwap": "#7C3AED",
    "rsi": "#7C3AED",
    "macd": "#2196F3",
    "stoch": "#FF9800",
    "adx": "#E91E63",
    "obv": "#00BCD4",
    "cci": "#9C27B0",
    "atr": "#795548",
    "psar": "#607D8B",
}

# Reference lines for oscillators
_INDICATOR_REFS: Dict[str, List[float]] = {
    "rsi": [30.0, 70.0],
    "stoch": [20.0, 80.0],
    "cci": [-100.0, 100.0],
    "mfi": [20.0, 80.0],
}


# ──────────────────────────────────────────────
# LRU + TTL Cache
# ──────────────────────────────────────────────
@dataclass
class _CacheEntry:
    value: Any
    created_at: float = field(default_factory=time.monotonic)


class LRUTTLCache:
    """Thread-safe-ish LRU cache with TTL eviction."""

    def __init__(self, max_size: int = 128, ttl_seconds: float = 900.0) -> None:
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if (time.monotonic() - entry.created_at) > self._ttl:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return entry.value

    def put(self, key: str, value: Any) -> None:
        if key in self._store:
            del self._store[key]
        self._store[key] = _CacheEntry(value=value)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()


# Module-level caches
_resample_cache = LRUTTLCache(max_size=128, ttl_seconds=900)
_indicator_cache = LRUTTLCache(max_size=256, ttl_seconds=900)


# ──────────────────────────────────────────────
# NYSE calendar helper
# ──────────────────────────────────────────────
_nyse = mcal.get_calendar("NYSE")


def _get_trading_schedule(
    from_date: str, to_date: str
) -> pd.DataFrame:
    """Return NYSE trading schedule (market_open, market_close) for the range."""
    return _nyse.schedule(start_date=from_date, end_date=to_date)


def _count_trading_minutes(
    from_date: str, to_date: str, session: str
) -> int:
    """Count actual trading minutes using NYSE calendar."""
    schedule = _get_trading_schedule(from_date, to_date)
    if schedule.empty:
        return 0

    if session == "rth":
        total = 0
        for _, row in schedule.iterrows():
            open_t = row["market_open"]
            close_t = row["market_close"]
            total += int((close_t - open_t).total_seconds() / 60)
        return total
    else:
        # Extended: ~16 hours per trading day (04:00–20:00 ET)
        return len(schedule) * 960


def estimate_bars_per_timeframe(
    from_date: str, to_date: str, session: str
) -> Dict[str, int]:
    """Estimate bar count for each timeframe using NYSE calendar."""
    trading_mins = _count_trading_minutes(from_date, to_date, session)
    result: Dict[str, int] = {}
    for tf, tf_def in TIMEFRAME_DEFS.items():
        mins = tf_def["minutes"]
        result[tf] = max(1, trading_mins // mins) if trading_mins > 0 else 0
    return result


def get_allowed_timeframes(
    from_date: str, to_date: str, session: str
) -> Tuple[List[str], Dict[str, int], str]:
    """
    Return (allowed_timeframes, estimated_bars_per_tf, recommended_tf).
    Allowed = estimated bars <= _MAX_BARS.
    """
    estimates = estimate_bars_per_timeframe(from_date, to_date, session)
    allowed = [tf for tf, count in estimates.items() if count <= _MAX_BARS]
    if not allowed:
        allowed = ["1M"]  # always allow monthly

    # Recommended: smallest allowed timeframe (most detail)
    tf_order = list(TIMEFRAME_DEFS.keys())
    recommended = allowed[0]
    for tf in tf_order:
        if tf in allowed:
            recommended = tf
            break

    return allowed, estimates, recommended


# ──────────────────────────────────────────────
# Canonical cache key helpers
# ──────────────────────────────────────────────
def _canonical_indicator_key(indicators: List[Dict[str, Any]]) -> str:
    """Produce a stable string key from indicator specs."""
    normalized = []
    for ind in sorted(indicators, key=lambda x: x.get("name", "")):
        name = ind.get("name", "")
        params = ind.get("params", {})
        param_str = json.dumps(params, sort_keys=True, separators=(",", ":"))
        normalized.append(f"{name}:{param_str}")
    return "|".join(normalized)


def _resample_cache_key(
    ticker: str, from_date: str, to_date: str,
    timeframe: str, session: str, forward_fill: bool,
) -> str:
    return f"{ticker}|{from_date}|{to_date}|{timeframe}|{session}|{forward_fill}"


def _indicator_cache_key(resample_key: str, indicators: List[Dict[str, Any]]) -> str:
    return f"{resample_key}||{_canonical_indicator_key(indicators)}"


# ──────────────────────────────────────────────
# Data preprocessing
# ──────────────────────────────────────────────
@dataclass
class QualityReport:
    raw_bar_count: int = 0
    duplicates_removed: int = 0
    gaps_found: int = 0
    largest_gap_minutes: int = 0
    missing_sessions: int = 0
    session_coverage_pct: float = 0.0
    synthetic_bars: int = 0
    resampled_bar_count: int = 0


def _preprocess_minute_bars(
    bars: List[Dict[str, Any]],
    from_date: str,
    to_date: str,
    session: str,
    forward_fill: bool,
) -> Tuple[pd.DataFrame, QualityReport]:
    """
    Preprocess raw 1-minute bars:
    1. Sort by timestamp
    2. Drop duplicates (keep last)
    3. Validate monotonic index
    4. Session-mask using NYSE calendar
    5. Optionally forward-fill gaps
    6. Compute quality metrics
    """
    quality = QualityReport(raw_bar_count=len(bars))

    df = pd.DataFrame(bars)
    if df.empty:
        return df, quality

    # Sort + dedup
    df = df.sort_values("timestamp").reset_index(drop=True)
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    quality.duplicates_removed = before_dedup - len(df)

    # Convert to datetime for session masking (UTC internally)
    df["_dt_utc"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["_dt_et"] = df["_dt_utc"].dt.tz_convert(_ET)

    # Session mask using NYSE calendar
    schedule = _get_trading_schedule(from_date, to_date)

    if session == "rth" and not schedule.empty:
        masks = []
        for _, row in schedule.iterrows():
            open_t = row["market_open"]
            close_t = row["market_close"]
            masks.append((df["_dt_utc"] >= open_t) & (df["_dt_utc"] < close_t))
        if masks:
            combined = masks[0]
            for m in masks[1:]:
                combined = combined | m
            before_session = len(df)
            df = df[combined].reset_index(drop=True)
            logger.info(f"[SESSION] RTH filter: {before_session} → {len(df)} bars")

    # Tag session per bar (based on close timestamp)
    if not schedule.empty:
        df["session"] = "pre"  # default
        for _, row in schedule.iterrows():
            open_t = row["market_open"]
            close_t = row["market_close"]
            # RTH
            rth_mask = (df["_dt_utc"] >= open_t) & (df["_dt_utc"] < close_t)
            df.loc[rth_mask, "session"] = "rth"
            # Post-market: after close until 20:00 ET
            close_et = close_t.tz_convert(_ET)
            post_end = close_et.replace(hour=20, minute=0, second=0)
            post_end_utc = post_end.tz_convert("UTC")
            post_mask = (df["_dt_utc"] >= close_t) & (df["_dt_utc"] < post_end_utc)
            df.loc[post_mask, "session"] = "post"

    # Gap detection
    if len(df) > 1:
        diffs = df["timestamp"].diff().dropna()
        expected_gap = 60_000  # 1 minute in ms
        gaps = diffs[diffs > expected_gap * 2]  # gaps > 2 minutes
        quality.gaps_found = len(gaps)
        if not gaps.empty:
            quality.largest_gap_minutes = int(gaps.max() / 60_000)

    # Session coverage
    expected_mins = _count_trading_minutes(from_date, to_date, session)
    if expected_mins > 0:
        quality.session_coverage_pct = round(len(df) / expected_mins * 100, 1)

    # Missing sessions
    if not schedule.empty:
        trading_dates = set(schedule.index.date)
        actual_dates = set(df["_dt_et"].dt.date.unique())
        quality.missing_sessions = len(trading_dates - actual_dates)

    # Forward fill
    if forward_fill:
        before_fill = len(df)
        df = _forward_fill_bars(df, schedule, session)
        quality.synthetic_bars = len(df) - before_fill

    # Clean up helper columns (keep 'session')
    df = df.drop(columns=["_dt_utc", "_dt_et"], errors="ignore")

    return df, quality


def _forward_fill_bars(
    df: pd.DataFrame, schedule: pd.DataFrame, session: str
) -> pd.DataFrame:
    """Forward-fill missing minute bars. Synthetic bars: OHLC=prev close, volume=0."""
    if df.empty or schedule.empty:
        return df

    filled_frames = []
    df["_dt_et"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(_ET)

    for _, row in schedule.iterrows():
        open_t = row["market_open"].tz_convert(_ET)
        close_t = row["market_close"].tz_convert(_ET)

        if session == "rth":
            start = open_t
            end = close_t
        else:
            start = open_t.replace(hour=4, minute=0)
            end = close_t.replace(hour=20, minute=0)

        minute_range = pd.date_range(start=start, end=end - timedelta(minutes=1), freq="min", tz=_ET)
        _epoch = pd.Timestamp("1970-01-01", tz="UTC")
        minute_ts = ((minute_range.tz_convert("UTC") - _epoch).total_seconds() * 1000).astype("int64")

        day_mask = (df["_dt_et"].dt.date == open_t.date())
        day_df = df[day_mask].copy()

        template = pd.DataFrame({"timestamp": minute_ts})
        merged = template.merge(
            day_df.drop(columns=["_dt_et"], errors="ignore"),
            on="timestamp",
            how="left",
        )

        # Mark synthetic
        merged["synthetic"] = merged["close"].isna()

        # Forward fill OHLC
        merged["close"] = merged["close"].ffill()
        for col in ["open", "high", "low"]:
            merged[col] = merged[col].fillna(merged["close"])
        merged["volume"] = merged["volume"].fillna(0)
        if "transactions" in merged.columns:
            merged["transactions"] = merged["transactions"].fillna(0)
        if "vwap" in merged.columns:
            merged["vwap"] = merged["vwap"].ffill()
        if "session" in merged.columns:
            merged["session"] = merged["session"].ffill().bfill()

        filled_frames.append(merged)

    if not filled_frames:
        return df.drop(columns=["_dt_et"], errors="ignore")

    result = pd.concat(filled_frames, ignore_index=True)
    result = result.sort_values("timestamp").reset_index(drop=True)
    result = result.drop(columns=["_dt_et"], errors="ignore")
    return result


# ──────────────────────────────────────────────
# OHLCV Resampling
# ──────────────────────────────────────────────
def _resample_bars(
    df: pd.DataFrame, timeframe: str, session: str
) -> pd.DataFrame:
    """
    Resample 1-minute bars to target timeframe.

    Anchor rules:
    - RTH intraday: anchored to 9:30 ET (NYSE session start)
    - ETH intraday: anchored to 4:00 ET (pre-market start)
    - Daily+: standard calendar alignment
    """
    if timeframe == "1m":
        return df.copy()

    tf_def = TIMEFRAME_DEFS.get(timeframe)
    if tf_def is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")

    rule = tf_def["rule"]

    # Convert timestamp to datetime index in ET for correct alignment
    df = df.copy()
    df["_dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(_ET)
    df = df.set_index("_dt")

    # Determine resample offset for session-start anchoring
    offset = None
    minutes = tf_def["minutes"]
    if minutes < 390:  # intraday
        if session == "rth":
            # NYSE opens at 9:30 — 30 min offset from top-of-hour
            offset = timedelta(minutes=30)
        else:
            # Pre-market starts at 4:00 — no offset needed
            offset = timedelta(minutes=0)

    agg_dict: Dict[str, Any] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if "vwap" in df.columns:
        agg_dict["vwap"] = "last"
    if "transactions" in df.columns:
        agg_dict["transactions"] = "sum"

    # Carry session tag from last bar in group
    has_session = "session" in df.columns
    if has_session:
        agg_dict["session"] = "last"

    # Carry synthetic flag (True if any bar in group is synthetic)
    has_synthetic = "synthetic" in df.columns
    if has_synthetic:
        agg_dict["synthetic"] = "any"

    if offset is not None:
        resampled = df.resample(rule, offset=offset).agg(agg_dict)
    else:
        resampled = df.resample(rule).agg(agg_dict)

    # Drop empty bars (weekends, holidays)
    resampled = resampled.dropna(subset=["open"])

    # Convert back to UTC epoch ms timestamps
    _epoch = pd.Timestamp("1970-01-01", tz="UTC")
    resampled["timestamp"] = (
        (resampled.index.tz_convert("UTC") - _epoch).total_seconds() * 1000
    ).astype("int64")

    resampled = resampled.reset_index(drop=True)
    return resampled


# ──────────────────────────────────────────────
# Indicator computation on resampled bars
# ──────────────────────────────────────────────
def _compute_indicators(
    df: pd.DataFrame,
    indicators: List[Dict[str, Any]],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """
    Compute indicators on resampled OHLCV DataFrame.
    Returns (enriched_df, column_meta).
    Reuses dataset_service.calculate_dynamic_indicators.
    """
    if not indicators:
        return df, []
    return calculate_dynamic_indicators(df.copy(), indicators)


# ──────────────────────────────────────────────
# Indicator result formatting
# ──────────────────────────────────────────────
def _format_indicator_results(
    df: pd.DataFrame,
    column_meta: List[Dict[str, Any]],
    indicators_requested: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Format indicator columns into the structured response schema.
    Each indicator becomes an entry with id, panel, type, color, data, and optional refs.
    """
    results: List[Dict[str, Any]] = []

    # Group column_meta by indicator name + params
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for meta in column_meta:
        key = f"{meta['indicator']}|{meta['params']}"
        grouped.setdefault(key, []).append(meta)

    for ind_spec in indicators_requested:
        name = ind_spec.get("name", "")
        params = ind_spec.get("params", {})
        param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "default"
        group_key = f"{name}|{param_str}"
        cols = grouped.get(group_key, [])

        if not cols:
            continue

        # Determine panel
        panel = "main" if name in _OVERLAY_INDICATORS else name
        base_color = _INDICATOR_COLORS.get(name, "#607D8B")
        refs = _INDICATOR_REFS.get(name, [])

        # Build param suffix for ID
        if params:
            param_suffix = "_".join(str(v) for v in params.values())
            ind_id = f"{name}_{param_suffix}"
        else:
            ind_id = name

        # Handle multi-series indicators
        if name == "macd":
            macd_data = {"macd": [], "signal": [], "histogram": []}
            timestamps = df["timestamp"].tolist()
            for meta in cols:
                col = meta["column"]
                values = df[col].tolist()
                if "macdh" in col or "histogram" in col:
                    series_key = "histogram"
                elif "macds" in col or "signal" in col:
                    series_key = "signal"
                else:
                    series_key = "macd"
                macd_data[series_key] = [
                    {"t": int(t), "value": None if pd.isna(v) else round(float(v), 6)}
                    for t, v in zip(timestamps, values)
                ]
            results.append({
                "id": ind_id,
                "panel": "macd",
                "type": "macd",
                "color": base_color,
                "data": macd_data,
                "refs": [],
            })

        elif name == "bbands":
            # Upper, middle, lower as separate series
            timestamps = df["timestamp"].tolist()
            for meta in cols:
                col = meta["column"]
                values = df[col].tolist()
                series_data = [
                    {"t": int(t), "value": None if pd.isna(v) else round(float(v), 6)}
                    for t, v in zip(timestamps, values)
                ]
                if "bbu" in col:
                    results.append({"id": f"{ind_id}_upper", "panel": "main", "type": "line", "color": "#BDBDBD", "data": series_data, "refs": []})
                elif "bbm" in col:
                    results.append({"id": f"{ind_id}_middle", "panel": "main", "type": "line", "color": "#9E9E9E", "data": series_data, "refs": []})
                elif "bbl" in col:
                    results.append({"id": f"{ind_id}_lower", "panel": "main", "type": "line", "color": "#BDBDBD", "data": series_data, "refs": []})

        elif name == "supertrend":
            timestamps = df["timestamp"].tolist()
            for meta in cols:
                col = meta["column"]
                values = df[col].tolist()
                series_data = [
                    {"t": int(t), "value": None if pd.isna(v) else round(float(v), 6)}
                    for t, v in zip(timestamps, values)
                ]
                if "supertl" in col:
                    results.append({"id": f"{ind_id}_up", "panel": "main", "type": "line", "color": "#4CAF50", "data": series_data, "refs": []})
                elif "superts" in col:
                    results.append({"id": f"{ind_id}_down", "panel": "main", "type": "line", "color": "#F44336", "data": series_data, "refs": []})

        elif name == "stoch":
            timestamps = df["timestamp"].tolist()
            for meta in cols:
                col = meta["column"]
                values = df[col].tolist()
                series_data = [
                    {"t": int(t), "value": None if pd.isna(v) else round(float(v), 6)}
                    for t, v in zip(timestamps, values)
                ]
                if "stochk" in col:
                    results.append({"id": f"{ind_id}_k", "panel": "stoch", "type": "line", "color": "#2196F3", "data": series_data, "refs": [20.0, 80.0]})
                elif "stochd" in col:
                    results.append({"id": f"{ind_id}_d", "panel": "stoch", "type": "line", "color": "#FF9800", "data": series_data, "refs": []})

        elif name == "adx":
            timestamps = df["timestamp"].tolist()
            for meta in cols:
                col = meta["column"]
                values = df[col].tolist()
                series_data = [
                    {"t": int(t), "value": None if pd.isna(v) else round(float(v), 6)}
                    for t, v in zip(timestamps, values)
                ]
                if "dmp" in col:
                    results.append({"id": f"{ind_id}_di_plus", "panel": "adx", "type": "line", "color": "#4CAF50", "data": series_data, "refs": []})
                elif "dmn" in col:
                    results.append({"id": f"{ind_id}_di_minus", "panel": "adx", "type": "line", "color": "#F44336", "data": series_data, "refs": []})
                else:
                    results.append({"id": ind_id, "panel": "adx", "type": "line", "color": "#7C3AED", "data": series_data, "refs": [25.0]})

        else:
            # Single-series indicator (RSI, OBV, EMA, SMA, etc.)
            meta = cols[0]
            col = meta["column"]
            timestamps = df["timestamp"].tolist()
            values = df[col].tolist()
            series_data = [
                {"t": int(t), "value": None if pd.isna(v) else round(float(v), 6)}
                for t, v in zip(timestamps, values)
            ]
            results.append({
                "id": ind_id,
                "panel": panel,
                "type": "line",
                "color": base_color,
                "data": series_data,
                "refs": refs,
            })

    return results


# ──────────────────────────────────────────────
# Main public API
# ──────────────────────────────────────────────
_polygon = PolygonClientService()


def get_chart_data(
    ticker: str,
    from_date: str,
    to_date: str,
    timeframe: str,
    session: str = "rth",
    forward_fill: bool = False,
    indicators: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Main entry point: fetch 1m bars, preprocess, resample, compute indicators.
    Uses two-layer caching.
    """
    indicators = indicators or []

    # Validate timeframe
    if timeframe not in TIMEFRAME_DEFS:
        return {
            "error_code": "INVALID_RANGE",
            "detail": f"Unknown timeframe: {timeframe}",
        }

    # Check allowed timeframes
    allowed, estimates, recommended = get_allowed_timeframes(from_date, to_date, session)
    if timeframe not in allowed:
        return {
            "error_code": "TIMEFRAME_NOT_ALLOWED",
            "detail": f"Timeframe '{timeframe}' would produce ~{estimates.get(timeframe, 0)} bars (max {_MAX_BARS}).",
            "allowed_timeframes": allowed,
            "estimated_bars_per_timeframe": estimates,
            "recommended_timeframe": recommended,
        }

    # ── Layer 1: Fetch + Preprocess + Resample (cached) ──
    resample_key = _resample_cache_key(ticker, from_date, to_date, timeframe, session, forward_fill)
    cached_resample = _resample_cache.get(resample_key)

    if cached_resample is not None:
        df_resampled, quality = cached_resample
        logger.info(f"[CHART] Cache HIT for resample: {resample_key}")
        cache_hit_resample = True
    else:
        logger.info(f"[CHART] Cache MISS for resample: {resample_key}")
        cache_hit_resample = False

        # Fetch 1m bars with warmup for indicators
        fetch_from = from_date
        if indicators:
            max_lookback = estimate_max_lookback(indicators)
            fetch_from = compute_warmup_start_date(from_date, max_lookback)
            logger.info(f"[CHART] Warmup: fetching from {fetch_from} (requested {from_date})")

        bars = fetch_bars_chunked(_polygon, ticker, fetch_from, to_date)
        if not bars:
            return {
                "error_code": "NO_DATA",
                "detail": f"No data returned for {ticker} from {from_date} to {to_date}",
            }

        # Preprocess
        df_preprocessed, quality = _preprocess_minute_bars(
            bars, from_date, to_date, session, forward_fill
        )
        if df_preprocessed.empty:
            return {
                "error_code": "NO_DATA",
                "detail": f"No bars after preprocessing for {ticker}",
            }

        # Resample
        df_resampled = _resample_bars(df_preprocessed, timeframe, session)

        # Trim warmup bars
        if indicators:
            trim_ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp() * 1000)
            df_resampled = df_resampled[df_resampled["timestamp"] >= trim_ts].reset_index(drop=True)

        quality.resampled_bar_count = len(df_resampled)

        # Confirm actual count ≤ max
        if len(df_resampled) > _MAX_BARS:
            return {
                "error_code": "TIMEFRAME_NOT_ALLOWED",
                "detail": f"Actual bar count ({len(df_resampled)}) exceeds max ({_MAX_BARS}) after resample.",
                "allowed_timeframes": allowed,
                "estimated_bars_per_timeframe": estimates,
                "recommended_timeframe": recommended,
            }

        _resample_cache.put(resample_key, (df_resampled.copy(), quality))

    # ── Layer 2: Indicator computation (cached) ──
    indicator_results: List[Dict[str, Any]] = []
    cache_hit_indicators = False

    if indicators:
        ind_key = _indicator_cache_key(resample_key, indicators)
        cached_ind = _indicator_cache.get(ind_key)

        if cached_ind is not None:
            indicator_results = cached_ind
            cache_hit_indicators = True
            logger.info(f"[CHART] Cache HIT for indicators")
        else:
            logger.info(f"[CHART] Cache MISS for indicators, computing...")
            df_with_ind, col_meta = _compute_indicators(df_resampled, indicators)
            indicator_results = _format_indicator_results(df_with_ind, col_meta, indicators)
            _indicator_cache.put(ind_key, indicator_results)

    # ── Build response ──
    bars_out = []
    for _, row in df_resampled.iterrows():
        bar: Dict[str, Any] = {
            "t": int(row["timestamp"]),
            "o": round(float(row["open"]), 6) if pd.notna(row["open"]) else None,
            "h": round(float(row["high"]), 6) if pd.notna(row["high"]) else None,
            "l": round(float(row["low"]), 6) if pd.notna(row["low"]) else None,
            "c": round(float(row["close"]), 6) if pd.notna(row["close"]) else None,
            "v": round(float(row["volume"]), 2) if pd.notna(row.get("volume", None)) else 0,
        }
        if "session" in row and pd.notna(row["session"]):
            bar["session"] = row["session"]
        if "synthetic" in row and row.get("synthetic"):
            bar["synthetic"] = True
        bars_out.append(bar)

    return {
        "bars": bars_out,
        "indicators": indicator_results,
        "quality": {
            "raw_bar_count": quality.raw_bar_count,
            "resampled_bar_count": quality.resampled_bar_count,
            "duplicates_removed": quality.duplicates_removed,
            "gaps_found": quality.gaps_found,
            "largest_gap_minutes": quality.largest_gap_minutes,
            "missing_sessions": quality.missing_sessions,
            "session_coverage_pct": quality.session_coverage_pct,
            "synthetic_bars": quality.synthetic_bars,
        },
        "allowed_timeframes": allowed,
        "estimated_bars_per_timeframe": estimates,
        "recommended_timeframe": recommended,
        "meta": {
            "cached_resample": cache_hit_resample,
            "cached_indicators": cache_hit_indicators,
        },
    }
