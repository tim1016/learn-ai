"""Dataset generation service: chunked OHLCV fetch + dynamic pandas-ta indicator calculation"""
from __future__ import annotations

import io
import csv
import inspect
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pandas_ta as ta

from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

from zoneinfo import ZoneInfo

_POLYGON_MAX_BARS = 50_000
_MINUTES_PER_DAY = 450
_DAYS_PER_CHUNK = _POLYGON_MAX_BARS // _MINUTES_PER_DAY
_WARMUP_MULTIPLIER = 5

_ET = ZoneInfo("US/Eastern")
_RTH_START_HOUR, _RTH_START_MIN = 9, 30
_RTH_END_HOUR, _RTH_END_MIN = 16, 0

# Default indicator configurations matching TradingView standard setup
DEFAULT_INDICATORS: List[Dict[str, Any]] = [
    {"name": "ema", "params": {"length": 5}},
    {"name": "ema", "params": {"length": 10}},
    {"name": "ema", "params": {"length": 20}},
    {"name": "ema", "params": {"length": 30}},
    {"name": "ema", "params": {"length": 40}},
    {"name": "ema", "params": {"length": 50}},
    {"name": "ema", "params": {"length": 100}},
    {"name": "ema", "params": {"length": 200}},
    {"name": "bbands", "params": {"length": 20, "std": 2.0}},
    {"name": "supertrend", "params": {"length": 10, "multiplier": 3.0}},
    {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
]

# Configurable parameters for key indicators
INDICATOR_CONFIGS: Dict[str, List[Dict[str, Any]]] = {
    "ema": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "sma": [{"name": "length", "type": "int", "default": 20, "min": 1, "max": 500, "description": "Lookback period"}],
    "dema": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "tema": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "wma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "hma": [{"name": "length", "type": "int", "default": 9, "min": 1, "max": 500, "description": "Lookback period"}],
    "kama": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "zlma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "rma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "alma": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 500, "description": "Lookback period"}],
    "bbands": [
        {"name": "length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "SMA lookback period"},
        {"name": "std", "type": "float", "default": 2.0, "min": 0.1, "max": 5.0, "description": "Standard deviations"},
    ],
    "supertrend": [
        {"name": "length", "type": "int", "default": 10, "min": 1, "max": 100, "description": "ATR period"},
        {"name": "multiplier", "type": "float", "default": 3.0, "min": 0.5, "max": 10.0, "description": "ATR multiplier"},
    ],
    "rsi": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "macd": [
        {"name": "fast", "type": "int", "default": 12, "min": 1, "max": 100, "description": "Fast EMA period"},
        {"name": "slow", "type": "int", "default": 26, "min": 1, "max": 200, "description": "Slow EMA period"},
        {"name": "signal", "type": "int", "default": 9, "min": 1, "max": 50, "description": "Signal EMA period"},
    ],
    "adx": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "DI/ADX smoothing period"}],
    "atr": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "ATR period"}],
    "stoch": [
        {"name": "k", "type": "int", "default": 14, "min": 1, "max": 100, "description": "%K lookback period"},
        {"name": "d", "type": "int", "default": 3, "min": 1, "max": 50, "description": "%D smoothing period"},
    ],
    "stochrsi": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "RSI lookback period"}],
    "cci": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "willr": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "roc": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 100, "description": "Lookback period"}],
    "mom": [{"name": "length", "type": "int", "default": 10, "min": 1, "max": 100, "description": "Lookback period"}],
    "donchian": [
        {"name": "lower_length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "Lower channel period"},
        {"name": "upper_length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "Upper channel period"},
    ],
    "kc": [
        {"name": "length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "EMA period"},
        {"name": "scalar", "type": "float", "default": 1.5, "min": 0.5, "max": 5.0, "description": "ATR multiplier"},
    ],
    "psar": [
        {"name": "af0", "type": "float", "default": 0.02, "min": 0.001, "max": 0.1, "description": "Initial acceleration factor"},
        {"name": "af", "type": "float", "default": 0.02, "min": 0.001, "max": 0.1, "description": "Acceleration factor step"},
        {"name": "max_af", "type": "float", "default": 0.2, "min": 0.05, "max": 1.0, "description": "Maximum acceleration factor"},
    ],
    "aroon": [{"name": "length", "type": "int", "default": 25, "min": 1, "max": 100, "description": "Lookback period"}],
    "natr": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "ATR period"}],
    "obv": [],
    "ad": [],
    "cmf": [{"name": "length", "type": "int", "default": 20, "min": 1, "max": 100, "description": "Lookback period"}],
    "mfi": [{"name": "length", "type": "int", "default": 14, "min": 1, "max": 100, "description": "Lookback period"}],
    "vwap": [],
    "tsi": [
        {"name": "fast", "type": "int", "default": 13, "min": 1, "max": 100, "description": "Fast period"},
        {"name": "slow", "type": "int", "default": 25, "min": 1, "max": 200, "description": "Slow period"},
    ],
    "fisher": [{"name": "length", "type": "int", "default": 9, "min": 1, "max": 100, "description": "Lookback period"}],
    "squeeze": [
        {"name": "bb_length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "Bollinger Bands period"},
        {"name": "kc_length", "type": "int", "default": 20, "min": 1, "max": 200, "description": "Keltner Channel period"},
    ],
}


def get_indicator_configs() -> Dict[str, List[Dict[str, Any]]]:
    """Return configurable parameters for each indicator."""
    return INDICATOR_CONFIGS


def list_available_indicators() -> Dict[str, List[Dict[str, str]]]:
    """Return all pandas-ta indicators grouped by category with descriptions."""
    categories: Dict[str, List[Dict[str, str]]] = {}
    for cat_name, indicator_names in ta.Category.items():
        items = []
        for name in sorted(indicator_names):
            fn = getattr(ta, name, None)
            doc = ""
            if fn is not None:
                raw_doc = inspect.getdoc(fn) or ""
                for line in raw_doc.split("\n"):
                    stripped = line.strip()
                    if stripped:
                        doc = stripped
                        break
            items.append({"name": name, "category": cat_name, "description": doc})
        categories[cat_name] = items
    return categories


def fetch_bars_chunked(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    timespan: str = "minute",
    multiplier: int = 1,
) -> List[Dict[str, Any]]:
    """Fetch OHLCV bars for a long date range by splitting into chunks."""
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")

    # Dynamic chunk size based on timespan and multiplier
    _bars_per_day = {"minute": 450, "hour": 24, "day": 1}
    effective_bpd = _bars_per_day.get(timespan, 450) // max(1, multiplier)
    days_per_chunk = max(1, _POLYGON_MAX_BARS // max(1, effective_bpd))

    all_bars: List[Dict[str, Any]] = []
    chunk_start = start
    chunk_idx = 0

    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=days_per_chunk), end)
        chunk_idx += 1
        logger.info(
            f"[CHUNK {chunk_idx}] Fetching {multiplier}{timespan} bars for {ticker}: "
            f"{chunk_start.strftime('%Y-%m-%d')} to {chunk_end.strftime('%Y-%m-%d')}"
        )
        bars = polygon.fetch_aggregates(
            ticker=ticker, multiplier=multiplier, timespan=timespan,
            from_date=chunk_start.strftime("%Y-%m-%d"),
            to_date=chunk_end.strftime("%Y-%m-%d"),
        )
        all_bars.extend(bars)
        logger.info(f"[CHUNK {chunk_idx}] Got {len(bars)} bars (total: {len(all_bars)})")
        chunk_start = chunk_end + timedelta(days=1)

    seen: set[int] = set()
    unique_bars: List[Dict[str, Any]] = []
    for bar in all_bars:
        ts = bar["timestamp"]
        if ts not in seen:
            seen.add(ts)
            unique_bars.append(bar)
    unique_bars.sort(key=lambda b: b["timestamp"])
    logger.info(f"Total unique {multiplier}{timespan} bars: {len(unique_bars)}")
    return unique_bars


def filter_session(df: pd.DataFrame, session: str) -> pd.DataFrame:
    """Filter bars to RTH (09:30-16:00 ET) or keep all (extended)."""
    if session != "rth":
        return df

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)

    # RTH: 09:30 <= time < 16:00 ET, weekdays only
    time_minutes = dt_et.dt.hour * 60 + dt_et.dt.minute
    rth_start = _RTH_START_HOUR * 60 + _RTH_START_MIN  # 570
    rth_end = _RTH_END_HOUR * 60 + _RTH_END_MIN  # 960

    mask = (
        (time_minutes >= rth_start)
        & (time_minutes < rth_end)
        & (dt_et.dt.dayofweek < 5)  # Mon-Fri
    )
    before = len(df)
    df = df[mask].reset_index(drop=True)
    logger.info(f"[SESSION] RTH filter: {before} → {len(df)} bars")
    return df


def forward_fill_gaps(df: pd.DataFrame, session: str) -> pd.DataFrame:
    """
    Build a continuous minute grid and forward-fill missing bars.
    Missing bars get: open=high=low=close=prev_close, volume=0, transactions=0.
    """
    if df.empty:
        return df

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_et = dt_utc.dt.tz_convert(_ET)
    df["_dt_et"] = dt_et

    # Group by trading date and build continuous minute ranges
    filled_frames = []
    for date, group in df.groupby(dt_et.dt.date):
        day_dt = pd.Timestamp(date)
        if day_dt.dayofweek >= 5:
            continue  # skip weekends

        if session == "rth":
            start = datetime.combine(date, datetime.min.time().replace(hour=_RTH_START_HOUR, minute=_RTH_START_MIN, tzinfo=_ET))
            end = datetime.combine(date, datetime.min.time().replace(hour=_RTH_END_HOUR, minute=0, tzinfo=_ET))
        else:
            # Extended: 04:00 - 20:00 ET (typical Polygon range)
            first_bar = group["_dt_et"].iloc[0]
            last_bar = group["_dt_et"].iloc[-1]
            start = first_bar.floor("min")
            end = last_bar.ceil("min") + timedelta(minutes=1)

        minute_range = pd.date_range(start=start, end=end - timedelta(minutes=1), freq="min", tz=_ET)
        # Convert to milliseconds since epoch (pandas 3.0 uses variable resolution,
        # so .astype("int64") may return µs not ns — use total_seconds() for safety)
        _epoch = pd.Timestamp("1970-01-01", tz="UTC")
        minute_ts = ((minute_range.tz_convert("UTC") - _epoch).total_seconds() * 1000).astype("int64")

        template = pd.DataFrame({"timestamp": minute_ts})
        merged = template.merge(group.drop(columns=["_dt_et"], errors="ignore"), on="timestamp", how="left")

        # Forward-fill OHLC with previous close
        merged["close"] = merged["close"].ffill()
        for col in ["open", "high", "low"]:
            merged[col] = merged[col].fillna(merged["close"])
        merged["volume"] = merged["volume"].fillna(0)
        merged["transactions"] = merged["transactions"].fillna(0)
        if "vwap" in merged.columns:
            merged["vwap"] = merged["vwap"].ffill()

        filled_frames.append(merged)

    if not filled_frames:
        return df.drop(columns=["_dt_et"], errors="ignore")

    result = pd.concat(filled_frames, ignore_index=True)
    result = result.sort_values("timestamp").reset_index(drop=True)

    # Remove any remaining _dt_et column
    result = result.drop(columns=["_dt_et"], errors="ignore")

    logger.info(f"[FILL] Forward-filled: {len(df)} → {len(result)} bars")
    return result


def compute_warmup_start_date(
    from_date: str,
    max_lookback: int,
    timespan: str = "minute",
    multiplier: int = 1,
) -> str:
    """Step back from from_date enough calendar days to warm up indicators."""
    warmup_bars = max_lookback * _WARMUP_MULTIPLIER
    bars_per_day = {"minute": 390, "hour": 7, "day": 1}
    bpd = bars_per_day.get(timespan, 390) * multiplier
    warmup_days = max(1, (warmup_bars // bpd) + 2)
    return (
        datetime.strptime(from_date, "%Y-%m-%d") - timedelta(days=warmup_days)
    ).strftime("%Y-%m-%d")


def estimate_max_lookback(indicator_entries: List[Dict[str, Any]]) -> int:
    """Scan indicator_entries for the largest lookback parameter."""
    lookback = 0
    for entry in indicator_entries:
        params = entry.get("params", {})
        for key in ("length", "slow", "k", "bb_length", "kc_length",
                     "lower_length", "upper_length"):
            val = params.get(key, 0)
            if isinstance(val, (int, float)):
                lookback = max(lookback, int(val))
    return max(lookback, 200)


def indicator_table_params_to_entries(
    ema_periods: List[int],
    bb_length: int = 20,
    bb_std: float = 2.0,
    supertrend_length: int = 10,
    supertrend_multiplier: float = 3.0,
    rsi_length: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    adx_length: int = 14,
) -> List[Dict[str, Any]]:
    """Convert fixed indicator-table params into dynamic indicator_entries."""
    entries: List[Dict[str, Any]] = []
    for period in sorted(ema_periods):
        entries.append({"name": "ema", "params": {"length": period}})
    entries.append({"name": "bbands", "params": {"length": bb_length, "std": bb_std}})
    entries.append({"name": "supertrend", "params": {"length": supertrend_length, "multiplier": supertrend_multiplier}})
    entries.append({"name": "rsi", "params": {"length": rsi_length}})
    entries.append({"name": "macd", "params": {"fast": macd_fast, "slow": macd_slow, "signal": macd_signal}})
    entries.append({"name": "adx", "params": {"length": adx_length}})
    return entries


def preprocess_and_calculate(
    bars: List[Dict[str, Any]],
    indicator_entries: List[Dict[str, Any]],
    session: str = "extended",
    forward_fill: bool = False,
    trim_from_ts: Optional[int] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """
    Shared preprocessing pipeline:
      1. Sort and deduplicate bars
      2. Session filter (RTH or extended)
      3. Forward-fill gaps (optional)
      4. Calculate dynamic indicators
      5. Trim warm-up rows (optional, by timestamp)
    """
    df = pd.DataFrame(bars)
    df = df.sort_values("timestamp").reset_index(drop=True)

    df = filter_session(df, session)

    if forward_fill:
        df = forward_fill_gaps(df, session)

    column_meta: List[Dict[str, Any]] = []
    if indicator_entries:
        df, column_meta = calculate_dynamic_indicators(df, indicator_entries)

    if trim_from_ts is not None:
        before = len(df)
        df = df[df["timestamp"] >= trim_from_ts].reset_index(drop=True)
        logger.info(f"[TRIM] Warm-up trimmed: {before} → {len(df)} rows")

    return df, column_meta


def rename_to_indicator_table_columns(
    df: pd.DataFrame,
    column_meta: List[Dict[str, Any]],
) -> pd.DataFrame:
    """Rename pandas-ta raw column names to the indicator-table API contract."""
    rename_map: Dict[str, str] = {}
    for m in column_meta:
        col = m["column"]
        ind = m["indicator"]
        if ind == "bbands":
            if col.startswith("bbl"):
                rename_map[col] = "bb_lower"
            elif col.startswith("bbm"):
                rename_map[col] = "bb_basis"
            elif col.startswith("bbu"):
                rename_map[col] = "bb_upper"
        elif ind == "supertrend":
            if col.startswith("supertl"):
                rename_map[col] = "supertrend_up"
            elif col.startswith("superts"):
                rename_map[col] = "supertrend_down"
        elif ind == "rsi":
            rename_map[col] = "rsi"
        elif ind == "macd":
            if col.startswith("macdh"):
                rename_map[col] = "macd_histogram"
            elif col.startswith("macds"):
                rename_map[col] = "macd_signal"
            else:
                rename_map[col] = "macd"
        elif ind == "adx":
            if "dmp" not in col and "dmn" not in col:
                rename_map[col] = "adx"
        elif ind == "ema":
            # calculate_dynamic_indicators produces "ema_length5" → rename to "ema_5"
            length = m.get("params", "").replace("length=", "")
            if length:
                rename_map[col] = f"ema_{length}"

    # Drop columns not in the indicator-table contract (bbb, bbp, supert, supertd, dmp, dmn)
    keep_cols = set(rename_map.values()) | {"timestamp", "open", "high", "low", "close", "volume"}
    df = df.rename(columns=rename_map)
    drop_cols = [c for c in df.columns if c not in keep_cols]
    df = df.drop(columns=drop_cols, errors="ignore")
    return df


def calculate_dynamic_indicators(
    df: pd.DataFrame,
    indicator_entries: List[Dict[str, Any]],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """
    Calculate selected pandas-ta indicators on a DataFrame with OHLCV columns.
    indicator_entries: list of {"name": "ema", "params": {"length": 20}}
    Returns the enriched DataFrame and column metadata list.
    """
    column_meta: List[Dict[str, Any]] = []

    for entry in indicator_entries:
        ind_name = entry.get("name", "")
        params = entry.get("params", {})

        fn = getattr(ta, ind_name, None)
        if fn is None:
            logger.warning(f"Unknown indicator: {ind_name}, skipping")
            continue

        try:
            sig = inspect.signature(fn)
            param_names = list(sig.parameters.keys())

            args: list[Any] = []
            if _needs_param(param_names, "high", "low", "close"):
                args = [df["high"], df["low"], df["close"]]
            elif _needs_param(param_names, "high", "low"):
                args = [df["high"], df["low"]]
            elif _needs_param(param_names, "close"):
                args = [df["close"]]
            elif _needs_param(param_names, "open_"):
                args = [df["open"], df["high"], df["low"], df["close"]]
            elif _needs_param(param_names, "volume"):
                args = [df["volume"]]

            kwargs: Dict[str, Any] = {**params}
            if "volume" in param_names and "volume" not in [p for p in param_names[:len(args)]]:
                if len(args) > 0 and "volume" not in kwargs:
                    kwargs["volume"] = df["volume"]

            result = fn(*args, **kwargs)
            if result is None:
                logger.warning(f"Indicator {ind_name} returned None")
                continue

            param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "default"

            if isinstance(result, pd.DataFrame):
                for col in result.columns:
                    clean = col.lower().replace(" ", "_")
                    df[clean] = result[col]
                    column_meta.append({
                        "column": clean,
                        "indicator": ind_name,
                        "params": param_str,
                        "library": "pandas-ta",
                    })
            elif isinstance(result, pd.Series):
                col_name = f"{ind_name}_{param_str.replace(', ', '_').replace('=', '')}" if params else ind_name
                col_name = col_name.lower().replace(" ", "_")
                df[col_name] = result
                column_meta.append({
                    "column": col_name,
                    "indicator": ind_name,
                    "params": param_str,
                    "library": "pandas-ta",
                })

            logger.info(f"Calculated {ind_name}({param_str}): +{len(result.columns) if isinstance(result, pd.DataFrame) else 1} columns")

        except Exception as e:
            logger.error(f"Error calculating {ind_name}: {e}", exc_info=True)
            continue

    return df, column_meta


def build_csv_bytes(df: pd.DataFrame, columns: List[str]) -> bytes:
    """Serialize a DataFrame to CSV bytes with unix_ts and iso_time columns first."""
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["unix_ts", "iso_time"] + columns
    writer.writerow(header)

    for _, row in df.iterrows():
        ts = int(row["timestamp"])
        iso = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
        values = [ts, iso] + [_fmt(row.get(col)) for col in columns]
        writer.writerow(values)
    return output.getvalue().encode("utf-8")


def build_metadata_json(
    ticker: str,
    from_date: str,
    to_date: str,
    bar_count: int,
    column_meta: List[Dict[str, Any]],
    ohlcv_cols: List[str],
    session: str = "extended",
    forward_fill: bool = True,
    raw_bar_count: int = 0,
    filled_bar_count: int = 0,
) -> bytes:
    """Generate CSV metadata JSON describing every column and its calculation."""
    base_columns = [
        {"column": "unix_ts", "type": "int", "description": "Unix timestamp in milliseconds (UTC)", "source": "Polygon.io"},
        {"column": "iso_time", "type": "string", "description": "ISO 8601 datetime (UTC)", "source": "Derived from unix_ts"},
    ]
    for col in ohlcv_cols:
        desc_map = {
            "open": "Opening price of the minute bar",
            "high": "Highest price during the minute bar",
            "low": "Lowest price during the minute bar",
            "close": "Closing price of the minute bar",
            "volume": "Number of shares traded during the minute bar",
            "vwap": "Volume-weighted average price for the minute bar",
            "transactions": "Number of transactions in the minute bar",
        }
        base_columns.append({
            "column": col,
            "type": "float" if col != "transactions" else "int",
            "description": desc_map.get(col, col),
            "source": "Polygon.io REST API (list_aggs)",
        })

    indicator_columns = []
    for meta in column_meta:
        desc = _describe_indicator_column(meta["indicator"], meta["column"], meta["params"])
        indicator_columns.append({
            "column": meta["column"],
            "type": "float",
            "indicator": meta["indicator"],
            "parameters": meta["params"],
            "library": meta["library"],
            "description": desc,
        })

    metadata = {
        "dataset": {
            "ticker": ticker,
            "from_date": from_date,
            "to_date": to_date,
            "timespan": "minute",
            "multiplier": 1,
            "bar_count": bar_count,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "data_source": {
            "provider": "Polygon.io",
            "plan": "Starter (2-year history, 15-min delayed)",
            "api": "REST v2 list_aggs with auto-pagination",
            "chunking": f"Date range split into ~{_DAYS_PER_CHUNK}-day windows to stay within {_POLYGON_MAX_BARS} bar API limit",
        },
        "calculation_engine": {
            "library": "pandas-ta",
            "version": getattr(ta, "version", "unknown"),
            "description": "Technical Analysis library for Python built on pandas, providing 150+ indicators",
            "url": "https://github.com/twopirllc/pandas-ta",
        },
        "columns": base_columns + indicator_columns,
        "processing": {
            "session_filter": session,
            "session_description": "Regular Trading Hours 09:30-16:00 ET" if session == "rth" else "Extended hours (pre-market + RTH + after-hours)",
            "forward_fill": forward_fill,
            "forward_fill_description": "Missing minute bars filled with previous close (volume=0)" if forward_fill else "No fill — raw Polygon data with gaps",
            "raw_bars_from_polygon": raw_bar_count,
            "bars_after_processing": filled_bar_count or bar_count,
            "bars_added_by_fill": (filled_bar_count - raw_bar_count) if forward_fill and filled_bar_count else 0,
        },
        "known_behaviors": {
            "vwap": "Polygon VWAP is a daily rolling VWAP, not per-bar. It accumulates across the session and routinely falls outside a single bar's H/L range. This is correct behavior.",
            "supertrend_nans": "supertl (long/support) is NaN during downtrends; superts (short/resistance) is NaN during uptrends. This is by design — use supert for the main line and supertd for direction.",
            "polygon_0700_contamination": "Polygon includes late-reported settlement trades in minute aggregates around 07:00-07:02 ET, inflating close prices by $4-6 on some bars. TradingView filters these out. This causes EMA/indicator divergence vs TradingView — longer-period EMAs recover more slowly from the contaminated bar.",
            "flat_bars": "Bars where High=Low=Open=Close occur in pre/post market when only 1 trade happens in the minute. Expected in extended hours.",
            "saturday_data": "Some bars may appear on Saturday UTC — these are Friday after-hours trades past midnight UTC. Filtered out when session='rth'.",
        },
        "notes": [
            "All float values are rounded to 6 decimal places",
            "Empty cells indicate NaN (indicator warm-up period or insufficient data)",
            "Timestamps represent the start of each minute bar (bar-open convention)",
            "Data is de-duplicated by timestamp and sorted chronologically",
            "VWAP is a daily rolling accumulation — not bounded by individual bar H/L",
        ],
    }
    return json.dumps(metadata, indent=2).encode("utf-8")


_INDICATOR_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    "ema": {
        "": "Exponential Moving Average — weighted moving average giving more weight to recent prices",
    },
    "bbands": {
        "bbl": "Bollinger Lower Band — lower envelope at N standard deviations below the SMA",
        "bbm": "Bollinger Middle Band — simple moving average center line",
        "bbu": "Bollinger Upper Band — upper envelope at N standard deviations above the SMA",
        "bbb": "Bollinger Band Width — (upper - lower) / middle, measures volatility expansion/contraction",
        "bbp": "Bollinger %B — (close - lower) / (upper - lower), shows where price sits within the bands (0=lower, 1=upper)",
    },
    "supertrend": {
        "supert": "Supertrend line — trailing stop that flips between support and resistance based on ATR",
        "supertd": "Supertrend direction — +1 (bullish/uptrend) or -1 (bearish/downtrend)",
        "supertl": "Supertrend long/support level — NaN during downtrends (by design)",
        "superts": "Supertrend short/resistance level — NaN during uptrends (by design)",
    },
    "macd": {
        "macd": "MACD line — difference between fast and slow EMA (momentum direction)",
        "macdh": "MACD histogram — MACD line minus signal line (momentum acceleration)",
        "macds": "MACD signal line — EMA of the MACD line (smoothed trend signal)",
    },
    "rsi": {
        "": "Relative Strength Index — oscillator (0-100) measuring speed of price changes; >70 overbought, <30 oversold",
    },
    "adx": {
        "adx": "Average Directional Index — trend strength (0-100); >25 trending, <20 ranging",
        "dmp": "Plus Directional Indicator (+DI) — bullish pressure strength",
        "dmn": "Minus Directional Indicator (-DI) — bearish pressure strength",
    },
    "atr": {
        "": "Average True Range — volatility measure in price units (not directional)",
    },
    "stoch": {
        "stochk": "Stochastic %K — raw oscillator showing close relative to high-low range",
        "stochd": "Stochastic %D — smoothed %K (signal line)",
    },
    "cci": {
        "": "Commodity Channel Index — deviation from statistical mean; >100 overbought, <-100 oversold",
    },
    "obv": {
        "": "On-Balance Volume — cumulative volume flow (+ on up closes, - on down closes)",
    },
    "vwap": {
        "": "Volume Weighted Average Price — session anchored average price weighted by volume",
    },
}


def _describe_indicator_column(indicator: str, column: str, params: str) -> str:
    """Get a rich description for an indicator column."""
    descs = _INDICATOR_DESCRIPTIONS.get(indicator, {})
    # Try prefix match (e.g. "bbl" matches "bbl_20_2.0_2.0")
    for prefix, desc in descs.items():
        if prefix and column.startswith(prefix):
            return desc
    # Fallback to generic indicator description or default
    if "" in descs:
        return descs[""]
    return f"{indicator} indicator output ({params})"


def build_metadata_csv(
    column_meta: List[Dict[str, Any]],
    ohlcv_cols: List[str],
) -> bytes:
    """Generate a CSV describing every column in the dataset."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["column", "type", "source", "indicator", "parameters", "description"])

    base = [
        ("unix_ts", "int", "Polygon.io", "", "", "Unix timestamp in milliseconds (UTC)"),
        ("iso_time", "string", "Derived", "", "", "ISO 8601 datetime (UTC)"),
    ]
    desc_map = {
        "open": "Opening price of the minute bar",
        "high": "Highest price during the minute bar",
        "low": "Lowest price during the minute bar",
        "close": "Closing price of the minute bar",
        "volume": "Number of shares traded during the minute bar",
        "vwap": "Polygon daily rolling VWAP — accumulates across the session (not per-bar)",
        "transactions": "Number of transactions in the minute bar",
    }
    for col in ohlcv_cols:
        col_type = "int" if col == "transactions" else "float"
        base.append((col, col_type, "Polygon.io", "", "", desc_map.get(col, col)))

    for row in base:
        writer.writerow(row)

    for meta in column_meta:
        desc = _describe_indicator_column(meta["indicator"], meta["column"], meta["params"])
        writer.writerow([
            meta["column"],
            "float",
            "pandas-ta",
            meta["indicator"],
            meta["params"],
            desc,
        ])

    return output.getvalue().encode("utf-8")


def _needs_param(param_names: List[str], *required: str) -> bool:
    for req in required:
        if not any(req in p for p in param_names[:5]):
            return False
    return True


def _fmt(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if isinstance(val, float):
        return f"{val:.6f}"
    return str(val)
